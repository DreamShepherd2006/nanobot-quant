"""Lumibot Broker that executes trades via onchainos DEX aggregator.

Implements all 13 abstract methods of ``lumibot.brokers.Broker``.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from nanobot_quant.onchainos_cli import (
    confirm_swap_onchain,
    get_token_price,
    get_wallet_balance,
    resolve_token_address,
    swap_execute,
    swap_status,
    WSOL_ADDR,
)
from nanobot_quant.onchainos_errors import lookup as err_lookup
from nanobot_quant.tokens_store import token_chain
from nanobot_quant.tools.tools_wallet import get_active_wallet_address
from lumibot.brokers import Broker

logger = logging.getLogger("nanobot_quant.brokers.onchainos")


class OnchainOSBroker(Broker):
    """Lumibot Broker that routes orders through onchainos DEX aggregator.

    Parameters:
        tokens_json: User-configured token mappings from tokens.json.
            Each entry: ``{"symbol": "...", "address": "...", "chain": "solana"}``.
        slippage: Default slippage tolerance (0.01 = 1%).
        sol_buffer_pct: Extra SOL buffer on buy orders to absorb price movement
            between quote and execution (default 0.05 = 5%).
    """

    def __init__(
        self,
        tokens_json: list[dict] | None = None,
        slippage: str = "0.01",
        sol_buffer_pct: float = 0.05,
        **kwargs,
    ):
        if "data_source" not in kwargs:
            kwargs["data_source"] = _DummyDataSource()
        super().__init__(**kwargs)
        # Crypto is a 24/7 continuous market: keeps the StrategyExecutor loop
        # running for execution_mode="loop" (docs/quant-system.md §15.4).
        self.market = "24/7"
        self._tokens_json = tokens_json or []
        self._slippage = slippage
        self._sol_buffer_pct = sol_buffer_pct
        self._tracked: dict[str, dict] = {}  # tx_hash → order meta

    @staticmethod
    def _format_err(prefix: str, result: dict | None = None) -> str:
        """Build a human-readable error string with code lookup."""
        if not result:
            return prefix
        # Try to decode any error code in the response
        detail = err_lookup(result)
        if detail and detail != str(result):
            return f"{prefix}: {detail}"
        # CLI error envelope lives in stdout on failure ({"ok": false, "error": ...})
        err_msg = (
            result.get("error", "")
            or (result.get("_stdout_parsed") or {}).get("error", "")
            or (result.get("_stderr_parsed") or {}).get("error", "")
            or result.get("_stderr", "")
        )
        if err_msg:
            clipped = err_msg.strip()[-300:]
            return f"{prefix}: {clipped}"
        return prefix

    # ═══════════════════════════════════════════════════════════════
    #  Order Execution
    # ═══════════════════════════════════════════════════════════════

    def _submit_order(self, order) -> Any:
        """Submit a market order via onchainos swap.

        Flow:
        1. Resolve token addresses (user config → CLI fallback)
        2. Determine from_amount (exact for sells, estimated for buys)
        3. ``onchainos swap execute`` → txHash
        4. Update order with txHash + filled status
        """
        symbol = order.asset.symbol
        side = order.side.lower()
        # Keep decimals: explicit quantity (e.g. 0.05 CRCLX) must not be
        # truncated to 0 — int() turned 0.05 into 0 → swap aborted with
        # "--readable-amount 0.000000 is too small for this token".
        quantity = float(order.quantity)

        if side not in ("buy", "sell"):
            order.set_error(f"Unsupported side: {side} (only buy/sell)")
            return order

        # ── token resolution ──────────────────────────────────
        quote_symbol = order.quote.symbol if order.quote else "USDC"

        if side == "buy":
            from_symbol, to_symbol = quote_symbol, symbol
        else:
            from_symbol, to_symbol = symbol, quote_symbol

        from_addr = resolve_token_address(from_symbol, self._tokens_json)
        to_addr = resolve_token_address(to_symbol, self._tokens_json)

        if not from_addr or not to_addr:
            msg = self._format_err(
                f"Cannot resolve addresses: {from_symbol}→{to_symbol}"
            )
            order.set_error(msg)
            logger.error(msg)
            return order

        # ── amount calculation ─────────────────────────────────
        if side == "sell":
            from_amount = str(quantity)
        else:
            # Buy: estimate quote needed via market price + buffer.
            # Prices follow the target chain (from/to may live on the
            # same chain; pass chain so pricing hits the right one).
            chain_b = token_chain(symbol, self._tokens_json)
            sol_price = get_token_price(from_symbol, self._tokens_json,
                                        chain=chain_b) or 1.0
            token_price = get_token_price(to_symbol, self._tokens_json,
                                          chain=chain_b) or 0.0
            if token_price <= 0:
                order.set_error(self._format_err(f"Cannot get price for {symbol}"))
                return order
            sol_needed = (quantity * token_price / sol_price) * (1 + self._sol_buffer_pct)
            from_amount = f"{sol_needed:.6f}"

        # ── execute swap ───────────────────────────────────────
        # Per-target chain from the managed gate (tokens.json entry wins,
        # default solana) — a BNB target e.g. SPCXB swaps on chain 56.
        # Global okx.json chain is no longer used: the target's own chain
        # is the single source of truth.
        chain = token_chain(symbol, self._tokens_json)
        # 报价/广播地址 = Agentic Wallet 当前活跃账户（由 API Key 会话决定），
        # 非用户个人钱包地址 — 动态从钱包会话获取，避免填错地址导致报价不准
        wallet = get_active_wallet_address(chain)
        if not wallet:
            order.set_error(
                self._format_err(
                    f"Agentic Wallet 地址不可用：请先在钱包管理中完成登录（wallet_setup）"
                )
            )
            return order
        result = swap_execute(
            from_addr, to_addr, from_amount, self._slippage,
            chain=chain, wallet=wallet,
        )
        if not result:
            order.set_error(
                self._format_err(
                    f"Swap execute failed: {side} {quantity} {symbol}@{from_amount}"
                )
            )
            return order

        # Handle CLI-level failure (non-zero exit, no valid JSON)
        if "_exit_code" in result:
            order.set_error(self._format_err(f"Swap CLI exit={result['_exit_code']}", result))
            return order

        # onchainos CLI returns {"ok": true/false, "data": {...}, "error": "..."}
        if not result.get("ok"):
            order.set_error(self._format_err("Swap rejected", result))
            return order

        data = result.get("data", result) if isinstance(result.get("data"), dict) else result
        tx_hash = data.get("swapTxHash") or data.get("txHash") or ""
        order_id = data.get("swapOrderId") or ""
        status = data.get("status", "unknown")

        # Accept any non-error status that indicates the swap was submitted.
        reject_statuses = {"error", "failed", "rejected", "canceled", "cancelled"}

        if status and status.lower() in reject_statuses:
            # Try to get a more detailed error from the data envelope
            err_detail = (
                data.get("error")
                or data.get("errorMessage")
                or data.get("msg")
                or ""
            )
            if err_detail:
                order.set_error(self._format_err(f"Swap {status}", {"error": err_detail}))
            else:
                order.set_error(self._format_err(f"Swap {status}", data))
            return order

        # ── 链上成交确认（2026-08-11）──────────────────────────────
        # swap 提交成功 ≠ 链上成交：Gas Station 广播先返回 orderId，relayer
        # 后填充链上 hash；报价阶段判定失败时返回占位 hash（曾致 RENDER
        # 3.06 假成功脱管）。以官方 `wallet history` 的 txStatus 为准：
        # SUCCESS=成交才 set_filled；ERROR/CANCELLED=失败 set_error；持续
        # PENDING=已提交待确认（不 filled 不 error），策略层台账保持 open
        # + 后续轮询补确认（fail-safe，防假成功脱管）。
        order.set_identifier(tx_hash or order_id)
        confirmed = confirm_swap_onchain(tx_hash, order_id, chain)
        if confirmed == "error":
            order.set_error(self._format_err("Swap 链上确认失败", data))
            return order
        if confirmed == "pending":
            order.custom_params["onchain_pending"] = {
                "tx_hash": tx_hash,
                "order_id": order_id,
                "chain": chain,
            }
            logger.info(
                "Swap submitted (pending on-chain confirm): tx=%s",
                (tx_hash or order_id)[:16],
            )
            return order

        # ── confirmed == "success"：链上已成交 ────────────────────
        order.set_filled()

        to_amount = float(result.get("toAmount") or 0)
        fill_price = to_amount / quantity if quantity > 0 else 0

        self._tracked[tx_hash or order_id] = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "fill_price": fill_price,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(
            "Swap filled (on-chain confirmed): %s %s %s → tx=%s price=%.4f",
            side, quantity, symbol, (tx_hash or order_id)[:16], fill_price,
        )
        return order

    # ═══════════════════════════════════════════════════════════════
    #  Balance & Positions
    # ═══════════════════════════════════════════════════════════════

    def _get_balances_at_broker(self, quote_asset, strategy) -> tuple[float, float, float]:
        """Return (cash, positions_value, total_value) in USD."""
        balances = get_wallet_balance()
        if not balances:
            print(
                "[DIAG] broker balances empty → total=0 "
                "(portfolio BLOCK 风险；见 wallet balance DIAG)",
                file=sys.stderr, flush=True,
            )
            last = getattr(self, "_last_total", 0.0)
            if last > 0:
                print(
                    f"[DIAG] broker balances: use last known total={last:.4f}",
                    file=sys.stderr, flush=True,
                )
                return (
                    getattr(self, "_last_cash", 0.0),
                    getattr(self, "_last_pos", 0.0),
                    last,
                )
            return (0.0, 0.0, 0.0)

        cash = 0.0
        positions_val = 0.0
        items = []
        for t in balances:
            try:
                # CLI v4.3.1 字段为 usdValue（老形状兼容 valueUsd）
                val = float(t.get("usdValue") or t.get("valueUsd") or 0)
            except (TypeError, ValueError):
                val = 0.0
            symb = str(t.get("symbol", "")).upper()
            items.append(f"{symb}:{val:.2f}")
            if symb == "SOL":
                cash = val
            else:
                positions_val += val

        total = cash + positions_val
        self._last_cash, self._last_pos, self._last_total = cash, positions_val, total
        print(
            f"[DIAG] broker balances: n={len(balances)} "
            f"[{', '.join(items)}] cash={cash:.4f} pos={positions_val:.4f} "
            f"total={total:.4f}",
            file=sys.stderr, flush=True,
        )
        return (cash, positions_val, total)

    def _pull_positions(self, strategy) -> list:
        """Return current positions from onchainos wallet."""
        balances = get_wallet_balance()
        if not balances:
            return []

        from lumibot.entities import Asset, Position

        positions = []
        for t in balances:
            symb = t.get("symbol", "")
            if symb.upper() == "SOL":
                continue
            try:
                bal = float(t.get("balance") or 0)
            except (TypeError, ValueError):
                bal = 0.0
            try:
                # CLI v4.3.1 价格字段为 tokenPrice（非 price）
                price = float(t.get("tokenPrice") or t.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if bal <= 0:
                continue
            # lumibot v4.5.78 Position.__init__ 不接受 current_price 参数
            # （注释明确：current_price 等属性须在构造后赋值）
            pos = Position(
                strategy=strategy,
                asset=Asset(symbol=symb, asset_type="crypto"),
                quantity=bal,
            )
            pos.current_price = price
            positions.append(pos)
        return positions

    def _pull_position(self, strategy, asset) -> Any:
        """Return position for a specific asset, or None."""
        for pos in self._pull_positions(strategy):
            if pos.asset.symbol.upper() == asset.symbol.upper():
                return pos
        return None

    # ═══════════════════════════════════════════════════════════════
    #  Order Tracking
    # ═══════════════════════════════════════════════════════════════

    def _pull_broker_order(self, identifier: str) -> Any:
        """Look up order status by txHash."""
        from lumibot.entities import Asset, Order

        info = self._tracked.get(identifier, {})
        result = swap_status(identifier)

        status = "filled"
        if result:
            raw_status = result.get("status", "success")
            if raw_status in ("pending", "submitted"):
                status = "submitted"
            elif raw_status != "success":
                status = "cancelled"

        return Order(
            strategy=None,
            identifier=identifier,
            asset=Asset(symbol=info.get("symbol", ""), asset_type="crypto"),
            quantity=info.get("quantity", 0),
            side=info.get("side", "buy"),
            status=status,
        )

    def _parse_broker_order(
        self, response, strategy_name: str, strategy_object
    ) -> Any:
        """Parse swap result dict into a Lumibot Order."""
        from lumibot.entities import Asset, Order

        tx_hash = response.get("swapTxHash") or response.get("txHash", "")
        return Order(
            strategy=strategy_object,
            identifier=tx_hash,
            asset=Asset(
                symbol=response.get("symbol", ""), asset_type="crypto"
            ),
            quantity=int(response.get("quantity", 0)),
            side=response.get("side", "buy"),
            status="filled",
        )

    def _pull_broker_all_orders(self) -> list:
        """On-chain DEX has no open order book — swaps are atomic."""
        return []

    # ═══════════════════════════════════════════════════════════════
    #  No-ops (chain constraints)
    # ═══════════════════════════════════════════════════════════════

    def cancel_order(self, order) -> None:
        """Solana swaps are atomic — cannot cancel once submitted."""
        logger.debug("cancel_order no-op for onchainos")
        return None

    def _modify_order(
        self,
        order,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        quantity: Optional[float] = None,
    ) -> None:
        """Solana swaps cannot be modified once submitted."""
        logger.debug("_modify_order no-op for onchainos")
        return None

    # ═══════════════════════════════════════════════════════════════
    #  Streaming (not used — strategy uses timer/sleep loop)
    # ═══════════════════════════════════════════════════════════════

    def _get_stream_object(self):
        return None

    def _register_stream_events(self):
        pass

    def _run_stream(self):
        pass

    # ═══════════════════════════════════════════════════════════════
    #  Historical (not applicable)
    # ═══════════════════════════════════════════════════════════════

    def get_historical_account_value(self) -> dict:
        return {}


class _DummyDataSource:
    """Minimal data source stub for the Lumibot broker constructor."""
    def __init__(self):
        self._timestep = "minute"

    def get_last_price(self, *args, **kwargs) -> None:
        return None

    def get_historical_prices(self, *args, **kwargs):
        import pandas as pd
        return pd.DataFrame()

