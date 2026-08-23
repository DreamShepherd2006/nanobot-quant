"""Lumibot Broker that simulates Gate CEX spot fills on historical bars.

方案 B 历史回测（docs/quant-system.md 第二十五章）的模拟撮合 broker：
- 与实盘 CexBroker 语义对齐：slot 级资金、手续费从所得币扣（买扣 base、
  卖扣 quote）、min_quote 金额 fail-closed、成交后 ``set_filled()`` +
  ``status="fill"``（lumibot v4.5.78 set_filled 不更新 status）
- 撮合确定性：按当前 bar 收盘价成交（± slippage），无网络、无真实资金
- 每 slot 一个实例（对应实盘每 slot 一个 CexBroker），账本独立 = slot 级
  资金隔离；初始资金是纯模拟参数（默认 100U/slot），不碰真实子账号

Parameters:
    initial_quote: 每 slot 初始 quote 资金（USDT），默认 100.0（纯模拟，
        10–10000 可配）。收益率分母 = Σ slot initial_quote。
    fee_rate: 交易成本（Gate taker），默认 0.001（0.1%），每笔必扣。
    slippage: 成交价偏差（默认 0.0）：买 ×(1+slippage)、卖 ×(1−slippage)。
    min_quote_amount: 最小订单金额（默认 3.0，Gate 服务端规则），0=禁用。
    price_source: callable(symbol) → 当前 bar 收盘价（回测数据源游标）。
    tokens_json: tokens.json 条目（symbol/gate_symbol 映射，pair 用）。
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable, Optional

from lumibot.brokers import Broker

from nanobot_quant.gate_credentials import gate_pair

# Currencies treated as cash — everything else counts as a position.
_CASH_CURRENCIES = frozenset({"USDT", "USDC", "USDG", "USD", "TUSD"})


class _DummyDataSource:
    """Placeholder so Broker.__init__ does not require a real data source."""

    def __init__(self, *args, **kwargs):
        pass

    def get_chains(self, asset=None, quote=None):
        return {}


class BacktestBroker(Broker):
    """Deterministic bar-fill simulation broker for historical replay.

    Fill model (2026-08-23 拍板，docs/quant-system.md §25.3):
        buy  : 花费 quote = quantity × px × (1 + slippage)
               到账 base  = quantity × (1 − fee_rate)     （手续费从所得币扣）
               avg_price  = 花费 / 到账                   （含手续费摊薄，对齐 Gate avg_deal_price）
        sell : 到账 quote = quantity × px × (1 − slippage) × (1 − fee_rate)
               （手续费从所得 quote 扣；avg_price = px）
        资金不足 / min_quote 不足 / 无价格 → order.set_error（fail-closed，
        与实盘 CexBroker 语义一致——策略打 TD BATCH BUY FAIL / SLOT SKIP）。
    """

    SOURCE = "backtest"

    def __init__(
        self,
        initial_quote: float = 100.0,
        fee_rate: float = 0.001,
        slippage: float = 0.0,
        min_quote_amount: float = 3.0,
        price_source: Optional[Callable[[str], float]] = None,
        tokens_json: Optional[list[dict]] = None,
        **kwargs,
    ):
        if "data_source" not in kwargs:
            kwargs["data_source"] = _DummyDataSource()
        super().__init__(**kwargs)
        # Crypto is a 24/7 continuous market: keeps the StrategyExecutor loop
        # running (same as CexBroker).
        self.market = "24/7"
        self._cash = float(initial_quote)           # quote 币（USDT）可用
        self._positions: dict[str, float] = {}      # symbol → base 持仓量
        self._fee_rate = float(fee_rate)
        self._slippage = float(slippage)
        self._min_quote_amount = float(min_quote_amount)
        self._price_source = price_source
        self._tokens_json = tokens_json or []
        self._tracked: dict[str, dict] = {}         # oid → meta
        self._order_seq = 0

    # ═══════════════════════════════════════════════════════════
    #  Helpers
    # ═══════════════════════════════════════════════════════════

    def _price_of(self, symbol: str) -> float:
        """Current bar close price via the replay data-source cursor.

        0.0 on failure (fail-closed, consumers refuse to trade on 0) —
        mirrors CexBroker semantics, but deterministic and offline.
        """
        try:
            if self._price_source is not None:
                px = self._price_source(symbol)
                return float(px) if px and px > 0 else 0.0
        except Exception as exc:  # noqa: BLE001
            print(f"[DIAG] Backtest price error {symbol}: {exc}",
                  file=sys.stderr, flush=True)
        return 0.0

    def _balances(self) -> dict[str, dict]:
        """Available+locked balances keyed by currency (this slot's ledger)."""
        out: dict[str, dict] = {
            "USDT": {"available": self._cash, "locked": 0.0},
        }
        for cur, qty in self._positions.items():
            out[cur] = {"available": qty, "locked": 0.0}
        return out

    def snapshot(self) -> dict:
        """Net-value snapshot for the replay driver (mark-to-market).

        positions_value uses the current bar close via ``_price_of`` —
        same pricing the strategy sees, so per-bar net value is consistent
        with every decision taken on that bar.
        """
        positions_value = 0.0
        for cur, qty in self._positions.items():
            px = self._price_of(cur)
            positions_value += qty * px
        return {
            "cash": self._cash,
            "positions": dict(self._positions),
            "positions_value": positions_value,
            "total": self._cash + positions_value,
        }

    # ═══════════════════════════════════════════════════════════
    #  Order Execution
    # ═══════════════════════════════════════════════════════════

    def submit_order(self, order) -> Any:
        """Submit entry-point (mirrors lumibot ``Broker.submit_order``).

        Synchronous fill via ``_submit_order``; exceptions are captured into
        ``order.set_error(...)`` + ``status="error"`` (fail-closed) so the
        strategy's post-submit checks (order.error / is_filled) behave exactly
        like the live CexBroker path.
        """
        try:
            order = self._submit_order(order)
        except Exception as exc:  # noqa: BLE001
            order.set_error(str(exc))
            order.status = "error"
        return order

    def _submit_order(self, order) -> Any:
        """Simulate an immediate market fill on the current bar close.

        Deterministic: no network, no polling, no pending state. Fail-closed
        checks mirror CexBroker: unsupported side / non-positive quantity /
        no price / min_quote / insufficient balance.
        """
        symbol = order.asset.symbol
        side = str(order.side).lower()
        quantity = float(order.quantity)

        if side not in ("buy", "sell"):
            order.set_error(f"Unsupported side: {side} (only buy/sell)")
            return order
        if quantity <= 0:
            order.set_error(f"Invalid quantity: {quantity} ({symbol})")
            return order

        px = self._price_of(symbol)
        if px <= 0:
            order.set_error(f"Backtest cannot fill {side} {symbol}: no price")
            return order

        pair = gate_pair(symbol, self._tokens_json)
        min_quote = self._min_quote_amount
        if min_quote > 0 and quantity * px < min_quote:
            order.set_error(
                f"Backtest {pair} below min order amount {min_quote:g} USDT "
                f"(qty {quantity} x {px:.2f} = ${quantity * px:.2f})"
            )
            return order

        slippage = self._slippage
        if side == "buy":
            # 买：花费 quote = quantity × px × (1+slippage)，手续费从所得币扣
            cost = quantity * px * (1.0 + slippage)
            if self._cash < cost:
                order.set_error(
                    f"Backtest insufficient balance: need {cost:.4f} USDT, "
                    f"have {self._cash:.4f}"
                )
                return order
            self._cash -= cost
            received = quantity * (1.0 - self._fee_rate)
            self._positions[symbol] = self._positions.get(symbol, 0.0) + received
            avg = cost / received if received > 0 else px
        else:
            # 卖：到账 quote = quantity × px × (1−slippage) × (1−fee_rate)
            bal = self._positions.get(symbol, 0.0)
            if bal < quantity:
                order.set_error(
                    f"Backtest insufficient {symbol}: have {bal:.8f}, need {quantity:.8f}"
                )
                return order
            self._positions[symbol] = bal - quantity
            if self._positions[symbol] <= 1e-12:
                del self._positions[symbol]
            proceeds = quantity * px * (1.0 - slippage) * (1.0 - self._fee_rate)
            self._cash += proceeds
            avg = px

        oid = f"bt{self._order_seq}"
        self._order_seq += 1
        order.set_identifier(oid)
        order.custom_params = order.custom_params or {}
        order.custom_params["cex"] = {
            "pair": pair,
            "avg_price": avg,  # 含手续费摊薄——策略 _cex_avg_price 读它算滑点
        }
        order.set_filled()
        order.status = "fill"  # lumibot v4.5.78 set_filled 不更新 status，手动同步
        self._tracked[oid] = {
            "symbol": symbol,
            "pair": pair,
            "side": side,
            "quantity": quantity,
            "filled": quantity,
            "avg_price": avg,
            "ts": time.time(),
        }
        return order

    def cancel_order(self, order) -> None:
        """No-op: market fills are immediate in replay — nothing to cancel."""

    def _modify_order(self, order, limit_price=None, stop_price=None, quantity=None):
        """No-op: market orders cannot be amended."""
        return

    # ═══════════════════════════════════════════════════════════
    #  Balances & Positions
    # ═══════════════════════════════════════════════════════════

    def _get_balances_at_broker(self, quote_asset, strategy) -> tuple[float, float, float]:
        """Return (cash, positions_value, total) in the quote currency (USDT)."""
        cash = self._cash
        positions_value = 0.0
        for cur, qty in self._positions.items():
            positions_value += qty * self._price_of(cur)
        return (cash, positions_value, cash + positions_value)

    def _pull_positions(self, strategy) -> list:
        """Positions = non-cash balances; priced via the replay cursor."""
        from lumibot.entities import Asset, Position
        positions = []
        for cur, qty in self._positions.items():
            if qty <= 0 or cur in _CASH_CURRENCIES:
                continue
            pos = Position(
                strategy=strategy,
                asset=Asset(symbol=cur, asset_type="crypto"),
                quantity=qty,
            )
            pos.current_price = self._price_of(cur)
            positions.append(pos)
        return positions

    def _pull_position(self, strategy, asset):
        positions = self._pull_positions(strategy)
        for p in positions:
            if p.asset.symbol == asset.symbol:
                return p
        return None

    # ═══════════════════════════════════════════════════════════
    #  Order Reconciliation
    # ═══════════════════════════════════════════════════════════

    def _query_order(self, order_id: str, pair: str) -> tuple[str, float, float, float]:
        """Replay fills are immediate → any tracked order is filled."""
        info = self._tracked.get(order_id)
        if not info:
            return "failed", 0.0, 0.0, 0.0
        return "filled", info.get("filled", 0.0), 0.0, info.get("avg_price", 0.0)

    def _pull_broker_order(self, identifier: str) -> Any:
        """Reconstitute a filled lumibot Order from a tracked replay fill."""
        info = self._tracked.get(identifier)
        if not info:
            return None
        return self._parse_broker_order(
            {"id": identifier, "status": "filled", "filled": info.get("filled", 0.0),
             "left": 0.0, "avg_price": info.get("avg_price", 0.0),
             "symbol": info.get("symbol", ""),
             "side": info.get("side", "buy"), "quantity": info.get("quantity", 0)},
            strategy_name=None, strategy_object=None,
        )

    def _parse_broker_order(self, response: dict, strategy_name, strategy_object) -> Any:
        from lumibot.entities import Asset, Order
        oid = str(response.get("id") or "")
        status = str(response.get("status") or "submitted")
        symbol = str(response.get("symbol") or "")
        side = str(response.get("side") or "buy")
        quantity = float(response.get("quantity") or 0)
        order = Order(
            strategy=strategy_name or "backtest",
            identifier=oid,
            asset=Asset(symbol=symbol, asset_type="crypto") if symbol else None,
            quantity=quantity,
            side=side,
            status=status,
        )
        if status == "filled":
            order.set_filled()
            order.status = "fill"  # lumibot v4.5.78 set_filled 不更新 status，手动同步
        elif status == "cancelled":
            order.set_canceled()
        return order

    def _pull_broker_all_orders(self) -> list:
        return []

    # ═══════════════════════════════════════════════════════════
    #  Streaming (poll-based, matching CexBroker pattern)
    # ═══════════════════════════════════════════════════════════

    def _get_stream_object(self):
        return None

    def _register_stream_events(self):
        pass

    def _run_stream(self):
        pass

    def get_historical_account_value(self):
        return {}
