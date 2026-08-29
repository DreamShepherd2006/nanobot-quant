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

import functools
import pandas as pd
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from lumibot.strategies.strategy import Strategy

from nanobot_quant.order_tracker import OrderTracker
from nanobot_quant.portfolio import PortfolioEngine
from nanobot_quant.risk import RiskEngine
from nanobot_quant.strategies.td_sequential import calculate
from nanobot_quant.data_sources.periods import INTERVAL_SECONDS
from nanobot_quant.td_params import DEFAULT_TD_PARAMS


def _parse_sleeptime_seconds(value: str) -> int:
    """场景周期字符串 → 秒（16 周期全量，2026-08-24 方案 C）。"""
    return INTERVAL_SECONDS.get(str(value).strip(), 60)


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
        "sell_only_profit_high": 0.0,   # 高9 毛浮盈门上档（0=关闭无条件卖；≥此值无条件卖，2026-08-25 → 双档 08-29）
        "sell_only_profit_low": 0.002,  # 高9 毛浮盈门下档（<此值死扛；[下,上) 由动能判断）
        "momentum_exit": True,          # 高9 动能判断开关（true=拍板主方案；false=回退上门简单门，方案 B）
        "cd_stall_n": 3,                # 动能停滞阈值：cd_sell 连续 N 根无 +1 判定动能弱（下门落袋）
        "cd_exit_min_profit": 0.0,  # cd 13 通道保本门（≥此值卖；<死扛；0=不亏本金就走，2026-08-27）
        "cd_exit_all": True,        # cd 13 通道全平（true=一次清所有过门批次；false=每轮一个，2026-08-27）
        "td_sell_all": False,       # 高9 一次平所有盈利批（false=每轮一个）
        "min_hold_bars": 10,        # 买入后 N 根 bar 内 TD SELL（高9/cd13）不触发（0=关闭；止损/止盈不受限，2026-08-28）
        **DEFAULT_TD_PARAMS,
    }

    #: sleeptime → get_historical_prices timestep（精确粒度，S3a 多场景：
    #   mid=5m/15m 必须传 5min/15min 而非笼统 minute，否则数据源
    #   _BAR_MAP 把粒度丢失成 1m——K 线窗口与场景周期不匹配）。
    #   旧 8 周期保持 lumibot 风格（minute/5min/…，回测兼容）；新周期
    #   （3m/2H/6H/8H/12H/3D/7D/30D）lumibot 无对应解析器，直通统一
    #   周期名，live 走 ``bar:`` 前缀直拉、回测 replay 动态映射（方案 C）。
    _TIMESTEP_BY_SLEEPTIME = {
        "1m": "minute", "5m": "5min", "15m": "15min", "30m": "30min",
        "1H": "hour", "4H": "4hour", "1D": "day", "1W": "week",
        # 新周期：统一周期名直通（lumibot 风格无对应解析）
        "3m": "3m", "2H": "2H", "6H": "6H", "8H": "8H",
        "12H": "12H", "3D": "3D", "7D": "7D", "30D": "30D",
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
        self._min_hold_bars = int(self.parameters.get("min_hold_bars", 10) or 0)
        self._min_hold_until: dict[tuple, pd.Timestamp] = {}
        self._current_bar_time: pd.Timestamp | None = None
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
        # 2026-08-25 高9 出场：毛浮盈门（双档 2026-08-29）+ 动能判断 + 全平开关（场景级覆盖见 _activate_scene）
        self._sell_only_profit_high = float(
            self.parameters.get("sell_only_profit_high", 0.0) or 0.0
        )
        self._sell_only_profit_low = float(
            self.parameters.get("sell_only_profit_low", 0.002)
            if self.parameters.get("sell_only_profit_low") is not None else 0.002
        )
        self._momentum_exit = bool(
            self.parameters.get("momentum_exit", True)
        )
        self._cd_stall_n = max(
            int(self.parameters.get("cd_stall_n", 3) or 3), 1
        )
        # 动能停滞状态（per-symbol，2026-08-29）：
        #   {symbol: {"prev": cd_sell, "stall": 连续无 +1 根数}}
        self._cd_stall_state: dict[str, dict] = {}
        self._cd_exit_min_profit = float(
            self.parameters.get("cd_exit_min_profit", 0.0) or 0.0
        )
        self._cd_exit_all = bool(
            self.parameters.get("cd_exit_all", True)
        )
        self._td_sell_all = bool(
            self.parameters.get("td_sell_all", False) or False
        )
        # 单边交易成本（Gate taker 0.1%）：止损/止盈净值口径
        self._fee_rate = float(self.parameters.get("fee_rate", 0.001) or 0.0)
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
        # key = "{slot_no}:{account}"（2026-08-23：含 account，场景化后同一
        # slot_no 在不同场景池指向不同子账号，如 high slot1=gate_bot1 vs
        # mid slot1=gate_bot3，复用会串池）。
        self._cex_brokers: dict[str, Any] = {}

        # TD algorithm params (subset of the strategy parameters dict)
        self._td_params = {
            k: self.parameters.get(k, v)
            for k, v in DEFAULT_TD_PARAMS.items()
        }
        # 当前所属场景（页面场景化 B1，2026-08-21）：_activate_scene 设置；
        # 非场景模式/回测保持空串，事件归入 default 场景。
        self._current_scene = ""

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

    def _track_iteration(fn):
        """包裹 on_trading_iteration（2026-08-21 延迟停止方案）。

        - 维护 ``self._iteration_active``：当前轮业务是否正在执行
          （try/finally 保证异常也复位）——td_live.stop() 据此判断
          「当前轮是否已自然结束」，结束后才执行停止。
        - 开头检查 td_live_state.stop_requested：停止后主循环 break 前
          lumibot 可能重建 scheduler，新 scheduler 的孤儿 job 会在
          interval 后再次调 on_trading_iteration——置位后直接 return，
          防止停止后空跑/误下单。
        """
        @functools.wraps(fn)
        def wrapper(self, *a, **kw):
            from nanobot_quant import td_live_state

            if td_live_state.stop_requested.is_set():
                return None
            self._iteration_active = True
            try:
                return fn(self, *a, **kw)
            finally:
                self._iteration_active = False

        return wrapper

    @_track_iteration
    def on_trading_iteration(self):
        """Called for each bar (trading day) during the backtest.

        Fetches all available historical bars up to the current bar,
        calls ``calculate()`` for the latest TD Sequential signal,
        then creates buy/sell orders based on the rules above.

        标的池模式：按池子顺序（=优先级）逐标的评估，谁 Setup 9 谁执行；
        同 bar 多标的命中按顺序全部处理（资金天然隔离）。

        S3a 场景调度（2026-08-20）：td_live 注入 ``_scene_runtimes`` 时按
        场景到期调度（每场景独立 sleeptime/symbols/broker/批次台账，主循环
        心跳=最小场景周期）；回测/纸交易（无场景运行时）保持旧逻辑。

        共振错峰（2026-08-26）：每轮（心跳）重置全局建仓额度
        （_round_buy_used）——每轮每场景只建 1 笔，共振多标的错轮建仓。
        波结束判定在 _activate_scene 开头（回测 driver 每 bar 亦调用，
        语义与实盘自动一致）。
        """
        # ── 链上补确认（2026-08-11）────────────────────────────────
        # 每轮迭代先处理 pending 卖出/买入的链上确认（SUCCESS 补台账、
        # ERROR/CANCELLED 记失败、PENDING 继续等），再评估新信号。
        self._check_pending_confirmations()

        # ── S3a 场景调度（2026-08-20）──────────────────────────────
        runtimes = getattr(self, "_scene_runtimes", None)
        if runtimes:
            now = datetime.now(timezone.utc)
            self._batch_managers = {}
            next_due = None
            for name in sorted(runtimes.keys()):
                rt = runtimes[name]
                if not rt.get("enabled"):
                    continue
                last = rt.get("last_run")
                due = (last + timedelta(seconds=_parse_sleeptime_seconds(
                    rt.get("sleeptime") or "1m"))) if last is not None else now
                if next_due is None or due < next_due:
                    next_due = due
                if last is not None and (
                    now - last
                ).total_seconds() < _parse_sleeptime_seconds(
                    rt.get("sleeptime") or "1m"
                ) - 1:
                    # 到期容差 1s：避免心跳边界抖动（lumibot 心跳与 wall clock
                    # 对齐误差）导致场景被误跳过——跳过轮不拉数据，下一轮
                    # 增量拉 2 根补上（kline_cache 多根判定已修复）。
                    continue
                rt["last_run"] = now
                self._activate_scene(name, rt)
                _t_start = time.monotonic()
                # 余额快照预取（2026-08-28 A2）：K 线之前——用户拍板「提取
                # 完 K 线紧接着计算，有信号马上买卖」——余额必须先就绪。
                # BUY 路径从快照取（零网络）；SELL/估值保持逐查。
                _t0 = time.monotonic()
                self._prefetch_slot_balances()
                _bal = time.monotonic() - _t0
                # ticker 并发预取（2026-08-28）：串行取价是每轮耗时大头，
                # 池标的 + GT 并发预取填 15s 类级缓存，本轮后续调用零网络。
                _t0 = time.monotonic()
                self._prefetch_ticker_prices()
                _ticker = time.monotonic() - _t0
                # K 线并发预取（2026-08-24）：只并发拉取 K 线（每标的
                # HTTP ~1.5-2s），TD 计算/下单/台账仍在主线程串行——
                # wallet switch 是全局状态，并发切换会互相踩。
                _t0 = time.monotonic()
                prefetched = self._prefetch_all_bars()
                _kline = time.monotonic() - _t0
                _t0 = time.monotonic()
                for sym in self.symbols:
                    self.symbol = sym
                    self.batch_manager = self.batch_managers.get(sym)
                    self._evaluate_symbol(prefetched.get(sym))
                self._write_positions_state()
                _calc = time.monotonic() - _t0
                # 余额快照失败信息写入 LIVE_STATE（实时监控信号区显示，
                # 不阻塞主循环；预取失败时 balances_error 已设）
                try:
                    from nanobot_quant import td_live_state
                    td_live_state.set_balances_error(
                        name, getattr(self, "_balances_error", None)
                    )
                except Exception:  # noqa: BLE001
                    pass
                print(
                    f"[TD] TIMING | scene={name} kline={_kline:.1f}s "
                    f"bal={_bal:.1f}s ticker={_ticker:.1f}s calc={_calc:.1f}s "
                    f"total={time.monotonic() - _t_start:.1f}s",
                    file=sys.stderr, flush=True,
                )
            try:
                from nanobot_quant import td_live_state
                td_live_state.set_next_due(
                    next_due.strftime("%Y-%m-%d %H:%M:%S UTC") if next_due else None)
            except Exception:  # noqa: BLE001
                pass
            return

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
        self._write_positions_state()

    def _write_positions_state(self) -> None:
        """把全部标的 open 批次摘要写入 LIVE_STATE（实时监控持仓小节）。

        2026-08-22 拍板：位置=场景卡片内小节；粒度=批次级（每 open 批
        一行，slot 区分）；价格口径=ticker 实时价（_cex_price_of：gate_cex
        优先、okx_cex 兜底；仅对含 open 批次的标的取价，无持仓不取价）；
        止损/止盈仍用 TD bar 收盘价（signal.price）不受影响。TD 未运行时
        页面回退读台账离线快照（无实时价）。展示层异常不阻塞主循环。
        """
        if not self.parameters.get("live_mode"):
            return  # 回测/纸交易不写 LIVE_STATE（2026-08-28：防回测污染实盘监控）
        try:
            from nanobot_quant import td_live_state

            bms = getattr(self, "batch_managers", None) or getattr(
                self, "_batch_managers", None
            ) or {}
            by_sym: dict[str, list[dict]] = {}
            for sym, bm in (bms or {}).items():
                open_lots = [s for s in bm.slots if s.get("lot") is not None]
                if not open_lots:
                    continue  # 无 open 批次不取价
                price = 0.0
                try:
                    price = self._cex_price_of(sym)
                except Exception:  # noqa: BLE001
                    price = 0.0
                rows = []
                # 显示阈值（<$X 不显示；0=全部）。2026-08-26 用户拍板：
                # 显示阈值独立于交易门槛（Gate min_quote）——<$3 但 ≥$1
                # 的仓位可显示；价值用实时价（取价失败时用成本价近似）。
                display_min = float(
                    getattr(self, "parameters", {}).get(
                        "position_display_min_usd", 0.0
                    ) or 0.0
                )
                for s in open_lots:
                    lot = s["lot"]
                    entry = float(lot.get("entry_price") or 0)
                    pnl = None
                    if price > 0 and entry > 0:
                        pnl = (price - entry) / entry
                    qty = float(lot.get("qty") or 0)
                    value = qty * (price if price > 0 else entry)
                    if display_min > 0 and value < display_min:
                        continue  # dust 批次不显示（台账仍在，卖出不受影响）
                    rows.append({
                        "symbol": sym,
                        "slot": s["slot"],
                        "account_id": s.get("account_id"),
                        "qty": lot.get("qty"),
                        "entry_price": entry,
                        "entry_time": lot.get("entry_time"),
                        "price": price or None,
                        "pnl_pct": pnl,
                    })
                by_sym[sym] = rows
            td_live_state.set_positions(self._current_scene or "default", by_sym)
            self._write_account_funds()
        except Exception:  # noqa: BLE001
            # 展示层：失败不阻塞策略主循环
            pass

    def _write_account_funds(self) -> None:
        """子账号资金写入 LIVE_STATE（实时监控资金小表）。

        2026-08-22 拍板：持仓小节下方显示全部 slot 资金——USDT 可用 +
        总资产（USDT 计，含持仓币×ticker 价）。仅 CEX（gate）通道实现：
        主 key 批量拉子账号余额（fetch_all_balances，每轮 1 次调用），按
        场景 sub_accounts 取本场景 slot 对应子账号；DEX 子钱包资金展示
        待补（docs/quant-system.md 记录）。展示层异常不阻塞主循环。
        """
        if not self.parameters.get("live_mode"):
            return  # 回测/纸交易不写 LIVE_STATE（2026-08-28）
        try:
            from nanobot_quant import td_live_state
            from nanobot_quant.exec_params import load_exec_params
            from nanobot_quant.gate_credentials import (
                fetch_all_balances,
                load_gate_credentials,
            )

            if self.parameters.get("channel_family") != "cex":
                return  # DEX 通道资金展示待补
            ep = load_exec_params() or {}
            sc_cfg = (ep.get("scenes") or {}).get(self._current_scene or "default") or {}
            accts = sc_cfg.get("sub_accounts") or []
            if not accts:
                return
            creds = load_gate_credentials()
            if not creds:
                return
            # 2026-08-28 A2：优先复用预取快照（_prefetch_slot_balances 已
            # 每轮 1 次批量拉，uid→balances）——不再重复 fetch；预取失败/
            # 未预取（execute_signal 直调）才回退独立拉取。
            sub_by_uid: dict = getattr(self, "_slot_balances", None) or {}
            if not sub_by_uid:
                balances = fetch_all_balances(creds)
                rows = balances.get("sub_accounts") or []
                if isinstance(rows, list):
                    for r in rows:
                        if isinstance(r, dict) and r.get("uid"):
                            sub_by_uid[str(r["uid"])] = r.get("balances") or {}
            subs_cfg = creds.get("sub_accounts") or {}
            funds: list[dict] = []
            for i, acct in enumerate(accts, start=1):
                uid = str((subs_cfg.get(acct) or {}).get("uid") or "")
                bal = sub_by_uid.get(uid, {})
                usdt = float((bal.get("USDT") or {}).get("available") or 0)
                total = 0.0
                for cur, v in bal.items():
                    amt = float(v.get("available") or 0) + float(v.get("locked") or 0)
                    if amt <= 0:
                        continue
                    if cur == "USDT":
                        total += amt
                    else:
                        try:
                            px = float(self._cex_price_of(cur) or 0)
                        except Exception:  # noqa: BLE001
                            px = 0.0
                        if px > 0:
                            total += amt * px
                funds.append({
                    "slot": i,
                    "account": acct,
                    "uid": uid,
                    "usdt_available": round(usdt, 6),
                    "total_asset": round(total, 6),
                })
            td_live_state.set_account_funds(self._current_scene or "default", funds)
        except Exception:  # noqa: BLE001
            pass  # 展示层：失败不阻塞策略主循环

    def _activate_scene(self, name: str, rt: dict) -> None:
        """S3a（2026-08-20）：激活场景运行时。

        td_live 为每个启用场景构造独立 broker（含独立数据源实例 →
        KlineCache per-scene 天然隔离）与批次台账（场景 sub_accounts →
        slot 映射），主循环（心跳=最小场景周期）按场景到期切换。回测/
        纸交易不调用。

        场景参数（sleeptime/symbols/数量模式/出场顺序/止盈/起扫槽位/最低
        资产）覆盖 initialize 快照的全局参数；broker 同步替换 self.broker
        与 executor.broker——lumibot v4.5.78 get_historical_prices/
        get_position/submit_order 均动态读 self.broker（源码确认），executor
        心跳循环读 executor.broker.should_continue() 亦动态，故切换安全。

        2026-08-21（B1）：记录当前场景名，供 _record/_evaluate_symbol
        写 LIVE_STATE 与事件时标记来源场景。

        2026-08-26（共振错峰）：场景激活时做波结束判定——该场景所有标的
        离开买9信号区（setup < entry_setup）→ 清额度 + 清 _wave_tried。
        额度跨轮保持（波锁），LINK 建仓后其他标的无论是否首次到 9 都被
        拦，等各自 setup 重置；回测 driver 每 bar 调用本方法，语义与实盘
        自动一致（不可把判定放 on_trading_iteration——回测不走该路径）。
        """
        self._current_scene = name
        # 共振错峰（2026-08-26 晚拍板）：波结束判定——上一轮该场景所有
        # 标的离开买9信号区 → 新波，清额度 + 清 _wave_tried。回测 driver
        # 每 bar 激活场景亦执行，与实盘语义一致。
        self._round_buy_used = getattr(self, "_round_buy_used", {})
        last_setup = getattr(self, "_last_setup", {})
        prev = last_setup.get(name, {})
        if not any(v >= self._td_params.get("entry_setup", 9)
                   for v in prev.values()):
            self._round_buy_used[name] = False
            self._wave_tried = {}
        if not hasattr(self, "_wave_tried"):
            self._wave_tried: dict[tuple, bool] = {}
        p = rt.get("params") or {}
        self.symbols = list(p.get("symbols") or [])
        self.quantity_mode = str(p.get("quantity_mode") or "fixed")
        self.fixed_amount = float(p.get("td_fixed_amount") or 10.0)
        qty = p.get("td_quantity")
        if qty is not None:
            self.quantity = qty
        self.sleeptime = str(p.get("sleeptime") or "1m")
        self._timestep = self._TIMESTEP_BY_SLEEPTIME.get(
            self.sleeptime, "day"
        )
        self._exit_order = str(p.get("exit_order") or "fifo")
        sl = p.get("stop_loss_pct")
        if sl is not None and getattr(self, "_risk", None) is not None:
            self._risk.stop_loss_pct = float(sl)
        self._take_profit_pct = float(p.get("take_profit_pct") or 0.0)
        self._sell_only_profit_high = float(p.get("sell_only_profit_high") or 0.0)
        self._sell_only_profit_low = float(
            p["sell_only_profit_low"]
            if p.get("sell_only_profit_low") is not None else 0.002
        )
        self._momentum_exit = bool(p.get("momentum_exit", True))
        self._cd_stall_n = max(int(p.get("cd_stall_n") or 3), 1)
        self._cd_exit_min_profit = float(p.get("cd_exit_min_profit") or 0.0)
        self._cd_exit_all = bool(p.get("cd_exit_all", True))
        self._td_sell_all = bool(p.get("td_sell_all") or False)
        mh = p.get("min_hold_bars")
        if mh is not None:
            self._min_hold_bars = int(mh)
        else:
            # 场景留空 → 回退全局（td_live/driver 构造 parameters 时已注入
            # 扁平 min_hold_bars；缺失走类默认 10）。2026-08-28：None 时
            # 必须重置，否则上一场景的值残留（多场景轮换共用同一策略对象）。
            self._min_hold_bars = int(self.parameters.get("min_hold_bars", 10) or 0)
        self._start_slot = int(p.get("td_start_slot") or 1)
        self._min_account_value = float(p.get("min_account_value") or 0)
        # S3b-2：场景级 TD 阈值（entry_setup/exit_setup/exit_countdown）。
        # 缺省 None → 保留全局 td_params（td_live 构造 parameters 时已 merge），
        # 非 None 覆盖 self._td_params（策略每 bar 读取处即此处覆盖生效）。
        for key in ("entry_setup", "entry_countdown", "exit_setup", "exit_countdown"):
            v = p.get(key)
            if v is not None:
                self._td_params[key] = int(v)
        self.broker = rt.get("broker") or self.broker
        self.batch_managers = rt.get("batch_managers") or {}
        self._batch_managers = self.batch_managers
        ex = getattr(self, "_executor", None)
        if ex is not None:
            try:
                ex.broker = self.broker
            except Exception:  # noqa: BLE001
                pass
        if not rt.get("_diag_shown"):
            rt["_diag_shown"] = True
            try:
                broker_name = self.broker.__class__.__name__
            except Exception:  # noqa: BLE001
                broker_name = "?"
            sl_diag = getattr(getattr(self, "_risk", None), "stop_loss_pct", "?")
            print(
                f"[DIAG] td_live 场景激活: {name} {self.sleeptime} "
                f"symbols={self.symbols} mode={self.quantity_mode} "
                f"stop_loss={sl_diag} "
                f"broker={broker_name}",
                file=sys.stderr, flush=True,
            )

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
            if self.parameters.get("live_mode"):
                # 仅实盘写 LIVE_STATE（2026-08-28：回测/纸交易不再污染实时监控——
                # 回测标的曾覆盖实盘监控的标的/信号/持仓显示）
                td_live_state.update_symbol(sym, {
                    **sig, "signal": event, "note": note,
                }, scene=getattr(self, "_current_scene", "") or "")
                td_live_state.append_event({
                    "symbol": sym, "event": event, "note": note,
                    "scene": getattr(self, "_current_scene", "") or "",
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

    def _prefetch_all_bars(self) -> dict:
        """并发预取全部标的 K 线（仅拉取并发，评估保持串行）。

        TD 循环每轮最耗时环节是交易所 K 线 HTTP 往返（每标的 ~1.5-2s，
        串行 12 标的 ≈ 30s+）。并发数由 exec_params ``kline_concurrency``
        控制（1=串行，默认 4；Gate 公共端点限流 200 次/10s/端点，余量
        >100 倍，标的池再大也需按池子大小收敛）。

        线程安全边界：只并发 ``get_historical_prices``（KlineCache 有锁、
        data_source 无共享可变状态）；worker 不触碰 self.symbol /
        batch_manager（这些由主线程在评估循环里设置）。

        返回 {symbol: (bars, fetch_len, drop_in_progress, exc)}；失败标的
        bars=None 并携带异常对象（黑名单/网络错误），评估循环统一处理。
        workers<=1 或单标的时返回 {}（走旧串行路径，零开销）。
        """
        workers = int(self.parameters.get("kline_concurrency") or 1)
        if workers <= 1 or len(self.symbols) <= 1:
            return {}
        from concurrent.futures import ThreadPoolExecutor, as_completed

        out: dict = {}
        with ThreadPoolExecutor(max_workers=min(workers, len(self.symbols))) as pool:
            fut_map = {
                pool.submit(self._fetch_bars_worker, sym): sym
                for sym in self.symbols
            }
            for fut in as_completed(fut_map):
                out[fut_map[fut]] = fut.result()
        return out

    def _fetch_bars_worker(self, sym: str) -> tuple:
        """单标的 K 线拉取（线程池 worker）。异常不外抛，包装进返回值。"""
        _drops = bool(self.parameters.get("drops_in_progress_bars", False))
        drop_in_progress = self._is_live_broker and not _drops
        fetch_len = self._min_history + (1 if drop_in_progress else 0)
        timestep = f"bar:{self._timestep}" if self._is_live_broker else self._timestep
        try:
            bars = self.get_historical_prices(
                sym, length=fetch_len, timestep=timestep
            )
            return (bars, fetch_len, drop_in_progress, None)
        except Exception as e:  # noqa: BLE001
            return (None, fetch_len, drop_in_progress, e)

    def _cur_scene(self) -> str:
        """当前场景名（未激活场景/纸交易/execute_signal 直调 = "default"）。"""
        return getattr(self, "_current_scene", "") or "default"

    def _evaluate_symbol(self, prefetched: tuple | None = None) -> None:
        """单标的评估（拉 K 线 → TD 计算 → 信号 → 真分账/常规下单）。

        prefetched=(bars, fetch_len, drop_in_progress, exc)：并发预取路径
        传入，跳过拉取；None 时内部拉取（串行/回测路径）。
        """
        if prefetched is not None:
            bars, fetch_len, drop_in_progress, exc = prefetched
            if bars is None:
                # 黑名单标的（Gate 无交易对/已下架，如 MU/VSC）静默跳过——
                # 首次失败已打印原因，不再每轮刷屏；重启 TD 循环重新探测
                try:
                    from nanobot_quant.gate_cex_data import blacklist_reason
                    if blacklist_reason(self.symbol):
                        return
                except ImportError:  # pragma: no cover
                    pass
                print(
                    f"[TD] DATA ERROR | symbol={self.symbol} "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr, flush=True,
                )
                return
            if bars.df.empty:
                print(
                    f"[TD] DATA EMPTY | symbol={self.symbol} bars is None or empty",
                    file=sys.stderr, flush=True,
                )
                return
            df = bars.df.copy()
            self._current_bar_time = self._bar_utc_ts(bars.df.index[-1])
        else:
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
                # live 直拉场景粒度：bar: 前缀让 lumibot 无法解析（_parse_timestep
                # 返回 None → 原样透传），数据源 removeprefix 后直拉原生 bar（如
                # 5m）——绕开 lumibot multi-timeframe 转换（length×multiplier 根
                # 1m + 自己 resample），live 与回测完全同源（2026-08-20 定稿）。
                # 回测（broker=None）保持标准 timestep（PandasDataBacktesting 只
                # 认 lumibot 标准名）。
                timestep = f"bar:{self._timestep}" if self._is_live_broker else self._timestep
                bars = self.get_historical_prices(
                    self.symbol,
                    length=fetch_len,
                    timestep=timestep,
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
            self._current_bar_time = self._bar_utc_ts(bars.df.index[-1])
        _got = len(df)
        if drop_in_progress and len(df) > 2:
            # 丢弃进行中的最后一根（live + 数据源未过滤；回测数据源全为已收盘 bar）
            df = df.iloc[:-1]
        # 诊断（2026-08-28 A1）：压缩为 2 行/标的——原 4-6 行 × 10 标的 =
        # 40-60 次 stderr flush/轮，HF 容器日志管道背压是 calc 段耗时大头
        # （TIMING 实测无信号轮 calc≈11s）。信息全保留：K 线契约（got/final/
        # min/drop）+ 尾 3 根 close（CDN 精度排查，逗号分隔一行）。
        print(
            f"[TD] BARS | symbol={self.symbol} got={_got} "
            f"final={len(df)} min={self._min_history} drop={int(drop_in_progress)}",
            file=sys.stderr, flush=True,
        )
        close_col = "close" if "close" in df.columns else (
            "Close" if "Close" in df.columns else None)
        if close_col:
            tail = ", ".join(
                f"{_ts}:{_row[close_col]}" for _ts, _row in df.tail(3).iterrows()
            )
            print(
                f"[TD] BARS | symbol={self.symbol} tail={tail}",
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
        cd_buy = signal.get("cd_buy", 0) or 0
        cd_sell = signal.get("cd_sell", 0) or 0
        score = signal.get("score", 0) or 0

        # 动能停滞状态更新（2026-08-29）：cd_sell 连续 N 根无 +1 → 动能弱，
        # 供高9 盈利门 [下, 上) 区间动能判断使用（与 BUY/SELL 分支顺序无关）。
        self._update_cd_stall(self.symbol, cd_sell)

        # ── 信号周期状态（2026-08-19 分批次建仓语义，per-symbol）──
        # 一个信号周期内同一标的只建一次仓：建仓后 setup 计数单调不减
        # （9→10→11→12）视为同周期，跳过 BUY；计数变小（12→8 / 9→1）
        # 标记 reset → 新周期允许再建。slot 平仓释放后同周期也不建
        # （信号级，用户 2026-08-19 拍板）。SELL 侧不受影响。
        if not hasattr(self, "_cycle_state"):
            self._cycle_state: dict[str, dict] = {}
        if not hasattr(self, "_denied_cycle"):
            # 共振错峰（2026-08-26）：被全局额度拦截的标的在本 setup
            # 周期内不再尝试建仓，setup 重置时清除（见下方 reset 分支）。
            # key=(scene, symbol)——同一 symbol 跨场景独立（用户拍板 per-scene）
            self._denied_cycle: dict[tuple, bool] = {}
        if not hasattr(self, "_wave_tried"):
            # 共振错峰波锁：本波（自上次无信号以来）该标的是否已到过买9
            # 信号区——首次到 9 时检查波内额度（round_used），非首次跳过额度
            # 竞争（被拒的等重置、已建仓的走周期门控）。波结束时清空。
            self._wave_tried: dict[tuple, bool] = {}
        if not hasattr(self, "_last_setup"):
            # 共振错峰波结束判定：每场景各标的上轮 setup_buy（信号区判定）
            self._last_setup: dict[str, dict[str, float]] = {}
        st = self._cycle_state.get(self.symbol)
        if st is None:
            # 首次见到该标的：有 open 仓位 → 视为本周期已建仓（保守
            # 不追——重启边界：setup 累加期重启会立即再建，此守卫避免）
            st = {"bought": False, "prev_setup": 0, "reset": False, "cd_triggered": False}
            _bm = (getattr(self, "_batch_managers", None) or {}).get(
                self.symbol
            ) or getattr(self, "batch_manager", None)
            if _bm is not None and _bm.any_open():
                st["bought"] = True
                st["cd_triggered"] = True  # 有 open 仓 → 本 countdown 周期已建仓
            self._cycle_state[self.symbol] = st
        if setup_buy < st["prev_setup"]:
            st["reset"] = True  # 计数变小 → 新信号周期
            # 共振错峰（2026-08-26）：新周期恢复被额度拦截标的的建仓资格
            if getattr(self, "_denied_cycle", None):
                self._denied_cycle.pop((self._cur_scene(), self.symbol), None)
        st["prev_setup"] = setup_buy
        # cd 周期门控（2026-08-27）：countdown 跨 setup 翻转持续累积（09:5x 启动 →
        # 10:14 完成 13），setup 翻转触发 reset 会让 cd_buy 13 绕过周期门控——同一
        # countdown 周期内 setup 9 已建仓 + cd 13 再补买（4 根 bar 内两次建仓）。
        # cd_triggered：本 countdown 周期已建仓（setup 或 cd 触发均置位）；
        # 清位条件：cd 计数归 0（countdown 完成/未启动）且 setup 翻转（新周期）——
        # 之后 setup 重新数到 9 才允许再次建仓，cd_buy 独立触发（setup 从未到 9）保留。
        if st["reset"] and cd_buy == 0:
            st["cd_triggered"] = False
        # 共振错峰波结束判定数据：记录本标的 setup（信号区判定用）
        self._last_setup.setdefault(self._cur_scene(), {})[self.symbol] = setup_buy
        price = signal.get("price", 0) or 0

        # ── 实时状态共享（td-table「实时监控」tab，2026-08-11）──
        # 无条件更新内存（同进程零成本）；信号动作由 _record 更新 signal。
        _last_ts = df.index[-1] if len(df) else None
        if _last_ts is not None:
            try:
                if getattr(_last_ts, "tzinfo", None) is not None:
                    _last_ts = _last_ts.tz_convert("UTC")
                else:
                    _last_ts = _last_ts.tz_localize("UTC")
                _time_s = _last_ts.strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception:  # noqa: BLE001
                _time_s = str(df.index[-1])
        else:
            _time_s = ""
        self._last_signal = {
            "setup_buy": setup_buy,
            "setup_sell": setup_sell,
            "cd_buy": signal.get("cd_buy", 0) or 0,
            "cd_sell": cd_sell,
            "score": score,
            "price": price,
            "time": _time_s,
        }
        try:
            if self.parameters.get("live_mode"):
                from nanobot_quant import td_live_state
                td_live_state.update_symbol(self.symbol, {
                    **self._last_signal, "signal": "HOLD",
                }, scene=getattr(self, "_current_scene", "") or "")
        except Exception:  # noqa: BLE001
            pass

        has_position = self.get_position(self.symbol) is not None

        # ── Update peak portfolio for drawdown tracking ──
        pv = self.portfolio_value
        if self._peak_portfolio is None or pv > self._peak_portfolio:
            self._peak_portfolio = pv

        entry_setup = int(self._td_params.get("entry_setup", 9))
        entry_countdown = int(self._td_params.get("entry_countdown", 13))
        exit_setup = int(self._td_params.get("exit_setup", 9))
        exit_countdown = int(self._td_params.get("exit_countdown", 13))
        score_threshold = float(self._td_params.get("score_threshold", 0.0))

        # 同周期已建仓 → 跳过 BUY（信号级：slot 平仓释放也不建，等 setup
        # 重置后再现新信号；只拦截 BUY，不 return——SELL/止损仍正常评估）
        buy_signal = (setup_buy >= entry_setup) or (cd_buy >= entry_countdown)
        # 周期门控（2026-08-27 扩展）：setup 周期（bought+未重置）或 countdown
        # 周期（cd_triggered 且 cd_buy 仍在触发区——setup 翻转会让 cd 13 绕过
        # 旧门控，同一 countdown 周期内 setup 9 已建仓 + cd 13 补买被拦）
        cycle_blocked = (st["bought"] and not st["reset"]) or (
            st["cd_triggered"] and cd_buy >= entry_countdown
        )
        if (
            cycle_blocked
            and buy_signal
            and score > score_threshold
        ):
            print(
                f"[TD] BATCH WAIT | symbol={self.symbol} 同周期已建仓"
                f"（setup_buy={setup_buy} cd_buy={cd_buy} 未重置），等新信号周期",
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
        # 共振错峰（2026-08-26 用户拍板，晚修订）：波锁——LINK 建仓后，
        # 其他标的（无论是否首次到 9）都被拦（denied），等各自 setup 重置
        # 后才能建仓。额度（_round_buy_used[scene]）跨轮保持，仅波结束
        # （该场景所有标的离开买9信号区）时清除；_wave_tried 标记本波
        # 已到过 9 的标的（首次到 9 才参与额度竞争，非首次跳过额度检查）。
        scene = self._cur_scene()
        round_used = bool(getattr(self, "_round_buy_used", {}).get(scene, False))
        denied = bool(getattr(self, "_denied_cycle", {}).get((scene, self.symbol), False))
        wave_first = not bool(getattr(self, "_wave_tried", {}).get((scene, self.symbol), False))
        if denied:
            # 本周期已被全局额度拦过：setup 重置前不再尝试（等新周期）
            print(
                f"[TD] BATCH WAIT | symbol={self.symbol} 本买9周期已错过"
                f"（等 setup 重置后新周期）",
                file=sys.stderr, flush=True,
            )
            self._record(
                "WAIT", "本买9周期已错过（等重置后新周期）", symbol=self.symbol
            )
        elif (
            batch_mode
            and round_used
            and wave_first
            and buy_signal
            and score > score_threshold
            and can_buy
            and not (cycle_blocked)
        ):
            print(
                f"[TD] BATCH WAIT | symbol={self.symbol} 本轮已建仓"
                f"（全局每轮 1 笔；等 setup 重置后新周期）",
                file=sys.stderr, flush=True,
            )
            self._denied_cycle[(scene, self.symbol)] = True
            self._wave_tried[(scene, self.symbol)] = True
            self._record(
                "WAIT", "本轮已建仓（全局每轮 1 笔；等重置后新周期）",
                symbol=self.symbol,
            )
        if (
            buy_signal
            and score > score_threshold
            and can_buy
            and not (batch_mode and round_used and wave_first)
            and not denied
            and (not tdst_filter or (support is not None and price > support))
            # 信号周期门控（2026-08-19 分批次建仓）：同周期已建仓（bought 且
            # 未重置，或 cd_triggered 且 cd_buy 仍在触发区——cd 13 补买拦截）
            # → 不 BUY；reset=True（计数变小）→ 新周期允许
            and not (cycle_blocked)
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
            reason = f"TD LONG setup_buy={setup_buy} cd_buy={cd_buy} score={score:.1f}"
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
                        # 链上已确认成交 → 建仓。CEX 用实际成交 filled（Gate
                        # amount_precision 截断后 filled < 计划量，2026-08-26
                        # 实测 ETH 0.00126091→0.0012）——台账记实际到账，
                        # 否则账实差 → 卖出缩量 → min_quote 卡 slot。
                        lot_qty = self._cex_filled(order) or qty
                        self.batch_manager.open_lot(
                            slot=slot["slot"], qty=lot_qty, entry_price=price,
                        )
                        # 最短持有期（2026-08-28）：买入后 min_hold_bars 根 bar 内
                        # TD SELL（高9/cd13）不触发——避免震荡市双向 countdown
                        # 刚买就卖；止损/止盈在 _handle_batch_exits 前检查不受限。
                        self._note_min_hold(self.symbol, slot["slot"])
                        # 交易状态变更立即落盘（重启不丢台账）
                        self.batch_manager.save()
                        self.logger.info(
                            f"TD BATCH LONG | symbol={self.symbol} slot={slot['slot']} "
                            f"price={price:.2f} qty={lot_qty} "
                            f"setup_buy={setup_buy} cd_buy={cd_buy} score={score:.1f}"
                        )
                        self._record(
                            "LONG",
                            f"slot={slot['slot']} qty={lot_qty:.6g} price={price:.2f}",
                            slot=slot["slot"], qty=lot_qty, price=price,
                            direction="buy", status="ok",
                            actual_price=self._cex_avg_price(order),
                            tx_hash=((order.custom_params or {}).get("onchain_pending") or {}).get("tx_hash", ""),
                            chain=((order.custom_params or {}).get("onchain_pending") or {}).get("chain", ""),
                        )
                        executed = True
                        st["bought"] = True
                        st["reset"] = False
                        st["cd_triggered"] = True  # 本 countdown 周期已建仓（cd 13 补买拦截）
                        # 共振错峰：本轮额度已用 + 本波已到过 9
                        self._round_buy_used = getattr(self, "_round_buy_used", {})
                        self._round_buy_used[self._cur_scene()] = True
                        self._wave_tried = getattr(self, "_wave_tried", {})
                        self._wave_tried[(self._cur_scene(), self.symbol)] = True
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
                    st["cd_triggered"] = True  # 本 countdown 周期已建仓（cd 13 补买拦截）
                    # 共振错峰：本轮额度已用 + 本波已到过 9
                    self._round_buy_used = getattr(self, "_round_buy_used", {})
                    self._round_buy_used[self._cur_scene()] = True
                    self._wave_tried = getattr(self, "_wave_tried", {})
                    self._wave_tried[(self._cur_scene(), self.symbol)] = True
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
                st["cd_triggered"] = True  # 本 countdown 周期已建仓（cd 13 补买拦截）
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
            fee_rate=getattr(self, "_fee_rate", 0.001),
        )
        for s in hits:
            self._sell_lot(s, price, signal, s.pop("_exit_reason", "exit"))
        # 2a) 高9 通道（2026-08-25 高9 出场逻辑；2026-08-29 双档+动能判断）：
        #     setup_sell ≥ exit_setup。卖出门双档：
        #     pnl ≥ high 无条件卖；pnl < low 死扛；[low, high) 动能判断
        #     （momentum_exit=true：强动能持有至 high、停滞弱动能 low 落袋、未知持有）。
        #     td_sell_all=true 一次平掉所有通过盈利门的批；false 每轮只平一个（现有）。
        if setup_sell >= exit_setup:
            reason = f"setup_sell={setup_sell}"
            gate_desc = (
                f"high={self._sell_only_profit_high} low={self._sell_only_profit_low}"
                f" momentum={self._momentum_exit} stall_n={self._cd_stall_n}"
            )
            if self._td_sell_all:
                candidates = bm.pick_all_slots(self._exit_order)
                sold_any = False
                hold_skipped = 0
                for s in candidates:
                    if self._min_hold_blocked(s):
                        hold_skipped += 1
                        continue
                    if self._profit_gate_blocked(s, price, setup_sell, cd_sell):
                        continue
                    self._sell_lot(s, price, signal, reason)
                    sold_any = True
                if not sold_any:
                    self.logger.info(
                        f"TD SELL SKIP | symbol={self.symbol} 无满足盈利门的批次"
                        f"（{gate_desc}，open={len(candidates)}"
                        + (f"，min_hold 跳过 {hold_skipped}" if hold_skipped else "")
                        + "）"
                    )
            else:
                s = bm.pick_exit_slot(self._exit_order)
                if s is not None:
                    if self._min_hold_blocked(s):
                        self.logger.info(
                            f"TD SELL SKIP | symbol={self.symbol} min_hold"
                            f"（剩余 {self._min_hold_remaining(s)} bar < {self._min_hold_bars}）"
                        )
                    elif self._profit_gate_blocked(s, price, setup_sell, cd_sell):
                        lot = s["lot"]
                        pnl = (price - lot["entry_price"]) / lot["entry_price"]
                        self.logger.info(
                            f"TD SELL SKIP | symbol={self.symbol} 浮盈不足"
                            f"（pnl={pnl:.4f} < {gate_desc}）"
                        )
                    else:
                        self._sell_lot(s, price, signal, reason)
                else:
                    # 可观测性：无仓卖 9 显式提示，区分「信号未出现」与
                    # 「信号出现但无 open 批次」（fail-closed，不做空）
                    self.logger.info(
                        f"TD SELL SKIP | symbol={self.symbol} 无 open 批次（setup_sell={setup_sell} "
                        f"cd_sell={cd_sell}）"
                    )
        # 2b) cd 13 通道（2026-08-27，Step 1；2026-08-28 改 elif 互斥）：cd_sell ≥ exit_countdown
        #     动能最终耗尽确认（DeMark 标准）→ 保本离场：毛浮盈 ≥ cd_exit_min_profit
        #     的批次平仓（默认 0 = 不亏本金就走，承担交易成本）；< 阈值死扛。
        #     cd_exit_all=true 一次平掉所有过门批次；false 每轮只平一个。
        #     互斥语义（高9 优先）：同 bar 高9 触发时（无论卖出或被门拦）cd13 不再评估
        #     ——高9 盈利门「让利润奔跑」不被 cd13 保本门绕过；cd13 仅在高9 未触发
        #     （setup 回落、cd 持续累积）时独立起作用。
        elif cd_sell >= exit_countdown:
            reason = f"cd_sell={cd_sell}"
            if self._cd_exit_all:
                candidates = bm.pick_all_slots(self._exit_order)
                sold_any = False
                hold_skipped = 0
                for s in candidates:
                    if self._min_hold_blocked(s):
                        hold_skipped += 1
                        continue
                    if self._cd_gate_blocked(s, price):
                        continue
                    self._sell_lot(s, price, signal, reason)
                    sold_any = True
                if not sold_any:
                    self.logger.info(
                        f"TD CD EXIT SKIP | symbol={self.symbol} 无 ≥{self._cd_exit_min_profit} "
                        f"浮盈批次（cd_sell={cd_sell}，open={len(candidates)}"
                        + (f"，min_hold 跳过 {hold_skipped}" if hold_skipped else "")
                        + "）"
                    )
            else:
                s = bm.pick_exit_slot(self._exit_order)
                if s is not None:
                    if self._min_hold_blocked(s):
                        self.logger.info(
                            f"TD CD EXIT SKIP | symbol={self.symbol} min_hold"
                            f"（剩余 {self._min_hold_remaining(s)} bar < {self._min_hold_bars}）"
                        )
                    elif self._cd_gate_blocked(s, price):
                        lot = s["lot"]
                        pnl = (price - lot["entry_price"]) / lot["entry_price"]
                        self.logger.info(
                            f"TD CD EXIT SKIP | symbol={self.symbol} 浮盈不足"
                            f"（pnl={pnl:.4f} < cd_exit_min_profit={self._cd_exit_min_profit}）"
                        )
                    else:
                        self._sell_lot(s, price, signal, reason)
                else:
                    self.logger.info(
                        f"TD CD EXIT SKIP | symbol={self.symbol} 无 open 批次（cd_sell={cd_sell}）"
                    )

    def _update_cd_stall(self, symbol: str, cd_sell: int) -> None:
        """动能停滞状态（2026-08-29）：cd_sell 连续 N 根无 +1 → 动能弱。

        cd_sell 是累积计数（非连续计数）：满足 +1 条件的 bar 才 +1，
        不满足的 bar 计数保留。本函数记录每个 symbol 的 cd_sell 连续
        未变化根数——变化（+1 / 完成 13 重置 0 / setup 翻转）→ 清零；
        数值不变（含 countdown 未启动期间恒 0）→ 递增。
        """
        prev = self._cd_stall_state.get(symbol, {}).get("prev", cd_sell)
        stall = self._cd_stall_state.get(symbol, {}).get("stall", 0)
        if cd_sell == prev:
            stall += 1
        else:
            stall = 0
        self._cd_stall_state[symbol] = {"prev": cd_sell, "stall": stall}

    def _cd_gate_blocked(self, slot: dict, price: float) -> bool:
        """cd 13 通道保本门（2026-08-27，Step 1）：毛浮盈 < cd_exit_min_profit 拦截。

        默认 0 = 保本离场（不亏本金，承担交易成本）；>0 时要求毛浮盈 ≥ 阈值才平仓。
        负浮盈（pnl<0）恒死扛——「反正不能亏」（用户拍板 2026-08-27）。
        """
        thr = getattr(self, "_cd_exit_min_profit", 0.0) or 0.0
        lot = slot.get("lot")
        if lot is None or not lot.get("entry_price"):
            print(
                f"[TD CD GATE] symbol={self.symbol} slot={slot.get('slot')} "
                f"entry={lot.get('entry_price') if lot else None!r} price={price} "
                f"thr={thr} -> 放行（entry 无效，fail-open）",
                file=sys.stderr, flush=True,
            )
            return False
        pnl = (price - lot["entry_price"]) / lot["entry_price"]
        blocked = pnl < thr
        print(
            f"[TD CD GATE] symbol={self.symbol} slot={slot.get('slot')} "
            f"entry={lot['entry_price']} price={price} pnl={pnl:.4f} thr={thr} "
            f"-> {'拦（死扛）' if blocked else '放行（卖）'}",
            file=sys.stderr, flush=True,
        )
        return blocked

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
                self._confirm_cex_sell(slot_id, info)
                continue
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
                self._confirm_cex_buy(slot_id, info)
                continue
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

    # ── TD CEX Step 2：CEX pending 订单轮询确认（2026-08-21）────────
    # Gate 市价单异步结算：下单只回 order id，需轮询查单（约 0.5s×10 内
    # 结算）。此前对 CEX pending 直接 continue（fail-safe 不 open_lot），
    # 已成交的 slot 永不建仓/释放。此处分 BUY/SELL 两个确认路径：
    #   filled → BUY: open_lot 补台账（回填 avg_price）；
    #           SELL: close_lot 释放 slot。
    #   cancelled/ioc 零成交 → BUY: 记 FAIL 释放 slot；
    #                         SELL: 记 FAIL，台账保持 open（下轮可重试卖）。
    #   open/submitted → 继续等；查询异常 → 保留 pending（fail-safe）。

    def _cex_confirm_broker(self, slot_id: int, info: dict | None = None) -> Any:
        """CEX pending 确认用的子账号 broker（缓存缺失时按 slot 重建）。

        2026-08-23：pending 记录带 account_id（下单时写入的场景池子账号），
        确认必须用它重建 broker——旧记录无 account_id 时回退全局 slot_map
        （_cex_slot_broker 内部 fallback），兼容无 account 字段的历史记录。
        """
        info = info or {}
        try:
            return self._cex_slot_broker(
                {"slot": slot_id, "account_id": str(info.get("account_id") or "")}
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                f"TD CEX CONFIRM ERR | slot={slot_id} 重建 broker 失败 {exc}"
            )
            return None

    def _confirm_cex_sell(self, slot_id: int, info: dict) -> None:
        """CEX 卖单确认：filled→close_lot；终态零成交→EXIT_FAIL 台账保持 open。"""
        symbol = info.get("symbol", self.symbol)
        order_id = str(info.get("order_id") or "")
        if not order_id:
            return
        try:
            broker = self._cex_confirm_broker(slot_id, info)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                f"TD PENDING CHECK ERR | slot={slot_id} symbol={symbol} CEX {exc}"
            )
            return
        if broker is None:
            return
        try:
            from nanobot_quant.gate_credentials import gate_pair
            pair = gate_pair(symbol, self.parameters.get("tokens_json") or [])
            status, filled, left, avg = broker._query_order(order_id, pair)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                f"TD PENDING CHECK ERR | slot={slot_id} symbol={symbol} CEX {exc}"
            )
            return
        if status == "filled" and filled > 0:
            bm = self._batch_managers.get(symbol) or self.batch_manager
            bm.close_lot(slot_id)
            bm.save()
            self.logger.info(
                f"TD BATCH EXIT (CEX CONFIRM) | symbol={symbol} slot={slot_id} "
                f"qty={info.get('qty', 0):.6g} avg={avg}"
            )
            self._record(
                "EXIT",
                f"slot={slot_id} CEX 确认平仓 qty={info.get('qty', 0):.6g}",
                symbol=symbol, slot=slot_id, qty=info.get("qty", 0),
                price=info.get("price", 0),
                actual_price=avg or None,
                direction="sell", status="ok",
            )
            del self._pending_sells[slot_id]
        elif status in ("cancelled", "failed"):
            self.logger.warning(
                f"TD BATCH EXIT FAIL | symbol={symbol} slot={slot_id} CEX 确认 {status} 零成交"
            )
            self._record(
                "EXIT_FAIL",
                f"slot={slot_id} CEX 确认 {status} 零成交",
                symbol=symbol, slot=slot_id, qty=info.get("qty", 0),
                price=info.get("price", 0),
                direction="sell", status="fail",
            )
            del self._pending_sells[slot_id]
        else:
            self.logger.info(
                f"TD PENDING CHECK | slot={slot_id} symbol={symbol} CEX {status} "
                f"filled={filled:.6g}"
            )

    def _confirm_cex_buy(self, slot_id: int, info: dict) -> None:
        """CEX 买单确认：filled→open_lot 补台账；终态零成交→BUY_FAIL 释放 slot。"""
        symbol = info.get("symbol", self.symbol)
        order_id = str(info.get("order_id") or "")
        if not order_id:
            return
        try:
            broker = self._cex_confirm_broker(slot_id, info)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                f"TD PENDING CHECK ERR | slot={slot_id} symbol={symbol} CEX {exc}"
            )
            return
        if broker is None:
            return
        try:
            from nanobot_quant.gate_credentials import gate_pair
            pair = gate_pair(symbol, self.parameters.get("tokens_json") or [])
            status, filled, left, avg = broker._query_order(order_id, pair)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                f"TD PENDING CHECK ERR | slot={slot_id} symbol={symbol} CEX {exc}"
            )
            return
        if status == "filled" and filled > 0:
            # 必须用实际成交 filled，不能用计划量（info.qty）——Gate 市价单
            # amount_precision 截断后 filled < 计划量（2026-08-26 实测 ETH
            # 0.00126091→0.0012），用计划量台账虚高 → 卖出缩量 → min_quote
            # 卡 slot。
            qty = float(filled)
            bm = self._batch_managers.get(symbol) or self.batch_manager
            bm.open_lot(slot=slot_id, qty=qty,
                        entry_price=float(info.get("price") or avg or 0))
            bm.save()
            self.logger.info(
                f"TD BATCH LONG (CEX CONFIRM) | symbol={symbol} slot={slot_id} "
                f"qty={qty:.6g} price={info.get('price', 0):.2f} avg={avg}"
            )
            self._record(
                "LONG",
                f"slot={slot_id} CEX 确认建仓 qty={qty:.6g}",
                symbol=symbol, slot=slot_id, qty=qty,
                price=info.get("price", 0),
                actual_price=avg or None,
                direction="buy", status="ok",
            )
            del self._pending_buys[slot_id]
        elif status in ("cancelled", "failed"):
            self.logger.warning(
                f"TD BATCH BUY FAIL | symbol={symbol} slot={slot_id} CEX 确认 {status} 零成交"
            )
            self._record(
                "BUY_FAIL",
                f"slot={slot_id} CEX 确认 {status} 零成交",
                symbol=symbol, slot=slot_id, qty=info.get("qty", 0),
                price=info.get("price", 0),
                direction="buy", status="fail",
            )
            del self._pending_buys[slot_id]
        else:
            self.logger.info(
                f"TD PENDING CHECK | slot={slot_id} symbol={symbol} CEX {status} "
                f"filled={filled:.6g}"
            )

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

    def _cex_avg_price(self, order) -> float | None:
        """CEX 实际成交均价（Gate avg_deal_price，含手续费摊薄）。

        从 CexBroker filled 后写入的 order.custom_params["cex"]["avg_price"]
        读取；DEX order 无此字段返回 None（DEX 成交价走 swap_status
        确认路径 _actual_price_from_st）。0 / 空值视为无成交均价。
        """
        cex = (getattr(order, "custom_params", None) or {}).get("cex") or {}
        avg = cex.get("avg_price")
        try:
            return float(avg) if avg not in (None, "", 0) else None
        except (TypeError, ValueError):
            return None

    def _cex_filled(self, order) -> float | None:
        """CEX 订单实际成交数量（Gate filled，含 amount_precision 截断）。

        2026-08-26：Gate 市价单按金额成交、服务端换算 base 时按
        amount_precision 向下截断（ETH 0.00126091→0.0012）——台账必须记
        实际成交而非计划量，否则账实差 → 卖出缩量 → min_quote 卡 slot。
        """
        cex = (getattr(order, "custom_params", None) or {}).get("cex") or {}
        f = cex.get("filled")
        try:
            return float(f) if f not in (None, "", 0) else None
        except (TypeError, ValueError):
            return None

    def _cex_min_quote(self, symbol: str) -> float:
        """Gate 交易对 min_quote_amount（broker 缓存拉取；0=未知不过滤）。

        2026-08-26 B 方案：卖出预检——价值 < min_quote 释放台账不卖。
        """
        broker = getattr(self, "broker", None)
        fn = getattr(broker, "min_quote_for", None)
        try:
            return float(fn(symbol) if fn else 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _is_cex(self) -> bool:
        """执行通道大类：cex=Gate 交易所子账号；dex=链上子钱包（默认）。"""
        return self.parameters.get("channel_family") == "cex"

    def _cex_slot_broker(self, slot: dict) -> Any:
        """slot → 子账号 CexBroker（子账号 key 签名；缓存，避免每轮重建）。

        S3 场景化（2026-08-20）后 slot.account_id 是场景 sub_accounts 池
        （如 mid 场景 slot1=gate_bot3），必须优先使用；全局 slot_map 是
        DEX 时代 1..N 默认映射，与场景池错位时会把资金检查/下单指向
        错误子账号（2026-08-23 修复：mid 场景 RENDER 资金检查 slot1 曾
        查 gate_bot1 余额 0.0001 → 误报「无可用资金 slot」，而场景池
        gate_bot3 实际有 4.862 USDT）。缓存 key 含 account——同 slot_no
        跨场景不复用错 broker。
        """
        # 回测驱动注入（方案 B Step 3）：per-slot BacktestBroker（内存账本，
        # 零网络）。实盘默认 None → 走下方真实 CexBroker 路径，零影响。
        factory = getattr(self, "_slot_broker_factory", None)
        if factory is not None:
            return factory(slot)
        slot_no = int(slot["slot"])
        account = str(slot.get("account_id") or "")
        key = f"{slot_no}:{account}" if account else str(slot_no)
        broker = self._cex_brokers.get(key)
        if broker is None:
            from nanobot_quant.brokers.cex_broker import CexBroker
            from nanobot_quant.gate_credentials import load_slot_map
            name = account or (
                load_slot_map().get(str(slot_no)) or f"gate_bot{slot_no}"
            )
            broker = CexBroker(
                tokens_json=self.parameters.get("tokens_json") or [],
                slippage=str(self.parameters.get("slippage", "0.01")),
                sub_account=name,
            )
            self._cex_brokers[key] = broker
        return broker

    def _cex_slot_balances(self, slot: dict, use_snapshot: bool = False) -> dict:
        """子账号 spot 余额 {CURRENCY: {available, locked}}；失败返回 {}。

        2026-08-28 A2：use_snapshot=True（BUY 路径）从预取快照取（零网络）；
        预取已跑但快照缺失（失败/未覆盖）→ 返回 {}（fail-closed 资金 0 →
        SLOT SKIP）。快照未构建（execute_signal 直调未预取）→ 回退逐查
        （保持旧行为，直调路径不受影响）。默认逐查（SELL/估值——安全
        关键路径不赌快照实时性，余额误判会误触发幽灵批次清理）。
        """
        if use_snapshot:
            try:
                name = str(slot.get("account_id") or "")
                if not name:
                    from nanobot_quant.gate_credentials import load_slot_map
                    name = load_slot_map().get(str(slot.get("slot"))) or \
                        f"gate_bot{slot.get('slot')}"
                uid = self._name_to_uid.get(name)
                bal = self._slot_balances.get(uid) if uid else None
                if bal is not None:
                    return bal
            except Exception:  # noqa: BLE001
                pass  # 匹配异常 → 走下方回退/fail-closed 分支
            if getattr(self, "_balances_prefetched", False):
                return {}  # 预取失败/未覆盖 → fail-closed
        try:
            return self._cex_slot_broker(slot)._balances()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                f"TD CEX BAL ERR | symbol={self.symbol} slot={slot['slot']} {exc}"
            )
            return {}

    def _cex_slot_quote_balance(
        self, slot: dict, quote: str = "USDT", use_snapshot: bool = False
    ) -> float:
        """子账号 quote 币种可用+锁定余额。"""
        bal = self._cex_slot_balances(
            slot, use_snapshot=use_snapshot
        ).get(quote) or {}
        return float(bal.get("available") or 0) + float(bal.get("locked") or 0)

    def _cex_slot_token_balance(self, slot: dict, symbol: str) -> float:
        """子账号该标的持仓量（Gate 余额键=基础币，gate_symbol 可能是完整 pair）。

        Gate sub_account_balances 的 available 键为基础币大写（如 "CRCLX"），
        而 tokens.json 的 gate_symbol 可能是完整交易对（"CRCLXUSDT"/"CRCLX_USDT"）——
        直接用 gate_symbol 作键会查不到而误判无持仓（曾致 CRCLX 真实持仓被释放台账）。
        与对账导入同源：经 gate_pair 剥离 quote 得基础币键（td_live._reconcile_import_cex 同款）。
        """
        from nanobot_quant.gate_credentials import gate_pair

        tokens_json = self.parameters.get("tokens_json") or []
        key = gate_pair(symbol, tokens_json).split("_")[0]
        bal = self._cex_slot_balances(slot).get(key) or {}
        return float(bal.get("available") or 0) + float(bal.get("locked") or 0)

    def _cex_slot_portfolio_value(self, slot: dict, use_snapshot: bool = False) -> float:
        """子账号总资产 USD：USDT + Σ(持仓 × Gate 价)；失败返回 0（fail-closed）。"""
        try:
            balances = self._cex_slot_balances(slot, use_snapshot=use_snapshot)
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

    _PRICE_CACHE_TTL = 15.0
    _price_cache: dict[str, tuple[float, float]] = {}  # 类级共享（同轮多场景策略实例共享 ticker 取价）

    def _cex_price_of(self, currency: str) -> float:
        """Gate 计价（子账号持仓估值用）：gate_cex 优先，okx_cex 兜底。

        类级 15s 缓存（2026-08-28）：ticker 取价是每轮耗时大头（串行
        ~1.5-2s/次），迭代开始 ``_prefetch_ticker_prices`` 并发预取填缓存，
        本轮所有估值/持仓小节/止损止盈调用全部命中（零网络）；与
        CexBroker._price_of 同 TTL 语义。0 不入缓存（fail-closed，临时
        失败不固化）。回测 override 分支在缓存前返回，零影响。
        """
        # 回测驱动注入（方案 B Step 3）：历史重放价格源（当前 bar 收盘价），
        # 零网络。实盘默认 None → 走下方真实 ticker 路径，零影响。
        override = getattr(self, "_price_source_override", None)
        if override is not None:
            try:
                return float(override(currency))
            except Exception:  # noqa: BLE001
                return 0.0
        now = time.time()
        cached = self._price_cache.get(currency)
        if cached and now - cached[0] < self._PRICE_CACHE_TTL:
            return cached[1]
        try:
            from nanobot_quant.data_sources import get_data_source
            px = get_data_source("gate_cex").get_price(currency)
            if px and px > 0:
                px = float(px)
            else:
                px = get_data_source("okx_cex").get_price(currency)
                px = float(px) if px and px > 0 else 0.0
        except Exception:  # noqa: BLE001
            return 0.0
        if px > 0:
            self._price_cache[currency] = (now, px)
        return px

    def _prefetch_ticker_prices(self) -> None:
        """迭代开始并发预取 ticker 价格（填类级 15s 缓存）。

        2026-08-28：ticker 取价串行是每轮耗时大头（~1.5-2s/次，多标的池
        累计 ~10-20s）。池标的 + 固定平台币（GT 子账号 dust 估值）并发预取
        后，本轮所有估值/持仓小节/止损止盈调用全部命中缓存（零网络）。
        """
        symbols = [s for s in getattr(self, "symbols", []) if s]
        if "GT" not in symbols:
            symbols.append("GT")
        if not symbols:
            return
        from concurrent.futures import ThreadPoolExecutor
        concurrency = max(1, int(self.parameters.get("kline_concurrency", 4) or 4))
        with ThreadPoolExecutor(max_workers=min(concurrency, len(symbols))) as ex:
            list(ex.map(self._cex_price_of, symbols))

    def _prefetch_slot_balances(self) -> None:
        """CEX 通道预取子账号余额快照（2026-08-28 A2，K 线提取之前）。

        每轮 1 次 fetch_all_balances（主 key 批量拉全部子账号，~2s）→ 存
        uid→balances 快照 + name→uid 映射。BUY 路径从快照取（零网络——
        用户拍板：余额放 K 线前，信号出来后立即买卖、余额必须已就绪）；
        SELL/估值保持逐查（安全关键路径不赌快照实时性——余额误判会触发
        幽灵批次清理导致持仓脱管）。失败 → fail-closed：快照空（BUY 资金
        0 → SLOT SKIP）并在实时监控信号区显示 balances_error。DEX 通道
        跳过（真分账依赖 wallet switch 逐账户查询）。execute_signal 直调
        未预取时回退逐查（保持旧行为，见 _cex_slot_balances）。
        """
        self._balances_error = None
        self._slot_balances = {}
        self._name_to_uid = {}
        self._balances_prefetched = False
        if self.parameters.get("channel_family") != "cex":
            return
        try:
            from nanobot_quant.gate_credentials import (
                fetch_all_balances,
                load_gate_credentials,
            )
            creds = load_gate_credentials()
            if not creds:
                raise RuntimeError("gate.json not found")
            snap = fetch_all_balances(creds)
            if not isinstance(snap, dict):
                raise RuntimeError("fetch_all_balances 返回非 dict")
            err = snap.get("__error") or (snap.get("main") or {}).get("__error")
            if err:
                raise RuntimeError(err)
            rows = snap.get("sub_accounts") or []
            if isinstance(rows, list):
                for r in rows:
                    if isinstance(r, dict) and r.get("uid"):
                        self._slot_balances[str(r["uid"])] = r.get("balances") or {}
            subs_cfg = creds.get("sub_accounts") or {}
            if isinstance(subs_cfg, dict):
                items = subs_cfg.items()
            else:
                items = (
                    (sa.get("name"), sa) for sa in subs_cfg
                    if isinstance(sa, dict)
                )
            for name, sa in items:
                uid = sa.get("uid") if isinstance(sa, dict) else sa
                if name and uid:
                    self._name_to_uid[str(name)] = str(uid)
            self._balances_prefetched = True
        except Exception as exc:  # noqa: BLE001
            self._slot_balances = {}
            self._name_to_uid = {}
            self._balances_prefetched = True
            self._balances_error = str(exc)
            print(
                f"[TD] BAL SNAP FAIL | {type(exc).__name__}: {exc} "
                "（本轮 BUY 跳过，实时监控显示 balances_error）",
                file=sys.stderr, flush=True,
            )

    def _cex_submit(self, slot: dict, req) -> Any:
        """子账号 broker 下单（绕过策略主 broker——子账号必须用自己的 key）。

        回测触发源透传：BUY 的 req.reason（TD LONG）写 entry_reason、SELL 的
        req.reason（TD SELL / 止盈 / 止损）写 exit_reason，BacktestBroker 收进
        _tracked → fills_detail.reason；实盘 CexBroker 忽略该键，无副作用。
        """
        order = self.create_order(req.asset, req.quantity, req.action)
        if req.reason:
            order.custom_params = order.custom_params or {}
            order.custom_params["entry_reason" if req.action == "buy" else "exit_reason"] = req.reason
        return self._cex_slot_broker(slot).submit_order(order)

    def _buy_on_slot_cex(self, slot: dict, price: float, reason: str):
        """CEX 通道 BUY（Step 1）：子账号独立 key，无 wallet_switch；

        pv_slot = 子账号总资产（USDT + 持仓×Gate 价）→ min_account_value
        → qty（fixed/value）→ position_limit（pv_slot 基准）→ USDT 资金
        检查 → 子账号 broker 下单。返回 (order, qty) 或 None（跳下一 slot）。
        """
        try:
            pv_slot = self._cex_slot_portfolio_value(slot, use_snapshot=True)
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
            bal = self._cex_slot_quote_balance(slot, "USDT", use_snapshot=True)
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
            # B 方案（2026-08-26 用户拍板）：卖出价值 < Gate min_quote →
            # 释放台账不卖——服务端会拒单（below min order amount）→
            # EXIT_FAIL → 台账保持 open 卡 slot（2026-08-25 ETH $2.96 实证）。
            # 仓位留在子账号（脱管），价值回升 ≥ min_quote 后由重启对账重新导入。
            try:
                min_q = self._cex_min_quote(self.symbol)
            except Exception:  # noqa: BLE001
                min_q = 0.0
            if min_q > 0 and qty * price < min_q:
                self.batch_manager.close_lot(slot["slot"])
                self.batch_manager.save()
                self.logger.warning(
                    f"TD BATCH EXIT RELEASE | symbol={self.symbol} "
                    f"slot={slot['slot']} 价值 ${qty * price:.2f} < "
                    f"min_quote ${min_q:g}，释放台账（仓位留在子账号）"
                )
                self._record(
                    "EXIT_RELEASE",
                    f"slot={slot['slot']} 价值 ${qty * price:.2f} < "
                    f"min_quote ${min_q:g} 释放台账",
                    slot=slot["slot"], qty=qty, price=price,
                    direction="sell", status="ok",
                )
                return
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
                    actual_price=self._cex_avg_price(order),
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
    def _bar_seconds(self) -> int:
        """当前场景 bar 周期秒数（min_hold 换算用）。"""
        return _parse_sleeptime_seconds(self.sleeptime)

    def _note_min_hold(self, symbol: str, slot: int, ts=None) -> None:
        """记录最短持有期到期时间（BUY / 对账导入 / 确认补仓共用，2026-08-28）。

        到期 = 买入时刻 + min_hold_bars × bar 周期；min_hold_bars=0 不记录。
        ts 缺省用当前 bar 时间（实盘 BUY 路径）；对账/确认路径显式传时间。
        """
        if self._min_hold_bars <= 0:
            return
        base = self._bar_utc_ts(ts) if ts is not None else self._current_bar_time
        if base is None:
            return
        self._min_hold_until[(symbol, slot)] = base + pd.Timedelta(
            seconds=self._min_hold_bars * self._bar_seconds()
        )

    @staticmethod
    def _bar_utc_ts(ts) -> pd.Timestamp:
        """bar 时间戳统一转 UTC naive（min_hold 比较基准）。"""
        ts = pd.Timestamp(ts)
        return ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts

    def _min_hold_blocked(self, slot: dict) -> bool:
        """买入后 min_hold_bars 根 bar 内 TD SELL（高9/cd13）被拦截。"""
        if self._min_hold_bars <= 0:
            return False
        until = self._min_hold_until.get((self.symbol, slot["slot"]))
        if until is None:
            return False
        return self._current_bar_time < until

    def _min_hold_remaining(self, slot: dict) -> int:
        """剩余持有 bar 数（诊断日志用；未记录返回 -1）。"""
        until = self._min_hold_until.get((self.symbol, slot["slot"]))
        if until is None:
            return -1
        remain = (until - self._current_bar_time).total_seconds()
        return max(int(remain // self._bar_seconds()) + 1, 0)

    def _profit_gate_blocked(
        self, slot: dict, price: float, setup_sell: int = 0, cd_sell: int = 0
    ) -> bool:
        """高9 毛浮盈门（2026-08-25）+ 双档动能判断（2026-08-29 拍板落地）。

        毛口径 (price−entry)/entry 未扣手续费——用户自行计算含成本阈值
        （如 Gate 双程 0.2% → 0.002）。

        momentum_exit=False（方案 B）→ 简单门：pnl < high 拦（现有行为）。
        momentum_exit=True（拍板主方案）→ 三档：
          pnl ≥ high               → 放行（无条件卖，让利润奔跑的终点）
          pnl < low                → 拦（死扛，<低档不卖）
          low ≤ pnl < high         → 动能判断：
               setup_sell ≥ 10        → 拦（setup 仍在累加，强动能，持有至上门）
               cd_stall ≥ cd_stall_n  → 放行（cd_sell 连续 N 根无 +1，停滞弱，低档落袋）
               cd_sell > 0            → 拦（countdown 推进中，强动能，持有）
               其他（首次高9、cd_sell=0）→ 拦（动能未知，保守持有）
        """
        high = getattr(self, "_sell_only_profit_high", 0.0) or 0.0
        if high <= 0:
            return False
        lot = slot.get("lot")
        if lot is None or not lot.get("entry_price"):
            print(
                f"[TD HIGH9 GATE] symbol={self.symbol} slot={slot.get('slot')} "
                f"entry={lot.get('entry_price') if lot else None!r} price={price} "
                f"high={high} -> 放行（entry 无效，fail-open）",
                file=sys.stderr, flush=True,
            )
            return False
        pnl = (price - lot["entry_price"]) / lot["entry_price"]
        low = getattr(self, "_sell_only_profit_low", 0.0) or 0.0
        momentum = bool(getattr(self, "_momentum_exit", False))
        if not momentum:
            blocked = pnl < high
            mode = f"简单门(high={high})"
        elif pnl >= high:
            blocked = False
            mode = f"≥上档({high})无条件卖"
        elif pnl < low:
            blocked = True
            mode = f"<下档({low})死扛"
        else:
            stall = self._cd_stall_state.get(self.symbol, {}).get("stall", 0)
            n = max(int(getattr(self, "_cd_stall_n", 3) or 3), 1)
            if setup_sell >= 10:
                blocked = True
                mode = f"[{low},{high}) setup_sell={setup_sell}≥10 强→持有"
            elif stall >= n:
                blocked = False
                mode = f"[{low},{high}) stall={stall}≥{n} 弱→落袋"
            elif cd_sell > 0:
                blocked = True
                mode = f"[{low},{high}) cd_sell={cd_sell}>0 推进→持有"
            else:
                blocked = True
                mode = f"[{low},{high}) 未知(首次高9)→持有"
        print(
            f"[TD HIGH9 GATE] symbol={self.symbol} slot={slot.get('slot')} "
            f"entry={lot['entry_price']} price={price} pnl={pnl:.4f} "
            f"high={high} low={low} momentum={momentum} setup_sell={setup_sell} "
            f"cd_sell={cd_sell} stall={self._cd_stall_state.get(self.symbol, {}).get('stall', 0)} "
            f"-> {'拦' if blocked else '放行'}（{mode}）",
            file=sys.stderr, flush=True,
        )
        return blocked
