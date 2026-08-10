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
        "quantity_mode": "fixed",  # "fixed" = fixed quantity; "value" = pv × pct
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
        # 失败导致 "Cannot resolve addresses: X→USD"。此处显式设 USDC。
        broker = getattr(self, "broker", None)
        if broker is not None and broker.__class__.__name__ == "OnchainOSBroker":
            from lumibot.entities import Asset
            self.quote_asset = Asset("USDC", asset_type="crypto")
        self.symbol = symbol or self.parameters.get("symbol", "AAPL")
        # 标的池（多标的扫描，2026-08-10）：每轮遍历 symbols 算信号，
        # 谁 Setup 9 谁执行；self.symbol 在每标的评估时切换。
        self.symbols = list(self.parameters.get("symbols") or [self.symbol])
        self.quantity = quantity or self.parameters.get("quantity", 10)
        self.quantity_mode = quantity_mode or self.parameters.get("quantity_mode", "fixed")
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

        # TD algorithm params (subset of the strategy parameters dict)
        self._td_params = {
            k: self.parameters.get(k, v)
            for k, v in DEFAULT_TD_PARAMS.items()
        }

    def on_trading_iteration(self):
        """Called for each bar (trading day) during the backtest.

        Fetches all available historical bars up to the current bar,
        calls ``calculate()`` for the latest TD Sequential signal,
        then creates buy/sell orders based on the rules above.

        标的池模式：按池子顺序（=优先级）逐标的评估，谁 Setup 9 谁执行；
        同 bar 多标的命中按顺序全部处理（资金天然隔离）。
        """
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

    def _evaluate_symbol(self) -> None:
        """单标的评估（拉 K 线 → TD 计算 → 信号 → 真分账/常规下单）。"""
        # ── 1. Fetch historical data ──
        try:
            bars = self.get_historical_prices(
                self.symbol,
                length=self._min_history,
                timestep=self._timestep,
            )
        except Exception as e:
            self.logger.warning(
                f"TD DATA ERROR | {type(e).__name__}: {e}"
            )
            return

        if bars is None or bars.df.empty:
            self.logger.warning("TD DATA EMPTY | bars is None or empty")
            return

        df = bars.df.copy()

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
            self.logger.warning(
                f"TD SKIP | bars={len(df)} < min_history={self._min_history}"
            )
            return

        signal = calculate(df, params=self._td_params)

        # ── 4. Evaluate signals ──
        setup_buy = signal.get("setup_buy", 0) or 0
        setup_sell = signal.get("setup_sell", 0) or 0
        cd_sell = signal.get("cd_sell", 0) or 0
        score = signal.get("score", 0) or 0
        price = signal.get("price", 0) or 0

        has_position = self.get_position(self.symbol) is not None

        # ── Update peak portfolio for drawdown tracking ──
        pv = self.portfolio_value
        if self._peak_portfolio is None or pv > self._peak_portfolio:
            self._peak_portfolio = pv

        entry_setup = int(self._td_params.get("entry_setup", 9))
        exit_setup = int(self._td_params.get("exit_setup", 9))
        exit_countdown = int(self._td_params.get("exit_countdown", 13))
        score_threshold = float(self._td_params.get("score_threshold", 0.0))
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
                    self.logger.info(f"TD BLOCK ({result.check_name}) | {result.reason}")
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
                    ret = self._buy_on_slot(slot, price, reason)
                    if ret is None:
                        continue
                    order, qty = ret
                    self.batch_manager.open_lot(
                        slot=slot["slot"], qty=qty, entry_price=price,
                    )
                    # 交易状态变更立即落盘（重启不丢台账）
                    self.batch_manager.save()
                    self.logger.info(
                        f"TD BATCH LONG | slot={slot['slot']} "
                        f"price={price:.2f} qty={qty} "
                        f"setup_buy={setup_buy} score={score:.1f}"
                    )
                    executed = True
                    break
                if not executed:
                    self.logger.info(
                        "TD BATCH | 无可用资金 slot，跳过 BUY（见 TD SLOT SKIP 日志）"
                    )
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
                    f"TD LONG  | price={price:.2f} qty={req.quantity} "
                    f"setup_buy={setup_buy} score={score:.1f}"
                )
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
                    f"TD EXIT  | price={price:.2f} qty={req.quantity} {exit_reason}"
                )
                return

        # ── No signal this bar ──
        self.logger.info(
            f"TD HOLD | price={price:.4f} setup_buy={setup_buy} "
            f"setup_sell={setup_sell} cd_sell={cd_sell} score={score:.1f}"
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

    def _sell_lot(
        self, slot: dict, price: float, signal: dict, exit_reason: str
    ) -> None:
        """卖出一个批次（lot.qty），下单成功后回收 slot。

        v1.1 真分账：卖出前 switch 到该 slot 绑定的子钱包，交易后还原
        默认账户；卖出量改为 ``float(lot.qty)``（修复 int 截断小数问题，
        如 0.05 CRCLX）。lumibot 订单异步提交——下单成功即回收（记录
        卖出意图），链上余额与台账偏差由对账逻辑以链上为准修正。

        2026-08-10 链上校验（缩量卖出，用户拍板）：switch 后查该账户
        实际余额——余额 < lot.qty 按实际余额卖（不跳过、不卖空），
        余额 0 或查询失败跳过该批并告警。
        """
        lot = self.batch_manager.close_lot(slot["slot"])
        if lot is None:
            return
        # 平仓/释放状态立即落盘（重启不丢台账）
        self.batch_manager.save()
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
                        f"TD BATCH EXIT SKIP | slot={slot['slot']} "
                        f"链上余额查询失败"
                    )
                    # 查询失败 = 链上状态未知 → 恢复台账（fail-safe），
                    # 避免 close_lot 已释放但链上仍有持仓的账实脱节
                    self.batch_manager.open_lot(
                        qty, lot.get("entry_price", 0.0),
                        lot.get("entry_time"), slot=slot["slot"],
                    )
                    self.batch_manager.save()
                    return
                if bal <= 0:
                    self.logger.warning(
                        f"TD BATCH EXIT SKIP | slot={slot['slot']} "
                        f"链上余额为 0（台账 {qty} 已释放）"
                    )
                    return
                if bal < qty:
                    self.logger.warning(
                        f"TD BATCH EXIT SHRINK | slot={slot['slot']} "
                        f"台账 {qty} 链上 {bal:.6f} → 缩量卖出"
                    )
                    qty = bal
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
                    self.logger.warning(f"TD RESTORE ERR | {exc}")
        if order is not None and not _order_error(order):
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
                f"TD BATCH EXIT | slot={slot['slot']} price={price:.2f} "
                f"qty={qty} {exit_reason}"
            )
            return
        # 订单失败（如 quote 解析失败、资金不足）→ 恢复 slot，台账回到卖出前
        err = _order_error(order) or "order is None"
        self.logger.warning(
            f"TD BATCH EXIT FAIL | slot={slot['slot']} price={price:.2f} "
            f"qty={qty} {exit_reason} error={err}"
        )
        try:
            self.batch_manager.open_lot(
                float(lot["qty"]), float(lot["entry_price"]),
                lot.get("entry_time"), slot=slot["slot"],
            )
            self.batch_manager.save()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"TD LOT RESTORE ERR | {exc}")

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
        """switch 到目标子钱包（全局状态，改写 selected_account_id）。"""
        try:
            from nanobot_quant.tools.tools_wallet import wallet_switch
            r = wallet_switch(account_id)
            return bool(r and r.get("ok", False))
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
                    self.logger.warning(f"TD RESTORE ERR | {exc}")

    def _buy_on_slot(self, slot: dict, price: float, reason: str):
        """真分账 BUY（B 方案 2026-08-10）：switch 到 slot 子钱包 →
        目标 slot 账户总资产（pv_slot）→ min_account_value 门槛 → qty
        （fixed=td_quantity；value=pv_slot×max_position_pct 小数不取整）→
        position_limit（基于 pv_slot）→ USDC 资金检查 → submit → 还原默认账户。

        返回 (order, qty) 或 None（switch 失败/余额查询失败/低于资金门槛/
        风控拒绝/USDC 不足 → 调用方跳下一 slot）。
        """
        aid = slot.get("account_id")
        home = self._home_account_id()
        if aid and not self._wallet_switch(aid):
            self.logger.warning(f"TD SLOT SKIP | slot={slot['slot']} switch 失败")
            return None
        try:
            pv_slot = self._slot_portfolio_value()
            if pv_slot <= 0:
                self.logger.warning(
                    f"TD SLOT SKIP | slot={slot['slot']} 余额查询失败/为零"
                )
                return None
            # 子账户最小资金门槛（BUY-only；SELL/止损/止盈平仓永远允许）
            min_v = float(self.parameters.get("min_account_value", 0) or 0)
            if min_v > 0 and pv_slot < min_v:
                self.logger.warning(
                    f"TD SLOT SKIP (min_account_value) | slot={slot['slot']} "
                    f"pv=${pv_slot:.2f} < ${min_v:.2f}"
                )
                return None
            # 数量：fixed=固定 td_quantity；value=pv_slot × max_position_pct / price
            # （小数不取整——避免 SOL $77/CRCLX $68 等高价标的 int 截断成 0
            #  后被 max(...,1) 抬成 1 个导致永远 BLOCK；金额驱动自动适配价格差）
            if self.quantity_mode == "value":
                qty = pv_slot * self._risk.max_position_pct / price if price > 0 else 0.0
            else:
                qty = float(self.quantity or 0)
            if qty <= 0:
                return None
            result = self._risk.can_enter(
                position_value=qty * price,
                portfolio_value=pv_slot,
                peak_portfolio=pv_slot,
            )
            if not result.approved:
                self.logger.info(
                    f"TD BLOCK ({result.check_name}) | slot={slot['slot']} "
                    f"pos=${qty * price:.2f} > "
                    f"{self._risk.max_position_pct * 100:.0f}% of slot pv=${pv_slot:.2f}"
                )
                return None
            bal = self._slot_quote_balance("USDC")
            needed = qty * price
            if bal is None or bal < 0 or bal < needed:
                self.logger.warning(
                    f"TD SLOT SKIP | slot={slot['slot']} 资金不足 "
                    f"({bal:.4f} < {needed:.4f} USDC)"
                )
                return None
            req = self._portfolio.build_buy_order(
                self.symbol, price, reason, quantity=qty,
            )
            order = self._portfolio.submit_order(req)
            return (order, qty)
        finally:
            if home and aid and home != aid:
                try:
                    self._wallet_switch(home)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(f"TD RESTORE ERR | {exc}")

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


