"""Batch position management — 子钱包分批（第一版，2026-08-09 设计定稿）.

每个 TD lot 绑定一个 Agentic Wallet 子账户（slot）。本模块负责批次
状态机、FIFO/LIFO 平仓选择、独立止损/止盈检查与持久化——策略层
（TdSequentialStrategy live 分批路径）消费本模块决定买卖数量与目标。

资金模型 B（子钱包独立充值）：本模块不做任何转账，只维护台账。
链上余额为真相：卖出量以链上实际余额为准（由策略/broker 层处理），
本模块的 lot.qty 仅作计划与对账。

数据文件：``{data_root}/legion/credentials/batches.json``（与
okx.json / tokens.json / exec_params.json 同目录，Factory Rebuild 不丢）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

#: Slot 状态
AVAILABLE = "available"
OPEN = "open"

#: 平仓顺序
EXIT_ORDERS: tuple[str, ...] = ("fifo", "lifo")


def batches_path() -> Path:
    """持久化路径（与 exec_params.json 同一 credentials 目录）。"""
    for root in ("/data", "/mnt/workspace"):
        d = Path(root) / "legion" / "credentials"
        try:
            if d.exists():
                return d / "batches.json"
        except OSError:
            continue
    return Path.home() / ".batches.json"


class BatchManager:
    """批次台账：slot 列表 + 状态机 + 选择/退出逻辑。"""

    def __init__(
        self,
        symbol: str,
        account_ids: list[str],
        path: Optional[Path | str] = None,
    ) -> None:
        self.symbol = symbol
        self.path = Path(path) if path else batches_path()
        self.slots: list[dict[str, Any]] = []
        self._init_slots(account_ids)

    # ── 初始化 ──────────────────────────────────────────────────────
    def _init_slots(self, account_ids: list[str]) -> None:
        """按给定子钱包 account_id 列表建立 slot（按顺序）。"""
        self.slots = [
            {
                "slot": i + 1,
                "account_id": aid,
                "status": AVAILABLE,
                "lot": None,
            }
            for i, aid in enumerate(account_ids)
        ]

    # ── 持久化 ──────────────────────────────────────────────────────
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"symbol": self.symbol, "slots": self.slots},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, path: Optional[Path | str] = None) -> Optional["BatchManager"]:
        """从磁盘加载；文件缺失/损坏 → None。"""
        p = Path(path) if path else batches_path()
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            bm = cls.__new__(cls)
            bm.symbol = raw.get("symbol", "")
            bm.path = p
            bm.slots = raw.get("slots", [])
            return bm
        except (OSError, ValueError, KeyError):
            return None

    # ── 查询 ────────────────────────────────────────────────────────
    def available_slots(self) -> list[dict[str, Any]]:
        return [s for s in self.slots if s["status"] == AVAILABLE]

    def open_slots(self) -> list[dict[str, Any]]:
        return [s for s in self.slots if s["status"] == OPEN]

    def next_buy_slot(self) -> Optional[dict[str, Any]]:
        """BUY 目标：slot 顺序第一个 available（轮转复用）。"""
        return next((s for s in self.slots if s["status"] == AVAILABLE), None)

    def pick_exit_slot(self, order: str = "fifo") -> Optional[dict[str, Any]]:
        """平仓目标：open 批次按 exit_order 选一个。

        fifo：entry_time 最早（先买先卖）；lifo：最新。
        """
        open_lots = [
            s for s in self.slots
            if s["status"] == OPEN and s["lot"] is not None
        ]
        if not open_lots:
            return None
        key = lambda s: s["lot"]["entry_time"]  # noqa: E731
        open_lots.sort(key=key, reverse=(order == "lifo"))
        return open_lots[0]

    # ── 状态机 ──────────────────────────────────────────────────────
    def open_lot(
        self, qty: float, entry_price: float,
        entry_time: Optional[str] = None,
        slot: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """BUY：占用一个 available slot 并记录 lot。返回该 slot。"""
        target = None
        if slot is not None:
            target = next((s for s in self.slots if s["slot"] == slot), None)
            if target is None or target["status"] != AVAILABLE:
                return None
        else:
            target = self.next_buy_slot()
        if target is None:
            return None
        target["status"] = OPEN
        target["lot"] = {
            "qty": float(qty),
            "entry_price": float(entry_price),
            "entry_time": entry_time or time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        return target

    def close_lot(self, slot: int) -> Optional[dict[str, Any]]:
        """平仓：清空 lot，slot 立即回收为 available。返回原 lot（供卖出量）。"""
        target = next((s for s in self.slots if s["slot"] == slot), None)
        if target is None or target["status"] != OPEN:
            return None
        lot = target["lot"]
        target["lot"] = None
        target["status"] = AVAILABLE
        return lot

    # ── 退出条件（独立止损/止盈，每批独立计算）────────────────────────
    def check_exit(
        self,
        price: float,
        stop_loss_pct: float = 0.10,
        take_profit_pct: float = 0.0,
        order: str = "fifo",
    ) -> list[dict[str, Any]]:
        """逐批检查退出条件，返回**按 exit_order 排序**的待平仓 slot 列表。

        - 浮亏 ≥ stop_loss_pct → 止损命中
        - take_profit_pct > 0 且浮盈 ≥ take_profit_pct → 止盈命中
        每批独立（各自 entry_price 对比同一当前价）。
        """
        hits: list[dict[str, Any]] = []
        for s in self.open_slots():
            lot = s["lot"]
            if lot is None or lot["entry_price"] <= 0:
                continue
            pnl = (price - lot["entry_price"]) / lot["entry_price"]
            reason = None
            if pnl <= -stop_loss_pct:
                reason = f"stop_loss: pnl={pnl:.2%}"
            elif take_profit_pct > 0 and pnl >= take_profit_pct:
                reason = f"take_profit: pnl={pnl:.2%}"
            if reason:
                s["_exit_reason"] = reason
                hits.append(s)
        hits.sort(
            key=lambda s: s["lot"]["entry_time"],
            reverse=(order == "lifo"),
        )
        return hits

    # ── 展示 ────────────────────────────────────────────────────────
    def summarize(self, price: Optional[float] = None) -> list[dict[str, Any]]:
        """WebUI 批次状态卡片数据（每批：slot/account/status/lot/浮盈%）。"""
        out: list[dict[str, Any]] = []
        for s in self.slots:
            item = {
                "slot": s["slot"],
                "account_id": s["account_id"],
                "status": s["status"],
                "qty": None,
                "entry_price": None,
                "entry_time": None,
                "pnl_pct": None,
                "exit_reason": s.pop("_exit_reason", None),
            }
            if s["lot"] is not None:
                item["qty"] = s["lot"]["qty"]
                item["entry_price"] = s["lot"]["entry_price"]
                item["entry_time"] = s["lot"]["entry_time"]
                if price and s["lot"]["entry_price"]:
                    item["pnl_pct"] = (
                        (price - s["lot"]["entry_price"]) / s["lot"]["entry_price"]
                    )
            out.append(item)
        return out


def ensure_batches(td_batches: int, symbol: str) -> tuple[Optional[BatchManager], str]:
    """WebUI 保存 td_batches 时调用：确保子钱包数量 ≥ td_batches 并建/复用批次映射。

    - 子钱包不足 → ``wallet add`` 补足（add 会自动切换活跃账户，
      补足后 switch 回原活跃账户）。
    - 已有 batches.json 且 symbol 一致 → 复用（保留 open 批次，不重建）。
    - 返回 ``(BatchManager | None, 日志信息)``。
    """
    from .tools.tools_wallet import wallet_accounts, wallet_add, wallet_switch

    bm = BatchManager.load()
    if bm is not None and bm.symbol == symbol and bm.slots:
        return bm, f"复用已有批次台账（{symbol}，{len(bm.slots)} slots）"

    acc = wallet_accounts()
    if acc.get("status") != "ok":
        return None, f"wallet_accounts 失败: {acc.get('error')}"
    data = acc.get("data", {}) or {}
    ids = [a["account_id"] for a in data.get("accounts", [])]
    prev_active = data.get("selected_account_id")
    created = 0
    while len(ids) < td_batches:
        r = wallet_add()
        if r.get("status") != "ok":
            break
        created += 1
        acc = wallet_accounts()
        if acc.get("status") != "ok":
            break
        ids = [
            a["account_id"]
            for a in (acc.get("data", {}) or {}).get("accounts", [])
        ]
    if created and prev_active:
        try:
            wallet_switch(prev_active)
        except Exception:  # noqa: BLE001 — 切换回原账户失败不阻断
            pass
    if len(ids) < td_batches:
        return None, (
            f"子钱包不足：现有 {len(ids)}，要求 {td_batches} "
            "（wallet add 失败或达到 50 上限）"
        )
    bm = BatchManager(symbol=symbol, account_ids=ids[:td_batches])
    bm.save()
    return bm, f"批次初始化完成：{len(bm.slots)} slots（新建 {created}）"
