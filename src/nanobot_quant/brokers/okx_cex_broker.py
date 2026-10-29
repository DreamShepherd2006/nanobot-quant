"""OKX CEX 现货执行通道 broker（§33.52 P2）。

与 :class:`~nanobot_quant.brokers.cex_broker.CexBroker`（Gate）同构：策略层零改动，
只换 broker 实现。**P2 只落结构**——``EXECUTION_CHANNELS`` 不含 ``okx_cex``、
数据源 ``kind`` 仍为 ``research``，UI 不可选（拍板 A，见 docs/quant-system.md
§33.52.10）。

与 Gate 通道的语义差异（勿按 Gate 直觉改）：

- **单账号承载多逻辑 slot**（``slot_model="shared_pool"``）：账户由 ``sub_account``
  指定（子账号 name 或 uid），slot 只是台账概念。消费端（``ensure_batches`` /
  余额快照 / 资金池检查）在 P3 实现，本文件不假设。
- **``sz`` = 基础币数量**（``tgtCcy="base_ccy"``，按 ``lotSz`` 向下取整），台账与
  成交天然一致；不用 Gate 的「quote 金额」语义（服务端按精度截断 base，实测偏差
  0.1–4.7%，曾致 BNB 精度死锁与 ETH slot2 卡仓）。
- **instId** 经 :func:`nanobot_quant.okx_cex_data.to_inst_id`（与数据源同源），
  计价币默认 USDT，可由构造参数覆盖。
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Optional

from lumibot.brokers import Broker

from .. import okx_sdk, okx_spot
from ..okx_cex_data import inst_id_for, spot_inst_ids, to_inst_id
from ..okx_options_trade import account_balance, account_creds

logger = logging.getLogger(__name__)

#: 现货计价/现金币种（估值时归入 cash，不算持仓）
_CASH_CURRENCIES = frozenset({"USDT", "USDC", "USD", "USDG"})


def _num(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _base_of(inst_id: str) -> str:
    """instId → 基础币（``SOL-USDT`` → ``SOL``）。"""
    return (inst_id.split("-")[0] if inst_id else "").upper()


# 占位数据源（2026-10-29 起两个 CEX broker 共用一份）：
# 必须含 get_datetime，否则 StrategyExecutor 每轮 get_datetime() 抛
# AttributeError、循环被 on_bot_crash 打死（本通道实测）。
from nanobot_quant.brokers._dummy_data_source import (
    DummyDataSource as _DummyDataSource,
)


def _default_quote_ccy() -> str:
    """从 WebUI 执行参数读计价币（失败/非法 → USDT）。"""
    try:
        from ..exec_params import load_exec_params

        v = str(load_exec_params().get("quote_ccy") or "USDT").upper().strip()
        return v if v in ("USDT", "USDC") else "USDT"
    except Exception:  # noqa: BLE001 — 参数读失败不应打死下单
        return "USDT"


class OkxCexBroker(Broker):
    """Lumibot Broker，路由到 OKX CEX 现货市价单。

    Parameters:
        credentials: 未用（保留与 CexBroker 同签名；实际由 ``account_creds`` 读盘）。
        tokens_json: tokens.json 条目（symbol / gate_symbol / okx_symbol）。
        sub_account: OKX 子账号 name 或 uid；空 → 第一个已录入完整凭证的子账号。
        quote: 计价币（默认 USDT，与数据源一致；可传 USDC）。
        slippage: 价格保护容忍度（百分比，1 = 1%）——市价单不参与定价，
            保留参数只为与 CexBroker 同签名（预检与记录用）。
    """

    SOURCE = "okx_cex"

    #: 合约规格缓存（lotSz / minSz / tickSz），按 instId
    _limits_cache: dict[str, tuple[float, dict]] = {}
    _LIMITS_TTL = 300.0

    #: 取价缓存（同 CexBroker：类级共享、跨实例复用、15s ≈ 一轮生命周期）
    _price_cache: dict[str, tuple[float, float]] = {}
    _PRICE_TTL = 15.0

    def __init__(
        self,
        credentials: Optional[dict] = None,
        tokens_json: Optional[list[dict]] = None,
        sub_account: Optional[str] = None,
        quote: Optional[str] = None,
        slippage: str = "0.01",
        **kwargs,
    ):
        if "data_source" not in kwargs:
            kwargs["data_source"] = _DummyDataSource()
        super().__init__(**kwargs)
        # 加密 24/7：让 StrategyExecutor 循环持续运转
        self.market = "24/7"
        self._tokens_json = tokens_json or []
        self._sub_account = sub_account or ""
        # 2026-10-29：计价币由 WebUI /config/exec 的「计价币（OKX CEX）」决定
        # （quote_ccy），显式传参仍然优先（测试/回测可覆盖）。
        self._quote = (quote or _default_quote_ccy()).upper().strip()
        if self._quote not in ("USDT", "USDC"):
            self._quote = "USDT"
        self.__dict__.setdefault("_other_quote", "USDC" if self._quote == "USDT" else "USDT")
        self._slippage = float(slippage)

        self._creds = account_creds(self._sub_account)
        if not (self._creds.get("api_key") and self._creds.get("secret_key")):
            raise ValueError(
                "OKX CEX credentials missing api_key/secret_key"
                f" (okx_cex.json sub_account={sub_account!r})"
            )
        self._tracked: dict[str, dict] = {}  # ord_id → meta

    # ═══════════════════════════════════════════════════════════
    #  Helpers
    # ═══════════════════════════════════════════════════════════

    def _base_ticker(self, symbol: str) -> str:
        """symbol → 交易所 ticker（tokens.json 的 ``okx_symbol`` 优先）。"""
        base = symbol
        try:
            from ..gate_credentials import okx_ticker

            base = okx_ticker(symbol, self._tokens_json) or symbol
        except Exception as e:  # noqa: BLE001
            print(f"[DIAG] OKX-CEX okx_ticker {symbol} error: {e}",
                  file=sys.stderr, flush=True)
        return base

    def inst_id_of(self, symbol: str) -> str:
        """symbol → OKX 现货 instId（**以交易所 SPOT 清单为准**，不靠启发猜）。

        2026-10-29 实盘：``to_inst_id`` 的「常见币白名单 + 股票加 X」启发把
        SOL/BNB/AAVE 当成股票拼 ``XSOL-USDT`` → 51001。现走 ``resolve_inst_id``
        （清单判定，候选含 ``-USDT`` / ``-USDC`` / ``X`` 前缀），失败才回退启发。
        首次解析会打一条诊断（选中对 + 该 base 在 OKX 的全部可选对）。
        """
        base = self._base_ticker(symbol)
        inst = inst_id_for(base, self._quote, strict=True)
        if not inst and base.upper() != symbol.upper():
            # tokens.json 的 okx_symbol 登记错误（如把 SOL 写成 XSOL）：回退原始
            # symbol 再按交易所清单解析一次，别让一条脏映射打死整个标的。
            inst = inst_id_for(symbol, self._quote, strict=True)
            if inst:
                print(
                    f"[DIAG] OKX-CEX pair | {symbol}: tokens.json okx_symbol={base!r} "
                    f"在 OKX 不存在，已回退原始 symbol → {inst}",
                    file=sys.stderr, flush=True,
                )
        if not inst:
            inst = to_inst_id(base, quote=self._quote)
        logged = self.__dict__.setdefault("_pair_logged", set())
        if symbol not in logged:
            logged.add(symbol)
            ids = spot_inst_ids() or frozenset()
            avail = sorted(
                i for i in ids
                if i.startswith(f"{base.upper()}-") or i.startswith(f"X{base.upper()}-")
            )
            print(
                f"[DIAG] OKX-CEX pair | {symbol} → {inst}"
                f"（OKX 可选对: {avail or '本次未取到清单'}）",
                file=sys.stderr, flush=True,
            )
        return inst

    def quote_of(self, symbol: str) -> str:
        """该标的实际采用的计价币（从解析出的 instId 尾部取，USDT/USDC 自适应）。"""
        inst = self.inst_id_of(symbol)
        tail = inst.rsplit("-", 1)[-1].upper() if "-" in inst else self._quote
        return tail or self._quote

    def limits_of(self, symbol: str) -> dict:
        """合约规格 ``{lot_sz, min_sz, tick_sz}``（300s 缓存；失败抛异常）。"""
        inst = self.inst_id_of(symbol)
        now = time.time()
        cached = self._limits_cache.get(inst)
        if cached and now - cached[0] < self._LIMITS_TTL:
            return cached[1]
        lim = okx_spot.instrument_limits(inst)
        self._limits_cache[inst] = (now, lim)
        return lim

    def _price_of(self, symbol: str) -> float:
        """当前价（15s 缓存；0 = 取价失败 → 调用方 fail-closed）。

        直接用 OKX ``/market/ticker``（不经数据源模块，避开 pandas 依赖），
        instId 与本 broker 的 quote 一致（``-USDT`` 或 ``-USDC``）。
        """
        now = time.time()
        cached = self._price_cache.get(symbol)
        if cached and now - cached[0] < self._PRICE_TTL:
            return cached[1]
        px = 0.0
        try:
            rows = okx_sdk.check(okx_sdk.market().get_ticker(instId=self.inst_id_of(symbol)))
            row = rows[0] if isinstance(rows, list) and rows else {}
            px = _num(row.get("last"))
        except Exception as e:  # noqa: BLE001
            print(f"[DIAG] OKX-CEX price {symbol} error: {e}", file=sys.stderr, flush=True)
        if px > 0:
            self._price_cache[symbol] = (now, px)
        return px

    def _balances(self) -> dict:
        """子账号资产 → ``{ccy: {available, locked, eq_usd}}``；失败返回 {}（fail-closed）。

        未绑具体子账号（shared_pool 主 broker）时**聚合现货池**：直接查默认账号
        会落到第一个子账号（期权占用）→ 50119 → 余额 0 → portfolio BLOCK。
        现货池 = 已配置子账号 − 期权账号（``spot_account_names``，与批次账号池
        同一口径）。
        """
        if not self._sub_account:
            names = self._spot_names()
            if len(names) > 0:
                merged = self._merged_balances(names)
                self._log_cash_once(merged)
                return merged
            # 现货池为空（例如只配了一个账号且被期权占用）：退回默认账号，
            # 由 _fetch_account_balances 打 DIAG，调用方 fail-closed。
        return self._fetch_account_balances(self._sub_account)

    def _log_cash_once(self, bal: dict) -> None:
        """打印现货池现金快照（进程内一次）。

        2026-10-29：OKX 现货同一币并存 ``XXX-USDT`` / ``XXX-USDC`` 两组对，
        资金到底在哪一侧决定能否下单——必须可见，不能等「USDT 不足」才猜。
        交易账户现金为 0 时**也要打印**（否则「什么都没打」会被误当没执行），
        并顺带查资金账户（OKX 资金账户 ≠ 交易账户，钱常躺在那边）。
        """
        if self.__dict__.get("_cash_logged"):
            return
        self.__dict__["_cash_logged"] = True
        snap = {
            c: round(_num((bal.get(c) or {}).get("available")), 6)
            for c in sorted(_CASH_CURRENCIES)
            if _num((bal.get(c) or {}).get("available")) > 0
        }
        if snap:
            print(f"[DIAG] OKX-CEX 现货池现金 | {snap}", file=sys.stderr, flush=True)
            return
        held = {
            c: round(_num(v.get("total") or v.get("available")), 8)
            for c, v in bal.items()
            if _num(v.get("total")) + _num(v.get("available")) > 0
        }
        print(
            f"[DIAG] OKX-CEX 现货池现金 | ⚠️ 交易账户无可交易现金"
            f"（其它余额: {held or '无'}）",
            file=sys.stderr, flush=True,
        )
        self._log_funding_hint()

    def _log_funding_hint(self) -> None:
        """交易账户现金为 0 时查资金账户（买不了货最常见的原因）。"""
        try:
            from ..okx_sdk import funding_for

            found: dict[str, float] = {}
            for name in self._spot_names():
                creds = account_creds(name)
                if not creds:
                    continue
                payload = funding_for(creds).get_balances()
                for row in (payload.get("data") or []):
                    ccy = str(row.get("ccy") or "").upper()
                    val = _num(row.get("bal"))
                    if ccy and val > 0:
                        found[f"{name}:{ccy}"] = round(val, 8)
            print(
                f"[DIAG] OKX-CEX 资金账户 | {found or '无'}"
                f"（资金账户≠交易账户：资金账户里的币不能直接下单，需划转到交易账户）",
                file=sys.stderr, flush=True,
            )
        except Exception as e:  # noqa: BLE001 — 诊断失败不阻断
            print(f"[DIAG] OKX-CEX 资金账户查询失败 | {e}",
                  file=sys.stderr, flush=True)

    def _spot_names(self) -> list[str]:
        """现货池账号名（带 5s 进程内缓存，避免每轮反复读盘）。（""）"""
        now = time.monotonic()
        cached = self.__dict__.get("_spot_names_cache")
        if cached and now - cached[1] < 5.0:
            return cached[0]
        try:
            from nanobot_quant.okx_cex_credentials import spot_account_names

            names = spot_account_names()
        except Exception:  # noqa: BLE001
            names = []
        self.__dict__["_spot_names_cache"] = (names, now)
        return names

    def _merged_balances(self, names: list[str]) -> dict:
        """现货池多账号余额聚合（available/locked/eq_usd 逐币种求和）。"""
        out: dict[str, dict] = {}
        ok = 0
        for name in names:
            bal = self._fetch_account_balances(name)
            if not bal:
                continue
            ok += 1
            for ccy, row in bal.items():
                cur = out.setdefault(
                    ccy, {"available": 0.0, "locked": 0.0, "eq_usd": 0.0}
                )
                for k in ("available", "locked", "eq_usd"):
                    cur[k] += _num(row.get(k))
        return out if ok else {}

    def _fetch_account_balances(self, account: str) -> dict:
        try:
            bal = account_balance(account)
        except Exception as e:  # noqa: BLE001
            # 账号名是排查 50119（API key doesn't exist）的关键信息，不能只报错因
            print(
                f"[DIAG] OKX-CEX balance error | account={account or '(default)'}: {e}",
                file=sys.stderr,
                flush=True,
            )
            return {}
        out: dict[str, dict] = {}
        for row in bal.get("details") or []:
            ccy = (row.get("ccy") or "").upper()
            if not ccy:
                continue
            out[ccy] = {
                "available": _num(row.get("avail_bal")),
                "locked": _num(row.get("frozen_bal")),
                "eq_usd": _num(row.get("eq_usd")),
            }
        return out

    # ═══════════════════════════════════════════════════════════
    #  Balances & Positions
    # ═══════════════════════════════════════════════════════════

    def _get_balances_at_broker(self, quote_asset, strategy) -> tuple[float, float, float]:
        """返回 ``(cash, positions_value, total)``（计价币单位）。

        ``cash`` = 计价币可用额（``availBal``，不含挂单冻结——冻结中的资金下不了
        新单，与 CexBroker 同口径）；``positions_value`` = 非现金币按 USD 估值。
        """
        balances = self._balances()
        if not balances:
            return (0.0, 0.0, 0.0)
        quote = (getattr(quote_asset, "symbol", None) or self._quote).upper()
        cash = _num((balances.get(quote) or {}).get("available"))
        positions_value = sum(
            _num(b.get("eq_usd"))
            for ccy, b in balances.items()
            if ccy not in _CASH_CURRENCIES
        )
        return (cash, positions_value, cash + positions_value)

    def _pull_positions(self, strategy) -> list:
        """非现金币种余额即持仓；现价经数据源取（失败 0，调用方 fail-closed）。"""
        balances = self._balances()
        if not balances:
            return []
        from lumibot.entities import Asset, Position

        positions = []
        for ccy, b in balances.items():
            qty = _num(b.get("available")) + _num(b.get("locked"))
            if qty <= 0 or ccy in _CASH_CURRENCIES:
                continue
            pos = Position(
                strategy=strategy,
                asset=Asset(symbol=ccy, asset_type="crypto"),
                quantity=qty,
            )
            pos.current_price = self._price_of(ccy)
            positions.append(pos)
        return positions

    def _pull_position(self, strategy, asset):
        for p in self._pull_positions(strategy):
            if p.asset.symbol == getattr(asset, "symbol", None):
                return p
        return None

    # ═══════════════════════════════════════════════════════════
    #  Orders
    # ═══════════════════════════════════════════════════════════

    def _submit_order(self, order) -> Any:
        """现货市价单：``sz`` 取基础币数量、按 ``lotSz`` 向下取整后提交。"""
        side = str(getattr(order, "side", "") or "").lower()
        asset = getattr(order, "asset", None)
        symbol = str(getattr(asset, "symbol", "") or "")
        quantity = _num(getattr(order, "quantity", 0))
        if side not in ("buy", "sell"):
            order.set_error(f"OKX invalid side: {side!r}")
            return order
        if not symbol or quantity <= 0:
            order.set_error(f"OKX invalid order: symbol={symbol!r} qty={quantity}")
            return order

        try:
            inst = self.inst_id_of(symbol)
            lim = self.limits_of(symbol)
        except Exception as e:  # noqa: BLE001
            order.set_error(f"OKX 合约规格不可用 {symbol}: {e}")
            return order

        lot = _num(lim.get("lot_sz"))
        min_sz = _num(lim.get("min_sz"))
        sz = okx_spot.floor_step(quantity, lot) if lot > 0 else quantity
        if sz <= 0:
            order.set_error(f"OKX {inst} 下单量取整后为 0（qty={quantity} lotSz={lot}）")
            return order
        if min_sz > 0 and sz < min_sz:
            order.set_error(
                f"OKX {inst} 下单量 {sz} < minSz {min_sz}（fail-closed，不凑单）"
            )
            return order

        balances = self._balances()
        if side == "sell":
            avail = _num((balances.get(_base_of(inst)) or {}).get("available"))
            if avail <= 0:
                order.set_error(f"OKX {_base_of(inst)} 可用余额为 0（或查询失败）")
                return order
            if sz > avail:
                shrunk = okx_spot.floor_step(avail, lot) if lot > 0 else avail
                if shrunk <= 0:
                    order.set_error(
                        f"OKX {_base_of(inst)} 可用 {avail} 按 lotSz {lot} 取整后为 0"
                    )
                    return order
                print(
                    f"[DIAG] OKX-CEX SHRINK {symbol}: 计划 {sz} → 可用 {shrunk}",
                    file=sys.stderr, flush=True,
                )
                sz = shrunk
        else:
            px = self._price_of(symbol)
            quote = self.quote_of(symbol)
            cash = _num((balances.get(quote) or {}).get("available"))
            if px <= 0:
                order.set_error(f"OKX {symbol} 取价失败（fail-closed，不盲下单）")
                return order
            need = sz * px
            if need > cash * 1.001:  # 0.1% 容差（价格瞬时波动）
                order.set_error(
                    f"OKX {quote} 不足：需 {need:.4f}，可用 {cash:.4f}"
                    f"（instId={inst}；现货池现金：" + ", ".join(
                        f"{c}={_num((balances.get(c) or {}).get('available')):.4f}"
                        for c in sorted(_CASH_CURRENCIES)
                        if _num((balances.get(c) or {}).get("available")) > 0
                    ) + "）"
                )
                return order

        try:
            req = okx_spot.strict_order(
                self._creds,
                inst_id=inst,
                side=side,
                sz=sz,
                tgt_ccy="base_ccy",
                tag="td-live",
            )
        except Exception as e:  # noqa: BLE001
            order.set_error(f"OKX 下单失败: {e}")
            return order

        oid = req["ord_id"]
        res = okx_spot.poll_fill(self._creds, inst, oid)
        status = str(res.get("status") or "unknown")
        print(
            f"OKX-CEX BROKER DIAG | submit {side} {sz} {symbol}@{inst} "
            f"oid={oid[:12]} status={status} filled={res.get('acc_fill_sz')} "
            f"avg={res.get('avg_px')} timed_out={res.get('timed_out')}",
            file=sys.stderr, flush=True,
        )

        meta = {
            "symbol": symbol, "inst": inst, "side": side,
            "quantity": sz, "ts": time.time(),
        }
        if status == "filled":
            order.set_filled()
            # lumibot v4.5.78 的 set_filled 不更新 status，手动同步
            order.status = "fill"
            cex = order.custom_params.get("cex") or {}
            cex["avg_price"] = _num(res.get("avg_px"))
            cex["filled"] = _num(res.get("acc_fill_sz"))
            cex["ord_id"] = oid
            order.custom_params["cex"] = cex
            meta.update({"avg_price": _num(res.get("avg_px")),
                         "filled": _num(res.get("acc_fill_sz"))})
            self._tracked[oid] = meta
        elif status == "cancelled":
            order.set_error(f"OKX order {oid} cancelled（零成交或部分成交后撤销）")
            meta.update({"filled": _num(res.get("acc_fill_sz"))})
            self._tracked[oid] = meta
        else:
            # pending / 超时：保持已提交状态，由 _pull_broker_order 复检
            self._tracked[oid] = meta
        return order

    def cancel_order(self, order) -> None:
        """撤销未成交剩余（OKX 支持撤单）。"""
        oid = str(getattr(order, "identifier", "") or "")
        info = self._tracked.get(oid, {})
        inst = info.get("inst", "")
        if not oid or not inst:
            return
        try:
            from .. import okx_sdk

            okx_sdk.check(okx_sdk.trade_for(self._creds).cancel_order(
                instId=inst, ordId=oid))
        except Exception as e:  # noqa: BLE001
            logger.debug("cancel_order %s error: %s", oid, e)

    def _modify_order(self, order, limit_price=None, stop_price=None, quantity=None):
        """市价单不可改单（no-op）。"""
        return

    # ═══════════════════════════════════════════════════════════
    #  Order Reconciliation
    # ═══════════════════════════════════════════════════════════

    def _pull_broker_order(self, identifier: str) -> Any:
        info = self._tracked.get(identifier, {})
        inst = info.get("inst", "")
        if not inst:
            return None
        try:
            res = okx_spot.query_order(self._creds, inst, identifier)
        except Exception as e:  # noqa: BLE001
            print(f"[DIAG] OKX-CEX query order error {identifier}: {e}",
                  file=sys.stderr, flush=True)
            res = {"status": "submitted"}
        return self._parse_broker_order(
            {
                "id": identifier,
                "status": res.get("status") or "submitted",
                "filled": _num(res.get("acc_fill_sz")),
                "avg_price": _num(res.get("avg_px")),
                "symbol": info.get("symbol", ""),
                "side": info.get("side", "buy"),
                "quantity": info.get("quantity", 0),
            },
            strategy_name=None,
            strategy_object=None,
        )

    def _parse_broker_order(self, response: dict, strategy_name, strategy_object) -> Any:
        from lumibot.entities import Asset, Order

        oid = str(response.get("id") or "")
        status = str(response.get("status") or "submitted")
        symbol = str(response.get("symbol") or "")
        side = str(response.get("side") or "buy")
        quantity = _num(response.get("quantity"))
        order = Order(
            strategy=strategy_name or "okx_cex",
            identifier=oid,
            asset=Asset(symbol=symbol, asset_type="crypto") if symbol else None,
            quantity=quantity,
            side=side,
            status=status,
        )
        if status == "filled":
            order.set_filled()
            order.status = "fill"
        elif status == "cancelled":
            order.set_canceled()
        return order

    def _pull_broker_all_orders(self) -> list:
        return []

    # ═══════════════════════════════════════════════════════════
    #  Streaming（轮询式，与 CexBroker / OnchainOSBroker 同构）
    # ═══════════════════════════════════════════════════════════

    def _get_stream_object(self):
        return None

    def _register_stream_events(self):
        pass

    def _run_stream(self):
        pass

    def get_historical_account_value(self):
        return {}
