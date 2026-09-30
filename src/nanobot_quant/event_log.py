"""事件落盘前的重复抑制（2026-09-30）。

背景：到期日当天，期权 `exit` 巡检每 60 秒对同一合约写一条
`status=skipped_expired`（note「已到期，不提交买回」），一天约 480 条，
把真事件（成交 / 失败 / 结算）淹在噪音里。

规则：**「无动作」类状态**（`skipped_*` / `failed` / `pending_confirm` /
`dry_run*` / `no_action` / `no_signal` / `cycle_wait`）按
`(inst_id, type, status)` 去重 —— 首条必记，之后最少间隔 `THROTTLE_S`
（默认 1 小时）才再记一条；**真实动作**（`sold` / `bought_back` / `settle`
判定 / `cover` 成交…）不受限制，审计链不受影响。

记忆是进程内的：重启后同类事件会再记一条（可接受，也算「重启可见」）。
写事件的两个漏斗（策略 `_record`、runner `_append_event`）都调用本模块，
策略只此一份、不各自实现。
"""

from __future__ import annotations

import threading
import time

#: 同类「无动作」事件的最小重复间隔（秒）
THROTTLE_S = 3600

#: 视为「无动作」的状态前缀（真实动作不在其中 ⇒ 一律落盘）
NO_ACTION_PREFIXES = ("skipped", "failed", "pending_confirm", "dry_run",
                      "no_action", "no_signal", "cycle_wait")

_lock = threading.Lock()
_last: dict[tuple[str, str, str], float] = {}


def should_log_event(ev: dict) -> bool:
    """True = 允许落盘；False = 与最近一条同类「无动作」事件重复，抑制。

    真实动作（sold / bought_back / settled_* / cover filled …）永远返回 True。
    """
    e = ev or {}
    status = str(e.get("status") or "")
    if not status.startswith(NO_ACTION_PREFIXES):
        return True
    key = (str(e.get("inst_id") or ""), str(e.get("type") or ""), status)
    now = time.time()
    with _lock:
        prev = _last.get(key)
        if prev is not None and (now - prev) < THROTTLE_S:
            return False
        _last[key] = now
        return True


def reset() -> None:
    """清空记忆（测试用；生产不需要调用）。"""
    with _lock:
        _last.clear()
