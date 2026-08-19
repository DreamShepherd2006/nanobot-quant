"""TD Sequential lumibot Strategy — backtest-ready trading rules.

Usage::

    from datetime import datetime
    from lumibot.backtesting import YahooDataBacktesting
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    result = TdSequentialStrategy.run_backtest(
        YahooDataBacktesting,
        datetime(2024, 1, 1),
        datetime(2025, 1, 1),
        parameters={"symbol": "AAPL", "quantity": 10},
    )
"""

from __future__ import annotations

import logging
import sys

from lumibot.strategies.strategy import Strategy

from nanobot_quant.order_tracker import OrderTracker
from nanobot_quant.portfolio import PortfolioEngine
from nanobot_quant.risk import RiskEngine
from nanobot_quant.strategies.td_sequential import calculate
from nanobot_quant.td_params import DEFAULT_TD_PARAMS


def _order_error(order) -> str | None:
    """提取 lumibot Order 错误信息（兼容 .error / ._error / .status）。"""
    if order is None:
        return "order is None"
    for attr in ("error", "_error", "error_message"):
        val = getattr(order, attr, None)
        if val:
            return str(val)
    if getattr(order, "status", None) == "error":
        return "status=error"
    return None


class TdSequentialStrategy(Strategy):
    """A lumibot strategy that uses TD Sequential signals for trading.

    Trading rules (daily bars):
    1. LONG entry: setup_buy >= entry_setup AND score > score_threshold AND no position
    2. LONG exit:  setup_sell >= exit_setup OR cd_sell >= exit_countdown

    Parameters are passed via the ``parameters`` dict in ``run_backtest()``.
    TD algorithm parameters (setup_period, weights, …) live in the same dict
    and default to the values in ``DEFAULT_TD_PARAMS`` (== pre-parameterisation
    hardcoded behaviour).
    """

    parameters = {
        "symbol": "AAPL",
        "quantity": 10,
        "quantity_mode": "fixed",  # "fixed" = fixed quantity; "value" = pv × pct; "fixed_amount" = fixed USD amount
        "td_fixed_amount": 10.0,   # quantity_mode=fixed_amount 时的每笔建仓金额（U）
        "sleeptime": "1D",         # strategy main-loop cadence ("1m"…"1W")
        "max_position_pct": 0.20,   # max % of portfolio in one position
        "max_drawdown_pct": 0.15,   # skip new entries when drawdown > 15%
        "stop_loss_pct": 0.10,      # exit when loss exceeds 10%
        **DEFAULT_TD_PARAMS,
    }

    #: sleeptime → get_historical_prices timestep (lumibot granularity names)
    _TIMESTEP_BY_SLEEPTIME = {
        "1m": "minute", "5m": "minute", "15m": "minute",
        "1H": "hour", "1D": "day", "1W": "week",
    }

    # ── lifecycle hooks ───────────────────────────────────────────

    def initialize(
        self,
        symbol: str | None = None,
        quantity: int | None = None,
        quantity_mode: str | None = None,
        sleeptime: str | None = None,
        max_position_pct: float | None = None,
        max_drawdown_pct: float | None = None,
    ):
        """Called once before the backtest/live loop starts (lumibot lifecycle)."""
        # 链上 broker（OnchainOSBroker）：交易对必须是 X/USDC，lumibot
        # 默认 quote_asset 是 USD(forex) → resolve_token_address("USD")
        # 失败导致 "Cannot resolve addresses: X→USD"。此处按执行通道显式设
        # 计价币：DEX=USDC（链上），CEX=USDT（Gate 交易对计价）。
        broker = getattr(self, "broker", None)
        broker_cls = broker.__class__.__name__ if broker is not None else ""
        self._is_live_broker = broker_cls in ("OnchainOSBroker", "CexBroker")
        if self._is_live_broker:
            from lumibot.entities import Asset
            self.quote_asset = Asset(
                "USDT" if broker_cls == "CexBroker" else "USDC",
                asset_type="crypto",
            )
        self.symbol = symbol or self.parameters.get("symbol", "AAPL")
        # 标的池（多标的扫描，2026-08-10）：每轮遍历 symbols 算信号，
        # 谁 Setup 9 谁执行；self.symbol 在每标的评估时切换。
        self.symbols = list(self.parameters.get("symbols") or [self.symbol])
        self.quantity = quantity or self.parameters.get("quantity", 10)
        self.quantity_mode = quantity_mode or self.parameters.get("quantity_mode", "fixed")
        # fixed_amount 模式的每笔固定金额（U；CEX=USDT / DEX=USDC，2026-08-19）
        self.fixed_amount = float(self.parameters.get("td_fixed_amount", 10.0) or 10.0)
        self.sleeptime = sleeptime or self.parameters.get("sleeptime", "1D")
        self._timestep = self._TIMESTEP_BY_SLEEPTIME.get(
            self.sleeptime, "day"
        )
        # 固定窗口：每轮拉最近 N 根 K 线（不累积增长）。
        # N 可经 exec_params.td_bars 配置（默认 120），必须覆盖 TD
        # 计数序列（setup 9 + countdown 13 约需 35+ 根），并低于
        # onchainos CLI 单次 300 根上限。
        self._min_history = int(
            self.parameters.get("min_history", 120) or 120
        )
        self._peak_portfolio = None  # track peak for drawdown calc

        # TD 循环日志可见性（2026-08-17）：gatekeeper 进程无 logging handler，
        # logger.info 被 Python lastResort 静默丢弃（仅 WARNING+ 到 stderr）——
        # BUY/SELL 分支（TD BATCH LONG / TD BATCH EXIT / TD SLOT SKIP 等）全是
        # logger.info，实盘静默不可见。策略 logger 显式挂 stderr handler，
        # 使 INFO 级直达 gatekeeper 日志（与 HOLD/BLOCK 的 print(stderr) 同可见）。
        #
        # lumibot v4.5.78 self.logger 是 LazyStrategyLogger proxy（非标准
        # Logger）：无 .handlers/.propagate/setLevel，且 __getattr__ 只转发
        # .info/.warning 等日志方法——直接访问 .handlers 抛 AttributeError
        # （StrategyLoggerAdapter 也没有该属性，LoggerAdapter 才经 .logger 委托）。
        # 正确路径：LazyStrategyLogger.logger → StrategyLoggerAdapter.logger → Logger。
        _lg = self.logger
        for _ in range(3):
            if isinstance(_lg, logging.Logger):
                break
            _lg = getattr(_lg, "logger", _lg)
        if not isinstance(_lg, logging.Logger):
            _lg = logging.getLogger(type(self).__name__)
        _lg.setLevel(logging.INFO)
        _lg.propagate = False  # 阻断 root/lastResort 重复输出
        if not any(
            isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
            for h in _lg.handlers
        ):
            _h = logging.StreamHandler(sys.stderr)
            _h.setFormatter(logging.Formatter("%(message)s"))
            _lg.addHandler(_h)

        # Build RiskEngine from parameters
        self._risk = RiskEngine(
            max_position_pct=max_position_pct
            or self.parameters.get("max_position_pct", 0.20),
            max_drawdown_pct=max_drawdown_pct
            or self.parameters.get("max_drawdown_pct", 0.15),
            stop_loss_pct=self.parameters.get("stop_loss_pct", 0.10),
        )

        # Build PortfolioEngine for position sizing & order construction.
        # quantity_mode="value" → no fixed default → PortfolioEngine falls back
        # to pv × max_position_pct sizing; "fixed" keeps the classic behaviour.
        self._portfolio = PortfolioEngine(
            strategy=self,
            max_position_pct=self._risk.max_position_pct,
            default_quantity=None if self.quantity_mode == "value" else self.quantity,
        )

        # Build OrderTracker — links Signals to lumibot Orders
        self.tracker = OrderTracker()

        # ── 子钱包分批（批次=子钱包，第一版）──────────────────────────
        # batch_manager 由 td_live 注入（None = 单仓模式，回测/现状不变）。
        # 注入后 BUY 占用 available slot、SELL 按 exit_order 平一个批次、
        # 止损/止盈每批独立检查——批次状态由 batches.BatchManager 维护。
        # 标的池（多标的）：td_live 注入 {symbol: BatchManager} 字典，
        # 每标的评估时取出对应 manager（per-symbol 台账隔离）。
        # 兼容单实例注入（测试/回测直接设 batch_manager）。
        self._batch_managers = getattr(self, "batch_managers", None) or {}
        if not self._batch_managers:
            bm_single = getattr(self, "batch_manager", None)
            if bm_single is not None:
                self._batch_managers = {self.symbol: bm_single}
        self.batch_manager = self._batch_managers.get(self.symbol)
        self._exit_order = self.parameters.get("exit_order", "fifo")
        self._take_profit_pct = float(
            self.parameters.get("take_profit_pct", 0.0) or 0.0
        )
        # ── 真分账 v1.1（2026-08-10）：BUY 起点 + 默认账户还原 ──
        # td_start_slot：BUY 扫描起点（完整循环 + 起点偏移，设 3 → 3→4→5→1→2）
        # _home_account：交易后还原目标 = wallets.json 默认账户（懒解析缓存）
        self._start_slot = int(self.parameters.get("td_start_slot", 1) or 1)
        self._home_account = None  # str | None
        self._tokens_json = self.parameters.get("tokens_json") or {}
        # ── 链上成交确认（2026-08-11）────────────────────────────
        # 已提交未确认的卖出/买入：台账保持 open（fail-safe），每轮迭代
        # 轮询官方 wallet history 补确认——SUCCESS 补释放/补建仓，
        # ERROR/CANCELLED 记失败（可重试），彻底消除“提交成功但链上
        # 未成交”的账实脱管（RENDER 3.06 实证）。
        self._pending_sells: dict[int, dict] = {}
        self._pending_buys: dict[int, dict] = {}
        # ── CEX 通道（Step 1，2026-08-17）：slot → 子账号 CexBroker 缓存 ──
        self._cex_brokers: dict[int, Any] = {}

        # TD algorithm params (subset of the strategy parameters dict)
        self._td_params = {
            k: self.parameters.get(k, v)
            for k, v in DEFAULT_TD_PARAMS.items()
        }

    def _calc(self, df, news_count: int = 0) -> dict:
        """按策略变体分发 calculate（原版 / 同花顺九转 / 富途 NINE）。

        strategy_variant 由 td_live 构造时从 strategy.json 注入（方案 A，
        2026-08-12）——TD 自主循环与策略选择页 / td-params 参数集对齐。
        """
        variant = str(self.parameters.get("strategy_variant", "") or "td_sequential")
        if variant == "td_sequential_cycle":
            from nanobot_quant.strategies.td_sequential_cycle import calculate as fn
        elif variant == "td_sequential_futu":
            from nanobot_quant.strategies.td_sequential_futu import calculate as fn
        else:
            fn = calculate  # 原版（模块级 import）
        return fn(df, news_count=news_count, params=self._td_params)

    def on_trading_iteration(self):
        """Called for each bar (trading day) during the backtest.

        Fetches all available historical bars up to the current bar,
        calls ``calculate()`` for the latest TD Sequential signal,
        then creates buy/sell orders based on the rules above.

        标的池模式：按池子顺序（=优先级）逐标的评估，谁 Setup 9 谁执行；
        同 bar 多标的命中按顺序全部处理（资金天然隔离）。
        """
        # ── 链上补确认（2026-08-11）────────────────────────────────
        # 每轮迭代先处理 pending 卖出/买入的链上确认（SUCCESS 补台账、
        # ERROR/CANCELLED 记失败、PENDING 继续等），再评估新信号。
        self._check_pending_confirmations()

        # ── 批次台账实时刷新（2026-08-10 修复）────────────────────────
        # td_live 在 Strategy 构造完成后才注入 batch_managers，而 lumibot
        # Strategy.__init__ 先调 initialize()——initialize 快照的
        # _batch_managers 恒为空 dict，导致 batch_mode=False，TD BUY 误走
        # 非 batch 分支（旧 value 模式 max(int(...),1) → CRCLX 1 个 → BLOCK）。
        # 每轮实时读取属性，多标的（batch_managers dict）与单实例注入
        # （batch_manager）都生效。
        self._batch_managers = getattr(self, "batch_managers", None) or {}
        if not self._batch_managers:
            bm_single = getattr(self, "batch_manager", None)
            if bm_single is not None:
                self._batch_managers = {self.symbol: bm_single}
        for sym in self.symbols:
            self.symbol = sym
            self.batch_manager = self._batch_managers.get(sym)
            self._evaluate_symbol()

    def _record(self, event: str, note: str = "", *, symbol=None, **extra) -> None:
        """更新实时状态 signal + 追加事件历史（仅 live 模式写文件）。

        2026-08-11：TD live 每轮信号动作（LONG/SELL/EXIT/SKIP/FAIL）
        记录到内存 LIVE_STATE 与事件文件，供 /config/td-table
        「实时监控」tab 展示。回测/纸交易（live_mode=False）只更新
        内存、不写事件文件。

        2026-08-11 方案 B：成交事件额外携带 slot/qty/price/direction/
        status/tx_hash/chain 结构化字段，供「📊 交易记录」区块展示。

        2026-08-11 修复：确认路径（pending 检查在主循环、self.symbol 可能
        是其他标的）必须显式传 symbol，否则事件标的错（15:49:01 CRCLX
        确认被记成 RENDER）。
        """
        try:
            from nanobot_quant import td_live_state
            sig = getattr(self, "_last_signal", {})
            sym = symbol or self.symbol
            td_live_state.update_symbol(sym, {
                **sig, "signal": event, "note": note,
            })
            if self.parameters.get("live_mode"):
                td_live_state.append_event({
                    "symbol": sym, "event": event, "note": note,
                    "price": sig.get("price", 0),
                    "score": sig.get("score", 0),
                    "setup_buy": sig.get("setup_buy", 0),
                    "setup_sell": sig.get("setup_sell", 0),
                    "cd_sell": sig.get("cd_sell", 0),
                    **extra,
                })
        except Exception:  # noqa: BLE001
            pass

    def _confirmed_tx_hash(self, info: dict, st) -> str:
        """确认路径的真实 tx_hash（2026-08-11 拍板：只做 detail 提取，

        不做额外补查——保持简单）。

        取 detail 响应的 data[0].txHash（非占位 UUID 直接用，SELL 确认
        场景零额外调用）；占位 UUID/查询失败返回空（事件显示「—」，
        不阻塞确认）。
        """
        from nanobot_quant.onchainos_cli import is_placeholder_tx_hash
        tx_hash = str(info.get("tx_hash") or "")
        raw = (st or {}).get("raw") or {}
        data = raw.get("data")
        d0 = data[0] if isinstance(data, list) and data else (
            data if isinstance(data, dict) else None
        )
        # DIAG（2026-08-12）：把 detail 全量打出来——字段名 + 值都看，
        # 确认 hash 到底叫 txHash 还是别的名字（不假设字段名）。
        try:
            detail_tx = str(d0.get("txHash") or "") if isinstance(d0, dict) else ""
            if isinstance(d0, dict):
                d0_keys = ",".join(d0.keys())
                hash_like = {
                    k: str(v)[:40] for k, v in d0.items()
                    if any(w in k.lower() for w in ("hash", "tx", "order", "id"))
                }
                d0_json = repr(d0)
            else:
                d0_keys = "-"; hash_like = {}; d0_json = "-"
            self.logger.info(
                "TD CONFIRM DETAIL | slot=%s symbol=%s status=%s in_hash=%s order_id=%s "
                "data_len=%s keys=[%s] hash_like=%s d0=%s",
                info.get("slot"), info.get("symbol", self.symbol),
                (st or {}).get("tx_status"),
                tx_hash[:14] or "-", str(info.get("order_id") or "")[:14] or "-",
                len(data) if isinstance(data, list) else -1,
                d0_keys, hash_like, d0_json,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            if isinstance(d0, dict) and d0.get("txHash"):
                real = str(d0["txHash"])
                if real and not is_placeholder_tx_hash(real):
                    return real
        except Exception:  # noqa: BLE001
            pass
        return tx_hash if not is_placeholder_tx_hash(tx_hash) else ""

    def _evaluate_symbol(self) -> None:
        """单标的评估（拉 K 线 → TD 计算 → 信号 → 真分账/常规下单）。"""
        # ── 1. Fetch historical data ──
        # 数据源契约：Gate CEX（drops_in_progress_bars=True）在 rows_to_df 已过滤
        # 进行中 bar；OnchainOS（DEX，无该参数默认 False）返回含进行中 bar——
        # 只有后者需多拉 1 根并丢弃（2026-08-17 A 修复第二部分：gate_cex 曾
        # 121→源过滤→120→策略再丢→119 < min_history 永久 SKIP，双重丢弃）。
        # 契约由 td_live 从 broker.data_source 读取并注入 parameters（lumibot
        # Strategy 基类不保存 data_source，策略无法自取）。
        _drops_in_progress = bool(self.parameters.get("drops_in_progress_bars", False))
        drop_in_progress = self._is_live_broker and not _drops_in_progress

        fetch_len = self._min_history
        if drop_in_progress:
            # live 数据源（OKX DEX kline）会返回进行中的最后一根 bar——
            # TD 是收盘价状态机，未完成 bar 的 close 会导致 setup 虚增/虚减
            # （单根 setup=9 被进行中 bar 重置挤掉而错过，2026-08-11 00:23
            # SOL 买9 未生效根因）。多拉 1 根供丢弃，信号基于最近已收盘
            # bar——与 TD 理论（bar 收盘时判定）及回测口径一致。
            fetch_len += 1
        try:
            bars = self.get_historical_prices(
                self.symbol,
                length=fetch_len,
                timestep=self._timestep,
            )
        except Exception as e:
            # 黑名单标的（Gate 无交易对/已下架，如 MU/VSC）静默跳过——
            # 首次失败已打印原因，不再每轮刷屏；重启 TD 循环重新探测
            try:
                from nanobot_quant.gate_cex_data import blacklist_reason
                if blacklist_reason(self.symbol):
                    return
            except ImportError:  # pragma: no cover
                pass
            print(
                f"[TD] DATA ERROR | symbol={self.symbol} {type(e).__name__}: {e}",
                file=sys.stderr, flush=True,
            )
            return

        if bars is None or bars.df.empty:
            print(
                f"[TD] DATA EMPTY | symbol={self.symbol} bars is None or empty",
                file=sys.stderr, flush=True,
            )
            return

        df = bars.df.copy()
        print(
            f"[TD] BARS | symbol={self.symbol} requested={fetch_len} "
            f"got={len(df)} drop_in_progress={drop_in_progress}",
            file=sys.stderr, flush=True,
        )
        if drop_in_progress and len(df) > 2:
            # 丢弃进行中的最后一根（live + 数据源未过滤；回测数据源全为已收盘 bar）
            df = df.iloc[:-1]
        print(
            f"[TD] BARS | symbol={self.symbol} final={len(df)} (min_history={self._min_history})",
            file=sys.stderr, flush=True,
        )

        # ── 2. Ensure OHLCV columns ──
        col_map = {
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
        for src, dst in col_map.items():
            if src in df.columns and dst not in df.columns:
                df.rename(columns={src: dst}, inplace=True)

        required = {"Open", "High", "Low", "Close", "Volume"}
        if not required.issubset(set(df.columns)):
            self.logger.warning(f"Missing columns: {set(df.columns)}")
            return

        # ── 3. Run TD Sequential ──
        if len(df) < self._min_history:
            print(
                f"[TD] SKIP | symbol={self.symbol} bars={len(df)} < min_history={self._min_history}",
                file=sys.stderr, flush=True,
            )
            return

        signal = self._calc(df)

        # ── 4. Evaluate signals ──
        setup_buy = signal.get("setup_buy", 0) or 0
        setup_sell = signal.get("setup_sell", 0) or 0
        cd_sell = signal.get("cd_sell", 0) or 0
        score = signal.get("score", 0) or 0

        # ── 信号周期状态（2026-08-19 分批次建仓语义，per-symbol）──
        # 一个信号周期内同一标的只建一次仓：建仓后 setup 计数单调不减
        # （9→10→11→12）视为同周期，跳过 BUY；计数变小（12→8 / 9→1）
        # 标记 reset → 新周期允许再建。slot 平仓释放后同周期也不建
        # （信号级，用户 2026-08-19 拍板）。SELL 侧不受影响。
        if not hasattr(self, "_cycle_state"):
            self._cycle_state: dict[str, dict] = {}
        st = self._cycle_state.get(self.symbol)
        if st is None:
            # 首次见到该标的：有 open 仓位 → 视为本周期已建仓（保守
            # 不追——重启边界：setup 累加期重启会立即再建，此守卫避免）
            st = {"bought": False, "prev_setup": 0, "reset": False}
            _bm = (getattr(self, "_batch_managers", None) or {}).get(
                self.symbol
            ) or getattr(self, "batch_manager", None)
            if _bm is not None and _bm.any_open():
                st["bought"] = True
            self._cycle_state[self.symbol] = st
        if setup_buy < st["prev_setup"]:
            st["reset"] = True  # 计数变小 → 新信号周期
        st["prev_setup"] = setup_buy
        price = signal.get("price", 0) or 0

        # ── 实时状态共享（td-table「实时监控」tab，2026-08-11）──
        # 无条件更新内存（同进程零成本）；信号动作由 _record 更新 signal。
        self._last_signal = {
            "setup_buy": setup_buy,
            "setup_sell": setup_sell,
            "cd_buy": signal.get("cd_buy", 0) or 0,
            "cd_sell": cd_sell,
            "score": score,
            "price": price,
            "time": str(df.index[-1]) if len(df) else "",
        }
        try:
            from nanobot_quant import td_live_state
            td_live_state.update_symbol(self.symbol, {
                **self._last_signal, "signal": "HOLD",
            })
        except Exception:  # noqa: BLE001
            pass

        has_position = self.get_position(self.symbol) is not None

        # ── Update peak portfolio for drawdown tracking ──
        pv = self.portfolio_value
        if self._peak_portfolio is None or pv > self._peak_portfolio:
            self._peak_portfolio = pv

        entry_setup = int(self._td_params.get("entry_setup", 9))
        exit_setup = int(self._td_params.get("exit_setup", 9))
        exit_countdown = int(self._td_params.get("exit_countdown", 13))
        score_threshold = float(self._td_params.get("score_threshold", 0.0))

        # 同周期已建仓 → 跳过 BUY（信号级：slot 平仓释放也不建，等 setup
        # 重置后再现新信号；只拦截 BUY，不 return——SELL/止损仍正常评估）
        if (
            st["bought"]
            and not st["reset"]
            and setup_buy >= entry_setup
            and score > score_threshold
        ):
            print(
                f"[TD] BATCH WAIT | symbol={self.symbol} 同周期已建仓"
                f"（setup_buy={setup_buy} 未重置），等新信号周期",
                file=sys.stderr, flush=True,
            )
            self._record(
                "WAIT", f"同周期已建仓 setup_buy={setup_buy}", symbol=self.symbol
            )
        tdst_filter = bool(self._td_params.get("tdst_filter", False))
        support = signal.get("tdst_support")

        # ── BUY signal: setup_buy >= entry_setup, score above threshold, slot available ──
        batch_manager = getattr(self, "batch_manager", None)
        batch_mode = batch_manager is not None
        can_buy = (
            bool(batch_manager.scan_buy_slots(self._start_slot))
            if batch_mode
            else not has_position
        )
        if (
            setup_buy >= entry_setup
            and score > score_threshold
            and can_buy
            and (not tdst_filter or (support is not None and price > support))
            # 信号周期门控（2026-08-19 分批次建仓）：同周期已建仓（bought 且
            # 未重置）→ 不 BUY；reset=True（计数变小）→ 新周期允许
            and not (st["bought"] and not st["reset"])
        ):
            # Actual order size (fixed quantity or pv × pct for value mode);
            # the risk gate must see the real position value, not the default.
            # （batch 模式移入 _buy_on_slot，以目标 slot 子钱包资产为基准——B 方案）
            # 外层 can_enter 仅非 batch 执行：batch 模式风控全部在 _buy_on_slot
            # 内完成（pv_slot × max_position_pct），否则高单价标的（CRCLX $66）
            # 在组合 $11 时被非 batch qty 预检 BLOCK，永远到不了 slot 风控
            qty = self._portfolio.calculate_quantity(price)
            if not batch_mode:
                result = self._risk.can_enter(
                    position_value=qty * price,
                    portfolio_value=pv,
                    peak_portfolio=self._peak_portfolio or pv,
                )
                if not result.approved:
                    print(
                        f"[TD] BLOCK ({result.check_name}) | symbol={self.symbol} {result.reason}",
                        file=sys.stderr, flush=True,
                    )
                    return
            reason = f"TD LONG setup_buy={setup_buy} score={score:.1f}"
            if batch_mode:
                # ── 真分账 v1.1（B 方案 2026-08-10）：目标 slot 子钱包为风控基准 ──
                # position_limit/数量比例/资金检查全部基于 slot 账户资产（pv_slot），
                # 在 _buy_on_slot 内完成（switch 后查 pv_slot）。
                # switch 失败/低于 min_account_value/风控拒绝/USDC 不足
                # → 返回 None → 跳下一 slot（拍板 1）。
                executed = False
                for slot in batch_manager.scan_buy_slots(self._start_slot):
                    if slot["slot"] in self._pending_buys:
                        continue  # 该 slot 已有买入待确认，防重复买
                    ret = self._buy_on_slot(slot, price, reason)
                    if ret is None:
                        continue
                    order, qty = ret
                    if order.is_filled():
                        # 链上已确认成交 → 建仓
                        self.batch_manager.open_lot(
                            slot=slot["slot"], qty=qty, entry_price=price,
                        )
                        # 交易状态变更立即落盘（重启不丢台账）
                        self.batch_manager.save()
                        self.logger.info(
                            f"TD BATCH LONG | symbol={self.symbol} slot={slot['slot']} "
                            f"price={price:.2f} qty={qty} "
                            f"setup_buy={setup_buy} score={score:.1f}"
                        )
                        self._record(
                            "LONG",
                            f"slot={slot['slot']} qty={qty:.6g} price={price:.2f}",
                            slot=slot["slot"], qty=qty, price=price,
                            direction="buy", status="ok",
                            tx_hash=((order.custom_params or {}).get("onchain_pending") or {}).get("tx_hash", ""),
                            chain=((order.custom_params or {}).get("onchain_pending") or {}).get("chain", ""),
                        )
                        executed = True
                        st["bought"] = True
                        st["reset"] = False
                        break
                    # 已提交未确认（PENDING，2026-08-11）→ 不 open_lot，
                    # 记录 pending 由后续轮询补建仓（fail-safe，防假成功幽灵仓）
                    if self._is_cex():
                        # CEX：无链上 hash——order_id 来自 CexBroker 设置的 identifier
                        pend, order_id, chain = {}, order.identifier, ""
                    else:
                        pend = (order.custom_params or {}).get("onchain_pending") or {}
                        order_id, chain = pend.get("order_id", ""), pend.get("chain", "")
                    # DIAG（2026-08-12）：打印提交后 pending 记录——确认 tx_hash/order_id
                    self.logger.info(
                        "TD BUY SUBMIT | slot=%s symbol=%s tx_hash=%s order_id=%s chain=%s",
                        slot["slot"], self.symbol,
                        (pend.get("tx_hash") or "-")[:20],
                        (order_id or "-")[:20],
                        chain,
                    )
                    self._pending_buys[slot["slot"]] = {
                        "slot": slot["slot"],
                        "tx_hash": pend.get("tx_hash", ""),
                        "order_id": order_id,
                        "chain": chain,
                        "qty": qty, "price": price, "reason": reason,
                        "account_id": slot.get("account_id", ""),
                        "symbol": self.symbol,
                        "cex": self._is_cex(),
                    }
                    self.logger.info(
                        f"TD BATCH LONG PENDING | symbol={self.symbol} slot={slot['slot']} "
                        f"price={price:.2f} qty={qty} setup_buy={setup_buy}"
                    )
                    self._record(
                        "LONG_PENDING",
                        f"slot={slot['slot']} qty={qty:.6g} price={price:.2f}",
                        slot=slot["slot"], qty=qty, price=price,
                        direction="buy", status="pending",
                        tx_hash=pend.get("tx_hash", ""),
                        chain=pend.get("chain", ""),
                    )
                    executed = True
                    st["bought"] = True
                    st["reset"] = False
                    break
                if not executed:
                    self.logger.info(
                        f"TD BATCH | symbol={self.symbol} 无可用资金 slot，跳过 BUY（见 TD SLOT SKIP 日志）"
                    )
                    self._record("SKIP", "无可用资金 slot，跳过 BUY")
                return
            else:
                req = self._portfolio.build_buy_order(
                    self.symbol, price, reason,
                    quantity=qty,
                )
                order = self._portfolio.submit_order(req)
                if order is not None:
                    self.tracker.track(
                        order_id=order.identifier,
                        symbol=self.symbol,
                        action="buy",
                        quantity=req.quantity,
                        tag=f"signal:td-buy:{setup_buy}:{score:.1f}",
                        signal=signal,
                        reason=reason,
                    )
                self.logger.info(
                    f"TD LONG  | symbol={self.symbol} price={price:.2f} qty={req.quantity} "
                    f"setup_buy={setup_buy} score={score:.1f}"
                )
                self._record(
                    "LONG",
                    f"qty={req.quantity:.6g} price={price:.2f}",
                )
                st["bought"] = True
                st["reset"] = False
                return

        # ── SELL signal / stop-loss / take-profit（分批：逐批独立）──
        elif batch_mode or has_position:
            if batch_mode:
                self._handle_batch_exits(price, signal, setup_sell, cd_sell,
                                         exit_setup, exit_countdown)
                return

            position = self.get_position(self.symbol)
            exit_reason = ""

            # Check TD exit signal
            if setup_sell >= exit_setup:
                exit_reason = f"setup_sell={setup_sell}"
            elif cd_sell >= exit_countdown:
                exit_reason = f"cd_sell={cd_sell}"

            # Check stop-loss
            if not exit_reason and position is not None and position.avg_fill_price:
                sl = self._risk.should_exit(price, position.avg_fill_price)
                if sl.approved:
                    exit_reason = f"stop_loss: {sl.reason}"

            if exit_reason:
                req = self._portfolio.build_sell_order(
                    self.symbol, price, exit_reason,
                )
                order = self._portfolio.submit_order(req)
                if order is not None:
                    self.tracker.track(
                        order_id=order.identifier,
                        symbol=self.symbol,
                        action="sell",
                        quantity=req.quantity,
                        tag=f"signal:td-sell:{exit_reason}",
                        signal=signal,
                        reason=exit_reason,
                    )
                self.logger.info(
                    f"TD EXIT  | symbol={self.symbol} price={price:.2f} qty={req.quantity} {exit_reason}"
                )
                self._record(
                    "EXIT",
                    f"{exit_reason} qty={req.quantity:.6g} price={price:.2f}",
                )
                return

        # ── No signal this bar ──
        print(
            f"[TD] HOLD | symbol={self.symbol} price={price:.4f} setup_buy={setup_buy} "
            f"setup_sell={setup_sell} cd_sell={cd_sell} score={score:.1f}",
            file=sys.stderr, flush=True,
        )

    # ── 分批平仓（批次=子钱包，第一版）──────────────────────────────
    def _handle_batch_exits(
        self,
        price: float,
        signal: dict,
        setup_sell: int,
        cd_sell: int,
        exit_setup: int,
        exit_countdown: int,
    ) -> None:
        """分批模式下的平仓逻辑：先逐批止损/止盈，再处理 TD SELL 信号。

        顺序（文档 16.6）：止盈/止损逐批检查先于信号（防爆仓优先）；
        TD SELL 信号按 exit_order 平一个 open 批次（FIFO/LIFO）。
        每个命中批次卖出量 = 该批 lot.qty（链上实际余额由对账层处理）。
        """
        bm = self.batch_manager
        # 1) 止损/止盈逐批独立检查（take_profit_pct=0 时只查止损）
        hits = bm.check_exit(
            price,
            stop_loss_pct=self._risk.stop_loss_pct,
            take_profit_pct=self._take_profit_pct,
            order=self._exit_order,
        )
        for s in hits:
            self._sell_lot(s, price, signal, s.pop("_exit_reason", "exit"))
        # 2) TD SELL 信号 → 按 exit_order 平一个批次（止损刚平完则无批次可平）
        if setup_sell >= exit_setup or cd_sell >= exit_countdown:
            s = bm.pick_exit_slot(self._exit_order)
            if s is not None:
                reason = (
                    f"setup_sell={setup_sell}"
                    if setup_sell >= exit_setup
                    else f"cd_sell={cd_sell}"
                )
                self._sell_lot(s, price, signal, reason)
            else:
                # 可观测性：无仓卖 9 显式提示，区分「信号未出现」与
                # 「信号出现但无 open 批次」（fail-closed，不做空）
                self.logger.info(
                    f"TD SELL SKIP | symbol={self.symbol} 无 open 批次（setup_sell={setup_sell} "
                    f"cd_sell={cd_sell}）"
                )

    def _symbol_min_hold(self) -> float:
        """当前标的的链上保留量（tokens.json min_hold，SOL 用作 gas 底线）。

        对账导入时已扣减（导入量 = 余额 − min_hold），SELL 缩量卖出时
        同样保留 min_hold，防止卖出 gas 后子钱包无法交易。
        """
        if self._is_cex():
            # CEX 通道无 gas 保留概念（交易所内资金/持仓，无链上手续费）
            return 0.0
        try:
            from nanobot_quant.tokens_store import token_meta
            tokens = self.parameters.get("tokens_json") or []
            return float(token_meta(self.symbol, tokens).get("min_hold") or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def _sell_lot(
        self, slot: dict, price: float, signal: dict, exit_reason: str
    ) -> None:
        """卖出一个批次（lot.qty），链上确认成交后才释放 slot。

        v1.1 真分账：卖出前 switch 到该 slot 绑定的子钱包，交易后还原
        默认账户；卖出量改为 ``float(lot.qty)``（修复 int 截断小数问题，
        如 0.05 CRCLX）。

        2026-08-10 链上校验（缩量卖出，用户拍板）：switch 后查该账户
        实际余额——余额 < lot.qty 按实际余额卖（不跳过、不卖空），
        余额 0 或查询失败跳过该批并告警。

        2026-08-11 链上成交确认改造：close_lot 从“提交前”移到“链上
        确认成交后”——以官方 `wallet history` txStatus 为准：SUCCESS
        才 close_lot 释放；PENDING 记入 _pending_sells（台账保持 open，
        后续轮询补确认、防重复卖）；ERROR/CANCELLED 记 EXIT_FAIL（台账
        保持 open，下轮可重试）。彻底消除“提交成功但链上未成交”导致
        的账实脱管（RENDER 3.06 实证）。
        """
        if self._is_cex():
            return self._sell_lot_cex(slot, price, signal, exit_reason)
        if slot["slot"] in self._pending_sells:
            return  # 该 slot 已有卖出待确认，防重复卖
        lot = self.batch_manager.get_lot(slot["slot"])
        if lot is None:
            return
        qty = float(lot["qty"])
        aid = slot.get("account_id")
        home = self._home_account_id()
        switched = self._wallet_switch(aid) if aid else True
        order = None
        try:
            if aid:
                bal = self._slot_token_balance(self.symbol)
                if bal is None or bal < 0:
                    self.logger.warning(
                        f"TD BATCH EXIT SKIP | symbol={self.symbol} slot={slot['slot']} "
                        f"链上余额查询失败"
                    )
                    # 查询失败 = 链上状态未知 → 台账保持 open（fail-safe），
                    # 不释放（链上可能仍有持仓，防账实脱节）
                    self._record("EXIT_SKIP", f"slot={slot['slot']} 链上余额查询失败")
                    return
                if bal <= 0:
                    # 链上无持仓 → 幽灵批次，释放台账
                    self.batch_manager.close_lot(slot["slot"])
                    self.batch_manager.save()
                    self.logger.warning(
                        f"TD BATCH EXIT SKIP | symbol={self.symbol} slot={slot['slot']} "
                        f"链上余额为 0（台账 {qty} 已释放）"
                    )
                    self._record("EXIT_SKIP", f"slot={slot['slot']} 链上余额为 0")
                    return
                min_hold = self._symbol_min_hold()
                if bal <= min_hold:
                    # 2026-08-11 修复：链上仅剩保留量（如 SOL gas 0.01）→
                    # 视为无持仓，跳过卖出并释放台账（防卖 gas + 自动清理
                    # 幽灵批次——台账 open 但链上从未成交）。
                    self.batch_manager.close_lot(slot["slot"])
                    self.batch_manager.save()
                    self.logger.warning(
                        f"TD BATCH EXIT SKIP | symbol={self.symbol} slot={slot['slot']} "
                        f"链上余额 {bal:.6f} ≤ 保留量 {min_hold} "
                        f"（台账 {qty} 已释放）"
                    )
                    self._record(
                        "EXIT_SKIP",
                        f"slot={slot['slot']} 链上仅剩 {bal:.6f} ≤ 保留量 {min_hold}",
                    )
                    return
                if bal < qty:
                    sell_qty = max(bal - min_hold, 0.0)
                    self.logger.warning(
                        f"TD BATCH EXIT SHRINK | symbol={self.symbol} slot={slot['slot']} "
                        f"台账 {qty} 链上 {bal:.6f} → 缩量卖出 {sell_qty:.6f}"
                    )
                    qty = sell_qty
                    self._record(
                        "EXIT_SHRINK",
                        f"slot={slot['slot']} 台账 {qty:.6g} 链上 {bal:.6f} → 缩量",
                    )
            req = self._portfolio.build_sell_order(
                self.symbol, price, exit_reason,
                quantity=qty,
            )
            order = self._portfolio.submit_order(req)
        finally:
            if switched and home and home != aid:
                try:
                    self._wallet_switch(home)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(f"TD RESTORE ERR | symbol={self.symbol} {exc}")
        if order is not None and not _order_error(order):
            if order.is_filled():
                # 链上已确认成交 → 释放台账（close_lot 后置，2026-08-11）
                self.batch_manager.close_lot(slot["slot"])
                self.batch_manager.save()
                self.tracker.track(
                    order_id=order.identifier,
                    symbol=self.symbol,
                    action="sell",
                    quantity=qty,
                    tag=f"signal:td-sell:{exit_reason}",
                    signal=signal,
                    reason=exit_reason,
                )
                self.logger.info(
                    f"TD BATCH EXIT | symbol={self.symbol} slot={slot['slot']} price={price:.2f} "
                    f"qty={qty} {exit_reason}"
                )
                self._record(
                    "EXIT",
                    f"slot={slot['slot']} {exit_reason} qty={qty:.6g} price={price:.2f}",
                    slot=slot["slot"], qty=qty, price=price,
                    direction="sell", status="ok",
                    tx_hash=((order.custom_params or {}).get("onchain_pending") or {}).get("tx_hash", ""),
                    chain=((order.custom_params or {}).get("onchain_pending") or {}).get("chain", ""),
                )
                return
            # 已提交未确认（PENDING）→ 台账保持 open + pending 记录，
            # 后续轮询补确认（SUCCESS 补释放；失败则保持 open 可重试）
            pend = (order.custom_params or {}).get("onchain_pending") or {}
            self._pending_sells[slot["slot"]] = {
                "slot": slot["slot"],
                "tx_hash": pend.get("tx_hash", ""),
                "order_id": pend.get("order_id", ""),
                "chain": pend.get("chain", ""),
                "qty": qty,
                "price": price,
                "exit_reason": exit_reason,
                "account_id": slot.get("account_id", ""),
                "symbol": self.symbol,
            }
            self.logger.info(
                f"TD BATCH EXIT PENDING | symbol={self.symbol} slot={slot['slot']} price={price:.2f} "
                f"qty={qty} {exit_reason}"
            )
            self._record(
                "EXIT_PENDING",
                f"slot={slot['slot']} {exit_reason} qty={qty:.6g} price={price:.2f}",
                slot=slot["slot"], qty=qty, price=price,
                direction="sell", status="pending",
                tx_hash=pend.get("tx_hash", ""),
                chain=pend.get("chain", ""),
            )
            return
        # 明确失败（quote 解析失败、资金不足、链上确认失败等）→ 台账保持
        # open（未释放，无需恢复），下轮 setup_sell≥9 可自动重试卖出
        err = _order_error(order) or "order is None"
        self.logger.warning(
            f"TD BATCH EXIT FAIL | symbol={self.symbol} slot={slot['slot']} price={price:.2f} "
            f"qty={qty} {exit_reason} error={err}"
        )
        _pend = ((order.custom_params or {}).get("onchain_pending") or {}) if order is not None else {}
        self._record(
            "EXIT_FAIL", f"slot={slot['slot']} {exit_reason} {err}",
            slot=slot["slot"], qty=qty, price=price,
            direction="sell", status="fail",
            tx_hash=_pend.get("tx_hash", ""),
            chain=_pend.get("chain", ""),
        )

    def _check_pending_confirmations(self) -> None:
        """链上补确认（2026-08-11）：每轮迭代轮询官方 wallet history。

        - SELL pending：SUCCESS → close_lot 补释放 + EXIT 记录；
          ERROR/CANCELLED → EXIT_FAIL（台账保持 open，下轮可重试卖出）。
        - BUY pending：SUCCESS → open_lot 补建仓 + LONG 记录；
          ERROR/CANCELLED → BUY_FAIL（不建仓）。
        PENDING/UNKNOWN → 继续等待。
        """
        from nanobot_quant.onchainos_cli import swap_status
        home = self._home_account_id()
        for slot_id in list(self._pending_sells):
            info = self._pending_sells[slot_id]
            if info.get("cex"):
                continue  # CEX pending 确认在 Step 2（2026-08-17）——台账保持 open
            aid = info.get("account_id", "")
            if aid and not self._wallet_switch(aid):
                self.logger.warning(
                    f"TD PENDING CHECK SKIP | slot={slot_id} switch 到账户失败"
                )
                continue
            try:
                st = swap_status(
                    info.get("tx_hash", ""), info.get("order_id", ""),
                    info.get("chain", "solana"),
                )
                status = st.get("tx_status") if st else "UNKNOWN"
                # DIAG（2026-08-12）：SELL 对称——打印 pending 检查 detail + SUCCESS 回写
                try:
                    from nanobot_quant.onchainos_cli import is_placeholder_tx_hash as _ph
                    _raw = (st or {}).get("raw") or {}
                    _data = _raw.get("data")
                    _d0 = _data[0] if isinstance(_data, list) and _data else (
                        _data if isinstance(_data, dict) else None
                    )
                    _dtx = str(_d0.get("txHash") or "") if isinstance(_d0, dict) else ""
                    self.logger.info(
                        "TD PENDING DIAG | slot=%s symbol=%s side=SELL status=%s in_tx=%s "
                        "detail_txHash=%s placeholder=%s",
                        slot_id, info.get("symbol", self.symbol), status,
                        (info.get("tx_hash") or "-")[:16],
                        _dtx[:16] or "EMPTY", _ph(info.get("tx_hash") or ""),
                    )
                    if status == "SUCCESS" and _dtx and not _ph(_dtx):
                        info["tx_hash"] = _dtx  # 回写真实 hash——确认事件显示真实
                        self.logger.info(
                            "TD PENDING TXBACK | slot=%s side=SELL tx_hash -> %s",
                            slot_id, _dtx,
                        )
                except Exception:  # noqa: BLE001
                    pass
                if status != "SUCCESS":
                    self.logger.info(
                        f"TD PENDING CHECK | slot={slot_id} 账户={aid[:8] or 'home'} "
                        f"{info.get('chain')} status={status}"
                    )
                # 余额核对兜底（2026-08-11，对称 BUY）：卖出成交后该标的链上余额≈0——
                # 连续 3 轮订单状态查不到 → 查 slot 账户链上余额裁决（链上真相）。
                if status not in ("SUCCESS", "ERROR", "CANCELLED"):
                    info["unknown_count"] = info.get("unknown_count", 0) + 1
                    if info["unknown_count"] >= 3 and aid:
                        bal = self._slot_token_balance(
                            info.get("symbol", self.symbol)
                        )
                        if bal >= 0 and bal <= info.get("qty", 0) * 0.1:
                            self.logger.info(
                                f"TD PENDING BALANCE CONFIRM | slot={slot_id} "
                                f"链上余额 {bal:.6g} ≤ 残留 {info['qty'] * 0.1:.6g} → 卖出成交"
                            )
                            status = "SUCCESS"
                if status == "SUCCESS":
                    bm = self._batch_managers.get(
                        info.get("symbol", self.symbol)
                    ) or self.batch_manager
                    bm.close_lot(slot_id)
                    bm.save()
                    self.logger.info(
                        f"TD BATCH EXIT (确认) | slot={slot_id} "
                        f"qty={info['qty']} {info.get('exit_reason', '')}"
                    )
                    self._record(
                        "EXIT",
                        f"slot={slot_id} 链上确认平仓 qty={info['qty']:.6g}",
                        symbol=info.get("symbol", self.symbol),
                        slot=slot_id, qty=info.get("qty", 0),
                        price=info.get("price", 0),
                        actual_price=self._actual_price_from_st(st),
                        direction="sell", status="ok",
                        tx_hash=self._confirmed_tx_hash(info, st),
                        chain=info.get("chain", ""),
                    )
                    del self._pending_sells[slot_id]
                elif status in ("ERROR", "CANCELLED"):
                    self.logger.warning(
                        f"TD BATCH EXIT FAIL | slot={slot_id} 链上确认失败 {status}"
                    )
                    self._record(
                        "EXIT_FAIL",
                        f"slot={slot_id} 链上确认失败 {status}",
                        slot=slot_id, qty=info.get("qty", 0),
                        price=info.get("price", 0),
                        direction="sell", status="fail",
                        tx_hash=info.get("tx_hash", ""),
                        chain=info.get("chain", ""),
                    )
                    del self._pending_sells[slot_id]
                # PENDING/UNKNOWN → 继续等待
            finally:
                if aid and home and home != aid:
                    try:
                        self._wallet_switch(home)
                    except Exception as exc:  # noqa: BLE001
                        self.logger.warning(f"TD RESTORE ERR | {exc}")
        for slot_id in list(self._pending_buys):
            info = self._pending_buys[slot_id]
            if info.get("cex"):
                continue  # CEX pending 确认在 Step 2（2026-08-17）——不 open_lot（fail-safe）
            aid = info.get("account_id", "")
            if aid and not self._wallet_switch(aid):
                self.logger.warning(
                    f"TD PENDING CHECK SKIP | slot={slot_id} switch 到账户失败"
                )
                continue
            try:
                st = swap_status(
                    info.get("tx_hash", ""), info.get("order_id", ""),
                    info.get("chain", "solana"),
                )
                status = st.get("tx_status") if st else "UNKNOWN"
                # DIAG（2026-08-12）：打印每次 BUY pending 检查的 detail——确认
                # txHash 提取/回写（方案 A：SUCCESS 时回写真实 hash，确认事件显示）
                try:
                    from nanobot_quant.onchainos_cli import is_placeholder_tx_hash as _ph
                    _raw = (st or {}).get("raw") or {}
                    _data = _raw.get("data")
                    _d0 = _data[0] if isinstance(_data, list) and _data else (
                        _data if isinstance(_data, dict) else None
                    )
                    _dtx = str(_d0.get("txHash") or "") if isinstance(_d0, dict) else ""
                    self.logger.info(
                        "TD PENDING DIAG | slot=%s symbol=%s status=%s in_tx=%s "
                        "detail_txHash=%s placeholder=%s",
                        slot_id, info.get("symbol", self.symbol), status,
                        (info.get("tx_hash") or "-")[:16],
                        _dtx[:16] or "EMPTY", _ph(info.get("tx_hash") or ""),
                    )
                    if status == "SUCCESS" and _dtx and not _ph(_dtx):
                        info["tx_hash"] = _dtx  # 回写真实 hash——确认事件显示真实
                        self.logger.info(
                            "TD PENDING TXBACK | slot=%s tx_hash -> %s",
                            slot_id, _dtx,
                        )
                except Exception:  # noqa: BLE001
                    pass
                if status != "SUCCESS":
                    self.logger.info(
                        f"TD PENDING CHECK | slot={slot_id} 账户={aid[:8] or 'home'} "
                        f"{info.get('chain')} status={status}"
                    )
                # 余额核对兜底（2026-08-11）：占位 hash 或 OKX 状态回填不可靠时，
                # 连续 3 轮查不到 → 直接查 slot 账户链上余额裁决（链上真相）。
                if status not in ("SUCCESS", "ERROR", "CANCELLED"):
                    info["unknown_count"] = info.get("unknown_count", 0) + 1
                    if info["unknown_count"] >= 3 and aid:
                        bal = self._slot_token_balance(
                            info.get("symbol", self.symbol)
                        )
                        if bal >= info.get("qty", 0) * 0.9:
                            self.logger.info(
                                f"TD PENDING BALANCE CONFIRM | slot={slot_id} "
                                f"链上余额 {bal:.6g} ≥ 预期 {info['qty']:.6g} → 成交"
                            )
                            status = "SUCCESS"
                        elif bal >= 0:
                            self.logger.info(
                                f"TD PENDING BALANCE | slot={slot_id} "
                                f"链上余额 {bal:.6g}（预期 {info['qty']:.6g}）未确认"
                            )
                if status == "SUCCESS":
                    bm = self._batch_managers.get(
                        info.get("symbol", self.symbol)
                    ) or self.batch_manager
                    if bm.open_lot(
                        slot=slot_id, qty=info["qty"], entry_price=info["price"],
                    ):
                        bm.save()
                        self.logger.info(
                            f"TD BATCH LONG (确认) | slot={slot_id} "
                            f"qty={info['qty']} price={info['price']:.2f}"
                        )
                        self._record(
                                "LONG",
                                f"slot={slot_id} 链上确认建仓 qty={info['qty']:.6g}",
                                symbol=info.get("symbol", self.symbol),
                                slot=slot_id, qty=info.get("qty", 0),
                                price=info.get("price", 0),
                                actual_price=self._actual_price_from_st(st),
                                direction="buy", status="ok",
                                tx_hash=self._confirmed_tx_hash(info, st),
                                chain=info.get("chain", ""),
                            )
                    del self._pending_buys[slot_id]
                elif status in ("ERROR", "CANCELLED"):
                    self.logger.warning(
                        f"TD BATCH BUY FAIL | slot={slot_id} 链上确认失败 {status}"
                    )
                    self._record(
                        "BUY_FAIL",
                        f"slot={slot_id} 链上确认失败 {status}",
                        slot=slot_id, qty=info.get("qty", 0),
                        price=info.get("price", 0),
                        direction="buy", status="fail",
                        tx_hash=info.get("tx_hash", ""),
                        chain=info.get("chain", ""),
                    )
                    del self._pending_buys[slot_id]
                # PENDING/UNKNOWN → 继续等待
            finally:
                if aid and home and home != aid:
                    try:
                        self._wallet_switch(home)
                    except Exception as exc:  # noqa: BLE001
                        self.logger.warning(f"TD RESTORE ERR | {exc}")

    # ── 真分账 v1.1：子钱包 switch / 资金检查 / 还原 ────────────────

    def _home_account_id(self) -> str | None:
        """默认账户（wallets.json is_default）——交易后还原目标。

        懒解析并缓存；解析失败返回 None（此时交易后不还原，仅告警）。
        """
        if self._home_account is not None:
            return self._home_account or None
        self._home_account = ""
        try:
            from nanobot_quant.tools.tools_wallet import wallet_accounts
            r = wallet_accounts() or {}
            # wallet_accounts() 返回 {"status":"ok","data":{"accounts":[...]}}
            # （2026-08-10：曾误读 r["accounts"] 恒为空 → home="" →
            #  交易后不还原默认账户，活跃账户漂移留在 slot 子钱包）
            accs = (r.get("data") or {}).get("accounts") or []
            for a in accs:
                if a.get("is_default"):
                    self._home_account = a.get("account_id") or ""
                    break
            else:
                self._home_account = accs[0].get("account_id") or "" if accs else ""
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"TD HOME ERR | {exc}")
        return self._home_account or None

    def _wallet_switch(self, account_id: str) -> bool:
        """switch 到目标子钱包（全局状态，改写 selected_account_id）。

        兼容 tools_wallet 规范化契约（{"status": "ok", ...}）与 CLI 原始
        信封（{"ok": true, ...}）——曾只查 r.get("ok")，tools_wallet 返回
        {"status":"ok"} 时恒判失败（TD SLOT SKIP | switch 失败误报，
        CLI 实际已切换，2026-08-10 15:28 实测）。
        """
        try:
            from nanobot_quant.tools.tools_wallet import wallet_switch
            r = wallet_switch(account_id)
            return bool(r and (r.get("ok") or r.get("status") == "ok"))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"TD SWITCH ERR | {exc}")
            return False

    def _slot_quote_balance(self, quote_symbol: str = "USDC") -> float:
        """当前（已 switch 的）子钱包 quote 币种余额。

        wallet_balance() 返回 {status, data}，真实资产在
        data.details[0].tokenAssets —— 用 get_token_assets() 归一化
        （修复 2026-08-10：此前误读 r["token_assets"] 恒返回 0，
        真分账 BUY 资金检查形同虚设）。查询失败返回 -1（保守跳过）。
        """
        try:
            from nanobot_quant.onchainos_cli import get_token_assets
            from nanobot_quant.tools.tools_wallet import wallet_balance
            r = wallet_balance() or {}
            for a in get_token_assets(r.get("data") or {}):
                if str(a.get("symbol", "")).upper() == quote_symbol.upper():
                    return float(a.get("balance") or 0)
            return 0.0
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"TD BALANCE ERR | {exc}")
            return -1.0

    def _slot_token_balance(self, symbol: str) -> float:
        """当前（已 switch 的）子钱包该标的实际余额（SELL 链上校验）。

        按 tokens.json 条目的合约地址匹配（原生币按 symbol）；
        查询失败返回 -1（调用方保守跳过卖出）。
        """
        try:
            from nanobot_quant.onchainos_cli import get_token_assets
            from nanobot_quant.tokens_store import token_meta
            from nanobot_quant.tools.tools_wallet import wallet_balance
            meta = token_meta(symbol)
            address = str(meta.get("address") or "")
            r = wallet_balance() or {}
            for a in get_token_assets(r.get("data") or {}):
                if address:
                    addr = str(
                        a.get("tokenAddress") or a.get("token_address") or ""
                    )
                    if addr.lower() == address.lower():
                        return float(a.get("balance") or 0)
                # 地址匹配失败后必须回退 symbol 匹配：原生 SOL 的
                # tokenAddress 恒为空字符串，而 tokens.json 登记的是
                # wSOL 地址（So111…）——不回退则恒判 0 → SELL 误释放台账
                if str(a.get("symbol", "")).upper() == symbol.upper():
                    return float(a.get("balance") or 0)
            return 0.0
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"TD TOKEN BALANCE ERR | {exc}")
            return -1.0

    def _switch_submit_restore(self, account_id: str | None, submit_fn):
        """switch → submit → 还原默认账户（SELL/止损/止盈路径）。

        account_id 为空（单仓/回测）时跳过 switch 直接 submit。
        """
        home = self._home_account_id()
        switched = (
            self._wallet_switch(account_id) if account_id else True
        )
        try:
            return submit_fn()
        finally:
            if switched and home and home != account_id:
                try:
                    self._wallet_switch(home)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(f"TD RESTORE ERR | symbol={self.symbol} {exc}")

    def _buy_on_slot(self, slot: dict, price: float, reason: str):
        """真分账 BUY（B 方案 2026-08-10）：switch 到 slot 子钱包 →
        目标 slot 账户总资产（pv_slot）→ min_account_value 门槛 → qty
        （fixed=td_quantity；value=pv_slot×max_position_pct 小数不取整）→
        position_limit（基于 pv_slot）→ USDC 资金检查 → submit → 还原默认账户。

        返回 (order, qty) 或 None（switch 失败/余额查询失败/低于资金门槛/
        风控拒绝/USDC 不足 → 调用方跳下一 slot）。
        """
        if self._is_cex():
            return self._buy_on_slot_cex(slot, price, reason)
        aid = slot.get("account_id")
        home = self._home_account_id()
        if aid and not self._wallet_switch(aid):
            self.logger.warning(f"TD SLOT SKIP | symbol={self.symbol} slot={slot['slot']} switch 失败")
            return None
        try:
            pv_slot = self._slot_portfolio_value()
            if pv_slot <= 0:
                self.logger.warning(
                    f"TD SLOT SKIP | symbol={self.symbol} slot={slot['slot']} 余额查询失败/为零"
                )
                return None
            # 子账户最小资金门槛（BUY-only；SELL/止损/止盈平仓永远允许）
            min_v = float(self.parameters.get("min_account_value", 0) or 0)
            if min_v > 0 and pv_slot < min_v:
                self.logger.warning(
                    f"TD SLOT SKIP (min_account_value) | symbol={self.symbol} slot={slot['slot']} "
                    f"pv=${pv_slot:.2f} < ${min_v:.2f}"
                )
                return None
            # 数量：fixed=固定 td_quantity；value=pv_slot × max_position_pct / price；
            # fixed_amount=固定金额 td_fixed_amount / price（2026-08-19 新增）
            # （小数不取整——避免 SOL $77/CRCLX $68 等高价标的 int 截断成 0
            #  后被 max(...,1) 抬成 1 个导致永远 BLOCK；金额驱动自动适配价格差）
            if self.quantity_mode == "value":
                qty = pv_slot * self._risk.max_position_pct / price if price > 0 else 0.0
            elif self.quantity_mode == "fixed_amount":
                # 固定金额：每笔建仓花固定 U（如 10U），与 slot 资产规模无关
                qty = self.fixed_amount / price if price > 0 else 0.0
            else:
                qty = float(self.quantity or 0)
            if qty <= 0:
                return None
            if self.quantity_mode != "fixed_amount":
                # 2026-08-19 拍板：fixed_amount 跳过单仓上限校验（金额即用户显式仓位），
                # 资金检查（下方 USDC/USDT 余额 ≥ needed）保留
                result = self._risk.can_enter(
                    position_value=qty * price,
                    portfolio_value=pv_slot,
                    peak_portfolio=pv_slot,
                )
                if not result.approved:
                    print(
                        f"[TD] BLOCK ({result.check_name}) | symbol={self.symbol} slot={slot['slot']} "
                        f"pos=${qty * price:.2f} > "
                        f"{self._risk.max_position_pct * 100:.0f}% of slot pv=${pv_slot:.2f}",
                        file=sys.stderr, flush=True,
                    )
                    return None
            bal = self._slot_quote_balance("USDC")
            needed = qty * price
            if bal is None or bal < 0 or bal < needed:
                self.logger.warning(
                    f"TD SLOT SKIP | symbol={self.symbol} slot={slot['slot']} 资金不足 "
                    f"({bal:.4f} < {needed:.4f} USDC)"
                )
                return None
            req = self._portfolio.build_buy_order(
                self.symbol, price, reason, quantity=qty,
            )
            order = self._portfolio.submit_order(req)
            if order is None or _order_error(order):
                # 2026-08-11 修复：下单失败（如 6010 滑点保护、资金不足）
                # 不得 open_lot——此前无条件 open_lot + 打 TD BATCH LONG 产生
                # 幽灵批次（台账有持仓、链上没有），SELL 时链上校验才暴露。
                    err = _order_error(order) or "order is None"
                    self.logger.info(
                        f"TD BATCH BUY FAIL | slot={slot['slot']} "
                        f"price={price:.2f} qty={qty} {err}"
                    )
                    self._record(
                        "BUY_FAIL", f"slot={slot['slot']} {err}",
                        slot=slot["slot"], qty=qty, price=price,
                        direction="buy", status="fail",
                        tx_hash=((order.custom_params or {}).get("onchain_pending") or {}).get("tx_hash", "") if order is not None else "",
                        chain=((order.custom_params or {}).get("onchain_pending") or {}).get("chain", "") if order is not None else "",
                    )
                    return None
            return (order, qty)
        finally:
            if home and aid and home != aid:
                try:
                    self._wallet_switch(home)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(f"TD RESTORE ERR | symbol={self.symbol} {exc}")

    # ── CEX 通道（Step 1，2026-08-17）──────────────────────────────
    # 与 DEX 真分账对称：批次状态机不变（slot 可用性/台账/平仓顺序），只换
    # 「钱在哪、怎么下单」——子账号独立 key，无 wallet_switch；pv_slot = 子
    # 账号总资产（USDT + 持仓×Gate 价）；quote = USDT。

    def _is_cex(self) -> bool:
        """执行通道大类：cex=Gate 交易所子账号；dex=链上子钱包（默认）。"""
        return self.parameters.get("channel_family") == "cex"

    def _cex_slot_broker(self, slot: dict) -> Any:
        """slot → 子账号 CexBroker（子账号 key 签名；缓存，避免每轮重建）。"""
        slot_no = int(slot["slot"])
        broker = self._cex_brokers.get(slot_no)
        if broker is None:
            from nanobot_quant.brokers.cex_broker import CexBroker
            from nanobot_quant.gate_credentials import load_slot_map
            name = load_slot_map().get(str(slot_no)) or f"gate_bot{slot_no}"
            broker = CexBroker(
                tokens_json=self.parameters.get("tokens_json") or [],
                slippage=str(self.parameters.get("slippage", "0.01")),
                sub_account=name,
            )
            self._cex_brokers[slot_no] = broker
        return broker

    def _cex_slot_balances(self, slot: dict) -> dict:
        """子账号 spot 余额 {CURRENCY: {available, locked}}；失败返回 {}。"""
        try:
            return self._cex_slot_broker(slot)._balances()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                f"TD CEX BAL ERR | symbol={self.symbol} slot={slot['slot']} {exc}"
            )
            return {}

    def _cex_slot_quote_balance(self, slot: dict, quote: str = "USDT") -> float:
        """子账号 quote 币种可用+锁定余额。"""
        bal = self._cex_slot_balances(slot).get(quote) or {}
        return float(bal.get("available") or 0) + float(bal.get("locked") or 0)

    def _cex_slot_token_balance(self, slot: dict, symbol: str) -> float:
        """子账号该标的持仓量（tokens.json gate_symbol 优先，回退 symbol）。"""
        token = next(
            (
                t
                for t in (self.parameters.get("tokens_json") or [])
                if str(t.get("symbol") or "").upper() == str(symbol).upper()
            ),
            None,
        )
        key = str((token or {}).get("gate_symbol") or symbol).upper()
        bal = self._cex_slot_balances(slot).get(key) or {}
        return float(bal.get("available") or 0) + float(bal.get("locked") or 0)

    def _cex_slot_portfolio_value(self, slot: dict) -> float:
        """子账号总资产 USD：USDT + Σ(持仓 × Gate 价)；失败返回 0（fail-closed）。"""
        try:
            balances = self._cex_slot_balances(slot)
            total = 0.0
            for cur, b in balances.items():
                avail = float(b.get("available") or 0) + float(b.get("locked") or 0)
                if avail <= 0:
                    continue
                if cur == "USDT":
                    total += avail
                else:
                    try:
                        px = self._cex_price_of(cur)
                    except Exception:  # noqa: BLE001
                        px = 0.0
                    total += avail * px
            return total
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                f"TD CEX PV ERR | symbol={self.symbol} slot={slot['slot']} {exc}"
            )
            return 0.0

    def _cex_price_of(self, currency: str) -> float:
        """Gate 计价（子账号持仓估值用）：gate_cex 优先，okx_cex 兜底。"""
        try:
            from nanobot_quant.data_sources import get_data_source
            px = get_data_source("gate_cex").get_price(currency)
            if px and px > 0:
                return float(px)
            px = get_data_source("okx_cex").get_price(currency)
            return float(px) if px and px > 0 else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _cex_submit(self, slot: dict, req) -> Any:
        """子账号 broker 下单（绕过策略主 broker——子账号必须用自己的 key）。"""
        return self._cex_slot_broker(slot).submit_order(
            self.create_order(req.asset, req.quantity, req.action)
        )

    def _buy_on_slot_cex(self, slot: dict, price: float, reason: str):
        """CEX 通道 BUY（Step 1）：子账号独立 key，无 wallet_switch；

        pv_slot = 子账号总资产（USDT + 持仓×Gate 价）→ min_account_value
        → qty（fixed/value）→ position_limit（pv_slot 基准）→ USDT 资金
        检查 → 子账号 broker 下单。返回 (order, qty) 或 None（跳下一 slot）。
        """
        try:
            pv_slot = self._cex_slot_portfolio_value(slot)
            if pv_slot <= 0:
                self.logger.warning(
                    f"TD SLOT SKIP | symbol={self.symbol} slot={slot['slot']} 余额查询失败/为零"
                )
                return None
            min_v = float(self.parameters.get("min_account_value", 0) or 0)
            if min_v > 0 and pv_slot < min_v:
                self.logger.warning(
                    f"TD SLOT SKIP (min_account_value) | symbol={self.symbol} slot={slot['slot']} "
                    f"pv=${pv_slot:.2f} < ${min_v:.2f}"
                )
                return None
            if self.quantity_mode == "value":
                qty = pv_slot * self._risk.max_position_pct / price if price > 0 else 0.0
            elif self.quantity_mode == "fixed_amount":
                qty = self.fixed_amount / price if price > 0 else 0.0
            else:
                qty = float(self.quantity or 0)
            if qty <= 0:
                return None
            if self.quantity_mode != "fixed_amount":
                result = self._risk.can_enter(
                    position_value=qty * price,
                    portfolio_value=pv_slot,
                    peak_portfolio=pv_slot,
                )
                if not result.approved:
                    print(
                        f"[TD] BLOCK ({result.check_name}) | symbol={self.symbol} slot={slot['slot']} "
                        f"pos=${qty * price:.2f} > "
                        f"{self._risk.max_position_pct * 100:.0f}% of slot pv=${pv_slot:.2f}",
                        file=sys.stderr, flush=True,
                    )
                    return None
            bal = self._cex_slot_quote_balance(slot, "USDT")
            needed = qty * price
            if bal < needed:
                self.logger.warning(
                    f"TD SLOT SKIP | symbol={self.symbol} slot={slot['slot']} 资金不足 "
                    f"({bal:.4f} < {needed:.4f} USDT)"
                )
                return None
            req = self._portfolio.build_buy_order(
                self.symbol, price, reason, quantity=qty,
            )
            order = self._cex_submit(slot, req)
            if order is None or _order_error(order):
                err = _order_error(order) or "order is None"
                self.logger.info(
                    f"TD BATCH BUY FAIL | symbol={self.symbol} slot={slot['slot']} "
                    f"price={price:.2f} qty={qty} {err}"
                )
                self._record(
                    "BUY_FAIL", f"slot={slot['slot']} {err}",
                    slot=slot["slot"], qty=qty, price=price,
                    direction="buy", status="fail",
                )
                return None
            return (order, qty)
        finally:
            pass  # CEX 无 switch/还原

    def _sell_lot_cex(self, slot: dict, price: float, signal: dict, exit_reason: str) -> None:
        """CEX 通道 SELL（Step 1）：子账号 key 查余额/下单；min_hold=0；

        filled → close_lot；pending（5s 未 closed）→ _pending_sells（台账
        保持 open，Step 2 补确认）；error → EXIT_FAIL（台账 open 可重试）。
        余额为 0 → 幽灵批次释放台账（与 DEX 对称）。
        """
        if slot["slot"] in self._pending_sells:
            return
        lot = self.batch_manager.get_lot(slot["slot"])
        if lot is None:
            return
        qty = float(lot["qty"])
        try:
            bal = self._cex_slot_token_balance(slot, self.symbol)
            if bal <= 0:
                # 子账号无持仓 → 幽灵批次，释放台账
                self.batch_manager.close_lot(slot["slot"])
                self.batch_manager.save()
                self.logger.warning(
                    f"TD BATCH EXIT SKIP | symbol={self.symbol} slot={slot['slot']} "
                    f"子账号无持仓（台账 {qty} 已释放）"
                )
                self._record("EXIT_SKIP", f"slot={slot['slot']} 子账号无持仓")
                return
            if bal < qty:
                qty = bal
                self.logger.warning(
                    f"TD BATCH EXIT SHRINK | symbol={self.symbol} slot={slot['slot']} "
                    f"台账 {lot['qty']} 子账号 {bal:.6f} → 缩量卖出 {qty:.6f}"
                )
                self._record(
                    "EXIT_SHRINK",
                    f"slot={slot['slot']} 台账 {lot['qty']:.6g} 子账号 {bal:.6f} → 缩量",
                )
            req = self._portfolio.build_sell_order(
                self.symbol, price, exit_reason,
                quantity=qty,
            )
            order = self._cex_submit(slot, req)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                f"TD BATCH EXIT FAIL | symbol={self.symbol} slot={slot['slot']} {exc}"
            )
            self._record(
                "EXIT_FAIL", f"slot={slot['slot']} {exc}",
                slot=slot["slot"], qty=qty, price=price,
                direction="sell", status="fail",
            )
            return
        if order is not None and not _order_error(order):
            if order.is_filled():
                self.batch_manager.close_lot(slot["slot"])
                self.batch_manager.save()
                self.tracker.track(
                    order_id=order.identifier,
                    symbol=self.symbol,
                    action="sell",
                    quantity=qty,
                    tag=f"signal:td-sell:{exit_reason}",
                    signal=signal,
                    reason=exit_reason,
                )
                self.logger.info(
                    f"TD BATCH EXIT | symbol={self.symbol} slot={slot['slot']} price={price:.2f} "
                    f"qty={qty} {exit_reason}"
                )
                self._record(
                    "EXIT",
                    f"slot={slot['slot']} {exit_reason} qty={qty:.6g} price={price:.2f}",
                    slot=slot["slot"], qty=qty, price=price,
                    direction="sell", status="ok",
                )
                return
            # pending（5s 未 closed）→ 台账保持 open + pending 记录（Step 2 补确认）
            self._pending_sells[slot["slot"]] = {
                "slot": slot["slot"],
                "order_id": order.identifier,
                "qty": qty,
                "price": price,
                "exit_reason": exit_reason,
                "account_id": slot.get("account_id", ""),
                "symbol": self.symbol,
                "cex": True,
            }
            self.logger.info(
                f"TD BATCH EXIT PENDING | symbol={self.symbol} slot={slot['slot']} price={price:.2f} "
                f"qty={qty} {exit_reason}"
            )
            self._record(
                "EXIT_PENDING",
                f"slot={slot['slot']} {exit_reason} qty={qty:.6g} price={price:.2f}",
                slot=slot["slot"], qty=qty, price=price,
                direction="sell", status="pending",
            )
            return
        err = _order_error(order) or "order is None"
        self.logger.warning(
            f"TD BATCH EXIT FAIL | symbol={self.symbol} slot={slot['slot']} price={price:.2f} "
            f"qty={qty} {exit_reason} error={err}"
        )
        self._record(
            "EXIT_FAIL", f"slot={slot['slot']} {exit_reason} {err}",
            slot=slot["slot"], qty=qty, price=price,
            direction="sell", status="fail",
        )

    def _slot_portfolio_value(self) -> float:
        """当前活跃（=目标 slot）子钱包总资产 USD；失败返回 0（fail-closed 跳过）。

        真分账 v1.1 拍板（2026-08-10，B 方案）：position_limit 与数量比例
        以目标 slot 子钱包资产为基准——每批独立风控，避免随活跃账户漂移。
        """
        try:
            from nanobot_quant.onchainos_cli import get_wallet_balance
            assets = get_wallet_balance() or []
            total = sum(float(a.get("usdValue") or 0) for a in assets)
            return total
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"TD PV ERR | {exc}")
            return 0.0

    # ── lumibot lifecycle hooks (delegated to tracker) ──

    def on_new_order(self, order):
        """Called by lumibot when a new order is created."""
        super().on_new_order(order)
        asset = order.asset if hasattr(order, 'asset') else getattr(order, 'symbol', '?')
        self.tracker.track(
            order_id=order.identifier,
            symbol=str(asset),
            action=str(order.side),
            quantity=int(order.quantity),
            status=str(getattr(order, 'status', 'new')),
        )

    def on_filled_order(self, position, order, price, quantity, multiplier):
        """Called by lumibot when an order fills."""
        super().on_filled_order(position, order, price, quantity, multiplier)
        self.tracker.on_fill(
            order_id=order.identifier,
            filled_quantity=int(quantity),
            filled_price=float(price),
        )

    def on_canceled_order(self, order):
        """Called by lumibot when an order is cancelled."""
        super().on_canceled_order(order)
        self.tracker.on_cancel(order_id=order.identifier)
    def _actual_price_from_st(self, st) -> float | None:
        """从 swap_status 确认数据提取 input/output → 稳定币规则实际成交价。

        2026-08-13 方案 B：交易恒以稳定币计价（broker quote=USDC）——
        找 input/output 里的稳定币作分子、另一侧数量作分母。无/歧义 → None。
        """
        try:
            from nanobot_quant import td_live_state
            _raw = (st or {}).get("raw") or {}
            _data = _raw.get("data")
            _d0 = _data[0] if isinstance(_data, list) and _data else (
                _data if isinstance(_data, dict) else None
            )
            return td_live_state.compute_actual_price(_d0)
        except Exception:  # noqa: BLE001
            return None

    def _confirmed_tx_hash(self, info: dict, st) -> str:
        """确认路径的真实 tx_hash（2026-08-11 拍板：只做 detail 提取，
        不做额外补查——保持简单）。

        取 detail 响应的 data[0].txHash（非占位 UUID 直接用，SELL 确认
        场景零额外调用）；占位 UUID/查询失败返回空（事件显示「—」，
        不阻塞确认）。
        """
        from nanobot_quant.onchainos_cli import is_placeholder_tx_hash
        tx_hash = str(info.get("tx_hash") or "")
        raw = (st or {}).get("raw") or {}
        data = raw.get("data")
        d0 = data[0] if isinstance(data, list) and data else (
            data if isinstance(data, dict) else None
        )
        # DIAG（2026-08-12）：把 detail 全量打出来——字段名 + 值都看，
        # 确认 hash 到底叫 txHash 还是别的名字（不假设字段名）。
        try:
            detail_tx = str(d0.get("txHash") or "") if isinstance(d0, dict) else ""
            if isinstance(d0, dict):
                d0_keys = ",".join(d0.keys())
                hash_like = {
                    k: str(v)[:40] for k, v in d0.items()
                    if any(w in k.lower() for w in ("hash", "tx", "order", "id"))
                }
                d0_json = repr(d0)
            else:
                d0_keys = "-"; hash_like = {}; d0_json = "-"
            self.logger.info(
                "TD CONFIRM DETAIL | slot=%s symbol=%s status=%s in_hash=%s order_id=%s "
                "data_len=%s keys=[%s] hash_like=%s d0=%s",
                info.get("slot"), info.get("symbol", self.symbol),
                (st or {}).get("tx_status"),
                tx_hash[:14] or "-", str(info.get("order_id") or "")[:14] or "-",
                len(data) if isinstance(data, list) else -1,
                d0_keys, hash_like, d0_json,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            if isinstance(d0, dict) and d0.get("txHash"):
                real = str(d0["txHash"])
                if real and not is_placeholder_tx_hash(real):
                    return real
        except Exception:  # noqa: BLE001
            pass
        return tx_hash if not is_placeholder_tx_hash(tx_hash) else ""
