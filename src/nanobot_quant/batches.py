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


def batches_path(symbol: Optional[str] = None, channel: Optional[str] = None) -> Path:
    """持久化路径（与 exec_params.json 同一 credentials 目录）。

    channel + symbol → ``batches.{channel}.{symbol}.json``（通道隔离，
    2026-08-17 拍板：DEX 与 CEX 台账独立，切通道不复用对方台账——
    此前同路径切换导致双向覆盖/快照堆积）；symbol 无 channel 时
    返回 ``batches.{symbol}.json``（旧格式，迁移用）；None 时返回旧式
    单文件路径（兼容/迁移用）。
    """
    if channel and symbol:
        fname = f"batches.{channel}.{symbol}.json"
    elif symbol:
        fname = f"batches.{symbol}.json"
    else:
        fname = "batches.json"
    for root in ("/data", "/mnt/workspace"):
        d = Path(root) / "legion" / "credentials"
        try:
            if d.exists():
                return d / fname
        except OSError:
            continue
    if symbol:
        return Path.home() / f".batches.{symbol}.json"
    return Path.home() / ".batches.json"


#: 历史台账默认归属通道（2026-08-17 拍板：旧批次全是 DEX 时代创建的）
_LEGACY_CHANNEL = "okx_dex"


def migrate_legacy_batches() -> None:
    """旧式单文件 batches.json → per-symbol 归档（保留历史台账，归 okx_dex）。

    读旧文件取出其 symbol，rename 到 ``batches.okx_dex.{symbol}.json``。
    目标已存在时不覆盖（新文件优先）。幂等：无旧文件时 no-op。
    """
    legacy = batches_path()  # 无 symbol → 旧式单文件路径
    if not legacy.exists():
        return
    try:
        old = BatchManager.load(path=legacy)
        if old is None or not old.symbol or not old.slots:
            return
        target = batches_path(old.symbol, _LEGACY_CHANNEL)
        if target.exists():
            return
        os.replace(legacy, target)
    except (OSError, ValueError):
        return


def _migrate_channel_legacy() -> None:
    """无通道前缀的 ``batches.{symbol}.json`` → ``batches.okx_dex.{symbol}.json``。

    通道隔离前的旧 per-symbol 台账没有通道维度，一律归历史默认通道
    okx_dex（2026-08-17 拍板）；目标已存在不覆盖。幂等：源不存在时 no-op。
    """
    import glob
    for p in glob.glob(str(batches_path("*"))):
        src = Path(p)
        if src.name.startswith("batches.") and src.suffix == ".json":
            sym = src.name[len("batches."):-len(".json")]
            if "." in sym:
                continue  # 已带通道前缀（batches.gate.CRCLX.json）——跳过
            tgt = batches_path(sym, _LEGACY_CHANNEL)
            if tgt.exists():
                continue
            try:
                os.replace(src, tgt)
            except OSError:
                continue


def _load_or_migrate(
    symbol: str, channel: Optional[str] = None
) -> Optional["BatchManager"]:
    """通道化 per-symbol 加载；缺失时先迁移旧格式再试一次。

    旧格式（无通道前缀 / 单文件）一律归 okx_dex 命名空间，不污染
    当前通道（gate 通道请求时 DEX 台账原地保留，不迁移到 gate）。
    """
    bm = BatchManager.load(symbol=symbol, channel=channel)
    if bm is not None and bm.slots:
        return bm
    migrate_legacy_batches()
    _migrate_channel_legacy()
    return BatchManager.load(symbol=symbol, channel=channel)


class BatchManager:
    """批次台账：slot 列表 + 状态机 + 选择/退出逻辑。"""

    def __init__(
        self,
        symbol: str,
        account_ids: list[str],
        path: Optional[Path | str] = None,
        channel: Optional[str] = None,
    ) -> None:
        self.symbol = symbol
        self.channel = channel
        self.path = Path(path) if path else batches_path(self.symbol, channel)
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
    def load(
        cls,
        path: Optional[Path | str] = None,
        symbol: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> Optional["BatchManager"]:
        """从磁盘加载；文件缺失/损坏 → None。

        path 显式时用 path；否则 symbol 提供时读 ``batches.{channel}.{symbol}.json``
        （channel 缺省时读旧格式 ``batches.{symbol}.json``），都不提供时
        读旧式 ``batches.json``。
        """
        if path is None:
            path = batches_path(symbol, channel) if symbol else batches_path()
        p = Path(path)
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            bm = cls.__new__(cls)
            bm.symbol = raw.get("symbol", "")
            bm.path = p
            bm.channel = channel
            bm.slots = raw.get("slots", [])
            return bm
        except (OSError, ValueError, KeyError):
            return None

    # ── 查询 ────────────────────────────────────────────────────────
    def available_slots(self) -> list[dict[str, Any]]:
        return [s for s in self.slots if s["status"] == AVAILABLE]

    def open_slots(self) -> list[dict[str, Any]]:
        return [s for s in self.slots if s["status"] == OPEN]

    def any_open(self) -> bool:
        """是否存在 open 仓位（信号周期状态初始化用：重启时
        有 open 仓位视为本周期已建仓，保守不追同周期信号）。"""
        return bool(self.open_slots())

    def next_buy_slot(self, start_slot: int = 1) -> Optional[dict[str, Any]]:
        """BUY 目标：从 start_slot（1-based）起循环扫描第一个 available。

        v1.1：td_start_slot 起点偏移（完整循环 + 起点偏移，设 3 → 3→4→5→1→2）。
        默认 start_slot=1 = 原行为（slot 顺序第一个 available）。
        """
        for s in self.scan_buy_slots(start_slot):
            return s
        return None

    def scan_buy_slots(self, start_slot: int = 1) -> list[dict[str, Any]]:
        """从 start_slot 起循环扫描的 available slot 列表（供资金不足跳 slot）。

        顺序：start_slot → start_slot+1 → … → N → 1 → … → start_slot-1；
        仅包含 status=available 的 slot（含起点 slot 已 open 时的自然跳过）。
        """
        n = len(self.slots)
        if n == 0:
            return []
        start = max(1, int(start_slot or 1))
        start = min(start, n)  # 超界截断到 N（不循环回绕）
        ordered = self.slots[start - 1:] + self.slots[:start - 1]
        return [s for s in ordered if s["status"] == AVAILABLE]

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

    def get_lot(self, slot: int) -> Optional[dict[str, Any]]:
        """读取 slot 当前 lot（不改变状态）。

        2026-08-11 链上确认改造：卖出改为“链上确认成交后才 close_lot”，
        提交前需读取 lot 但不释放 slot——未确认/失败时台账保持 open，
        从根上消除“提交成功但链上未成交”导致的账实脱管。
        """
        target = next((s for s in self.slots if s["slot"] == slot), None)
        if target is None or target["status"] != OPEN or target["lot"] is None:
            return None
        return dict(target["lot"])

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


def ensure_batches(
    td_batches: int, symbol: str, channel: Optional[str] = None
) -> tuple[Optional[BatchManager], str]:
    """WebUI 保存 td_batches 时调用：确保子钱包/子账号数量 ≥ td_batches 并建/复用批次映射。

    - 子钱包不足 → ``wallet add`` 补足（add 会自动切换活跃账户，
      补足后 switch 回原活跃账户）。
    - 已有本通道 batches.{channel}.{symbol}.json 且 symbol 一致 → 复用
      （保留 open 批次，不重建）。
    - 返回 ``(BatchManager | None, 日志信息)``。
    """
    from .tools.tools_wallet import wallet_accounts, wallet_add, wallet_switch

    bm = _load_or_migrate(symbol, channel)
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
    bm = BatchManager(symbol=symbol, account_ids=ids[:td_batches], channel=channel)
    bm.save()
    return bm, f"批次初始化完成：{len(bm.slots)} slots（新建 {created}）"
