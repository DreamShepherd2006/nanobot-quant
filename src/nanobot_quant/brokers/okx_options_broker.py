"""Lumibot Broker adapter for OKX USDSⓈ-M linear crypto options.

执行层适配（E 期，docs/quant-system.md §33.26）：lumibot 官方引擎原样跑，
这里只做「期权标的映射 + 账户差异」的薄适配——13 个抽象方法全部转发到
``okx_options_trade``（官方 python-okx + 已有下单/台账/持仓实现），不重写
任何业务逻辑。

关键点：
- **只卖不买**（卖方策略）：``side=sell`` 开仓（put/call）、``side=buy`` 买回平仓；
  方向由 asset.right 决定（CALL→call 路径、PUT→put 路径）。
- **multiplier 必须显式传**：期权 Asset 由 ``inst_to_asset`` 构造，带每张面值
  （SOL 0.1 / BTC 0.01）——依赖 lumibot fork 的 patch（honor explicit multiplier），
  否则 lumibot 会按美股约定当成 100。
- **不做市价单**：期权盘口薄，OKX 期权 ordType 仅接受 limit/post_only/fok/ioc。
  默认 IOC + 保护线 px（C22b：N 张模拟均价 × (1∓容忍滑点)），等价于
  「带价扫单、吃不完自动撤」。
- **不自动开仓**：本 Broker 只是执行通道，是否下单由策略/人工决定；
  期权 live 开关仍在 option_params.json（仅 WebUI 手动写入）。
"""

from __future__ import annotations

import logging
from typing import Optional

from lumibot.brokers import Broker
from lumibot.entities import Asset, Order, Position

from nanobot_quant import okx_options_trade as oot
from nanobot_quant.okx_options_assets import (
    asset_to_inst,
    family_of,
    inst_to_asset,
    lot_of,
)

logger = logging.getLogger("nanobot_quant.brokers.okx_options")

# 现金币种（期权保证金/权利金以 USDC/USD 结算）
_CASH_CCY = ("USDC", "USD", "USDG", "USDT")

# 「已受理、未定案」状态（不是失败也不是成功）：台账 pending 行接管，
# 下一轮复检定案。含 unknown —— 状态查不清时寧可等复检，不能当成失败重发单。
_IN_FLIGHT_STATUSES = frozenset({
    "pending", "unknown", "live", "open", "partially_filled", "submitted",
})


class _OptionsDummyDataSource:
    """占位 data source（Broker.__init__ 要求非 None）；期权链走 option_source。"""

    def __init__(self, *args, **kwargs):
        pass

    def get_chains(self, asset=None, quote=None):
        return {}


class OkxOptionsBroker(Broker):
    """OKX 期权执行通道（U 本位线性，逐仓 isolated）。

    Parameters:
        account: 期权子账号名/label（如 ``DreamShepherdbot1``），空 = 默认账号。
        cost_basis_map: {base: 成本锚 C}——卖 call（covered）保本门用；
            **逐单经 ``order.custom_params['cost_basis']`` 优先**（自动循环走这条路，
            因为 C 是每轮由 covered_context 现算的动态值）。
        option_source: lumibot 期权 DataSource（OkxOptionsDataSource）。
    """

    SOURCE = "okx_options"

    def __init__(self, account: str = "", cost_basis_map: Optional[dict] = None,
                 **kwargs):
        if "data_source" not in kwargs and "option_source" not in kwargs:
            kwargs["data_source"] = _OptionsDummyDataSource()
        super().__init__(**kwargs)
        self.market = "24/7"  # 期权 7×24 交易
        self.account = account or ""
        self._cost_basis_map = dict(cost_basis_map or {})
        self._tracked: dict[str, dict] = {}  # ord_id → {inst_id, sz, side}

    # ═══════════════════════════ 辅助 ═══════════════════════════
    def _creds(self) -> dict:
        return oot.account_creds(self.account)

    def _cost_basis_for(self, order) -> Optional[float]:
        """卖 call 的成本锚 C：逐单 ``order.custom_params['cost_basis']`` 优先，
        构造期 ``cost_basis_map`` 兼底。

        ⚠️ 必须从 **order** 读（不是 asset）：``Asset`` 上没有 custom_params 约定，
        早期版本读 ``asset.custom_params`` 永远为空 → 逐单通道形同死代码，
        自动卖 call 每轮因缺 C 被保本门 fail-closed（2026-09-30 实测）。
        """
        asset = getattr(order, "asset", None)
        base = str(getattr(asset, "symbol", "") or "").upper()
        cp = getattr(order, "custom_params", None)
        if isinstance(cp, dict) and cp.get("cost_basis"):
            return float(cp["cost_basis"])
        v = self._cost_basis_map.get(base)
        return float(v) if v else None

    def _remember(self, order: Order, inst_id: str) -> dict:
        """记录 ord_id ↔ instId（lumibot 的 _pull_broker_order 只给 identifier）。"""
        ident = str(getattr(order, "identifier", "") or "")
        if ident:
            self._tracked[ident] = {"inst_id": inst_id}
        return self._tracked.get(ident, {})

    # ══════════════════ lumibot Broker 抽象方法（13 个） ══════════════════
    def _submit_order(self, order: Order) -> Order:
        asset = order.asset
        inst_id = asset_to_inst(asset)
        sz = int(round(abs(float(order.quantity or 0))))
        if sz <= 0:
            order.set_error("期权下单张数必须为正整数")
            return order
        side = str(getattr(order, "side", "") or "").lower()
        right = "C" if str(getattr(asset, "right", "") or "").upper().startswith("C") else "P"
        # 定价：IOC + 保护线（C22b）——盘口不可用时 suggest 内部回退旧规则（bid×0.5 / ask）
        try:
            sug = oot.suggest_px_for_order(inst_id, "sell" if side == "sell" else "buy", sz=sz)
            px = sug.get("px")
        except Exception as e:
            order.set_error(f"取价失败：{e}")
            return order
        if not px or float(px) <= 0:
            order.set_error(f"无有效保护价（instId={inst_id}）——fail-closed 不下单")
            return order
        ord_type = "ioc"  # 期权不支持市价；IOC 带价扫单、未成交自动撤
        try:
            if side == "sell":
                if right == "C":
                    res = oot.open_call(self.account, inst_id=inst_id, sz=sz,
                                        ord_type=ord_type, px=float(px),
                                        cost_basis=self._cost_basis_for(order))
                else:
                    res = oot.open_put(self.account, inst_id=inst_id, sz=sz,
                                       ord_type=ord_type, px=float(px))
            else:
                fn = oot.close_call if right == "C" else oot.close_put
                res = fn(self.account, inst_id=inst_id, sz=sz,
                         ord_type=ord_type, px=float(px))
        except Exception as e:
            # 失败必须可见（fail-closed）：不静默、不近似执行
            print(f"[OKX-OPT-BROKER] submit failed {inst_id} sz={sz} side={side}: {e}",
                  flush=True)
            order.set_error(str(e))
            return order

        order.identifier = str(res.get("ord_id") or "")
        self._remember(order, inst_id)
        meta = self._tracked.get(order.identifier, {})
        meta.update({"inst_id": inst_id, "sz": sz, "side": side,
                     "px": float(px), "ord_type": ord_type})
        if order.identifier:
            self._tracked[order.identifier] = meta
        status = str(res.get("status") or "")
        # 状态透传给策略（三档判定用：filled / 在途未定案 / 失败）——
        # 不能只靠 order.error 二分：IOC 在 5s 轮询窗口内常报在途、随后成交。
        order.custom_params = order.custom_params or {}
        order.custom_params["opt_status"] = status
        order.custom_params["opt_note"] = str(res.get("note") or "")
        if status in ("filled", "closed"):
            order.set_filled()
        elif status in _IN_FLIGHT_STATUSES:
            # 交易所已受理、尚未定案（IOC 在途 / 限价未成交 / 状态查询失败）——
            # **不是失败**：台账已留下 pending 行接管（has_pending_ledger 防重），
            # 下一轮复检定案（2026-09-29 实测：14:05 的 IOC 在 5s 窗口内报 live，
            # 随后成交，却被打成「卖出失败」）。仍不算成功：不计成交量、
            # is_filled() 为假。
            pass
        else:
            # 真正的失败（撤销 / 拒单 / 空状态）必须如实上报，禁止假成功：
            # pending/unknown 曾被当成成功 → 页面显示「已买回」而持仓未动、
            # 每 60s 重复提交（2026-09-29 实测）。
            order.set_error(res.get("note")
                            or f"订单未成交（状态 {status or 'unknown'}）")
        return order

    def cancel_order(self, order: Order) -> None:
        ident = str(getattr(order, "identifier", "") or "")
        inst_id = (self._tracked.get(ident) or {}).get("inst_id") or asset_to_inst(order.asset)
        if not inst_id or not ident:
            return
        oot.cancel_order(self.account, inst_id=inst_id, ord_id=ident)

    def _modify_order(self, order: Order, limit_price=None, stop_price=None) -> None:
        """期权不支持改单——撤单后重新下单（保持接口存在，不静默失败）。"""
        logger.info("modify_order ignored (unsupported for options): %s",
                    getattr(order, "identifier", ""))
        return None

    def _get_balances_at_broker(self, quote_asset, strategy):
        """→ (cash, positions_value, portfolio_value)，均按 USD 口径。"""
        bal = oot.account_balance(self.account)
        cash = 0.0
        for d in bal.get("details", []):
            if str(d.get("ccy", "")).upper() in _CASH_CCY:
                cash += float(d.get("avail_bal") or 0.0)
        positions_value = 0.0
        for p in self._pull_positions(strategy) or []:
            positions_value += abs(float(p.quantity or 0)) * float(
                getattr(p.asset, "multiplier", 0) or 0) * float(getattr(p, "current_price", 0) or 0)
        total = cash + positions_value
        return cash, positions_value, total

    def _pull_positions(self, strategy):
        """OKX 当前期权净仓 → lumibot Position（含每张面值 multiplier）。"""
        rows = oot.open_option_positions(self.account)
        out = []
        for r in rows:
            asset = inst_to_asset(r.get("inst_id", ""))
            if asset is None:
                continue
            qty = float(r.get("pos") or 0)
            if qty == 0:
                continue
            if r.get("side") == "short":
                qty = -qty
            pos = Position(strategy, asset, quantity=qty)
            pos.current_price = float(r.get("mark_px") or r.get("avg_px") or 0.0)
            out.append(pos)
        return out

    def _pull_position(self, strategy, asset):
        target = asset_to_inst(asset)
        for pos in self._pull_positions(strategy) or []:
            if asset_to_inst(pos.asset) == target:
                return pos
        return None

    def _pull_broker_order(self, identifier):
        meta = self._tracked.get(str(identifier)) or {}
        inst_id = meta.get("inst_id")
        if not inst_id:
            return None
        try:
            return oot.poll_order(self._creds(), inst_id, str(identifier))
        except Exception as e:
            logger.warning("poll_order failed %s: %s", identifier, e)
            return None

    def _parse_broker_order(self, response: dict, strategy_name: str, strategy_object=None):
        """OKX 订单字典 → lumibot Order（最小可用映射）。"""
        if not isinstance(response, dict):
            return None
        asset = inst_to_asset(response.get("inst_id") or response.get("instId") or "")
        if asset is None:
            return None
        sz = response.get("sz")
        if sz in (None, ""):
            sz = response.get("acc_fill_sz") or 0
        side = str(response.get("side") or "sell").lower()
        # 全关键字构造：stub 与真实 lumibot 的位置参数顺序不同
        # （真实：strategy, asset, quantity…），关键字调用两边都正确。
        order = Order(strategy=strategy_object or strategy_name, asset=asset,
                      quantity=abs(float(sz or 0)),
                      side=side if side in ("buy", "sell") else "sell",
                      order_type=Order.OrderType.LIMIT,
                      identifier=str(response.get("ord_id") or ""))
        px = response.get("avg_px") or response.get("px")
        if px:
            order.limit_price = float(px)
        return order

    def _pull_broker_all_orders(self):
        """当前未成交委托（按家族逐个拉取，OKX 不支持无 family 全量）。"""
        out = []
        for base in ("BTC", "ETH", "SOL", "XAU"):
            try:
                rows = oot.pending_orders(self.account, inst_family=family_of(base))
            except Exception:
                continue
            for r in rows:
                o = self._parse_broker_order(r, "okx_options")
                if o is not None:
                    self._tracked[str(r.get("ord_id") or "")] = {
                        "inst_id": r.get("inst_id", ""), "sz": r.get("sz"),
                        "side": r.get("side")}
                    out.append(o)
        return out

    # ── 流式接口：期权走轮询（无常驻订阅需求） ──
    def _get_stream_object(self):
        return None

    def _register_stream_events(self):
        return None

    def _run_stream(self):
        return None

    def get_historical_account_value(self):
        return {}


def make_option_asset(base: str, expiration, strike: float, right: str = "PUT",
                      quote: Optional[Asset] = None) -> Asset:
    """便捷构造：期权 Asset + 正确的 multiplier（每张面值）。

    ``make_option_asset("SOL", date(2026, 9, 18), 94, "PUT")``
    """
    lot = lot_of(base)
    if lot <= 0:
        raise ValueError(f"未知期权家族 {base!r}（已知：{sorted(oot.FAMILY_LOT)}）")
    return Asset(symbol=str(base).upper(), asset_type="option",
                 expiration=expiration, strike=float(strike),
                 right=str(right).upper(), multiplier=lot)
