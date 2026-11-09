"""手动平仓命令队列（2026-11-09，用户需求）。

页面（``/config/td-table`` 实时监控 → 持仓小节）发起「手动平仓」→ 命令落盘
→ TD 循环在下一轮评估该标的时消费，走策略正常卖出路径（``_sell_lot``：
缩量卖出、min_quote 预检、成交确认、台账 ``close_lot``、事件记录全部复用）。

设计约束（与用户对齐的拍板点）：

- **只能由人触发**：页面两步确认（一次性 token，30s）+ 必须显式勾选
  「确认亏损平仓」。AI/循环永不自行入队。
- **单批粒度**，可勾选「同时平掉该标的全部 N 批」= 入队 N 条独立命令
  （每条独立核对台账，互不牵连）。
- **5 分钟未执行 → expired**：避免陈旧命令在很久之后突然成交。
- **fail-closed**：取价失败 / 台账无该批次 / 卖出未成交 → 标 failed 并原样
  记录原因，不重试、不猜测。
- 存储 = ``{credentials_dir}/td_manual_close.json``（与 exec_params 同目录），
  原子写 + 进程内锁；最多保留 50 条（含历史状态，供页面回看）。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

TTL_S = 300
_MAX_KEEP = 50

_LOCK = threading.RLock()


def queue_path() -> Path:
    """命令文件路径（与 exec_params.json 同目录，随测试隔离自动切换）。"""
    from .exec_params import exec_params_path

    return exec_params_path().parent / "td_manual_close.json"


def load() -> list[dict[str, Any]]:
    """读全部命令（损坏/缺失 → 空表，不抛）。"""
    try:
        raw = queue_path().read_text("utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _save(items: list[dict[str, Any]]) -> None:
    """原子写（tmp + replace，FUSE 抖动重试一次）。"""
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(items[-_MAX_KEEP:], ensure_ascii=False, indent=1)
    last: Exception | None = None
    for _ in range(3):
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mc-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
            return
        except OSError as exc:  # /data 是网络卷，偶发 EIO
            last = exc
            try:
                os.unlink(tmp)
            except OSError:
                pass
            time.sleep(0.05)
    if last is not None:
        raise last


def enqueue(
    scene: str,
    symbol: str,
    slot: int,
    qty: float | None = None,
    *,
    source: str = "manual",
) -> dict[str, Any]:
    """入队一条平仓命令，返回该命令记录。"""
    with _LOCK:
        items = load()
        rec = {
            "id": uuid.uuid4().hex[:12],
            "scene": str(scene),
            "symbol": str(symbol).upper(),
            "slot": int(slot),
            "qty": qty,
            "status": "queued",
            "created": _now_iso(),
            "created_ts": time.time(),
            "source": source,
            "note": "",
        }
        items.append(rec)
        _save(items)
        return rec


def expire_stale(*, now: float | None = None, ttl_s: int = TTL_S) -> int:
    """把超时未执行的 queued 命令标 expired，返回标记数量。"""
    cur = time.time() if now is None else now
    with _LOCK:
        items = load()
        n = 0
        for it in items:
            if it.get("status") != "queued":
                continue
            try:
                age = cur - float(it.get("created_ts") or 0)
            except (TypeError, ValueError):
                age = ttl_s + 1
            if age > ttl_s:
                it["status"] = "expired"
                it["note"] = "命令超时未执行（TD 未运行或该标的未评估）"
                it["finished"] = _now_iso()
                n += 1
        if n:
            _save(items)
        return n


def pending_for(scene: str, symbol: str, *, ttl_s: int = TTL_S) -> list[dict[str, Any]]:
    """取该 (场景, 标的) 待执行命令；顺带处理超时。"""
    expire_stale(ttl_s=ttl_s)
    sym = str(symbol).upper()
    with _LOCK:
        return [
            it
            for it in load()
            if it.get("status") == "queued"
            and str(it.get("symbol")).upper() == sym
            and str(it.get("scene")) == str(scene)
        ]


def mark(item_id: str, status: str, note: str = "", **extra: Any) -> None:
    """更新命令状态（done / failed / expired）。"""
    with _LOCK:
        items = load()
        for it in items:
            if it.get("id") == item_id:
                it["status"] = status
                if note:
                    it["note"] = note
                it["finished"] = _now_iso()
                it.update({k: v for k, v in extra.items() if v is not None})
                break
        _save(items)


def recent(limit: int = 20) -> list[dict[str, Any]]:
    """最近命令（新→旧），供页面显示结果。"""
    with _LOCK:
        items = load()
    return list(reversed(items[-max(1, int(limit)):]))


def clear() -> None:
    """清空队列（测试用）。"""
    with _LOCK:
        _save([])


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
