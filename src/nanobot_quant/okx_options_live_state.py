"""期权线进程内状态（LIVE_STATE 风格）—— 与 ``td_live_state`` 同构。

写方：lumibot 策略线程（``OkxOptionsPutStrategy``，跑在 gatekeeper 进程内）。
读方：期权页 ``live_state()``、runner（``okx_options_live``）。

只存**最近一轮**结果（页面展示用）；完整的逐笔历史在 append-only 事件文件
``okx_options_live_events.jsonl`` 里（跨重启保留）。状态本身不持久化 —— 重启后
进程内为空，页面会显示「上次 —」，下一轮自动补齐。

为什么单独一个模块而不是放在 ``okx_options_live`` 里：策略需要写状态，而
``okx_options_live`` 又要 import 策略（构造 executor），直接互相 import 会成环。
中间隔一层纯状态模块即可解耦（td_live / td_live_state 是同样的结构）。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.RLock()

_STATE: dict[str, Any] = {
    "settled": [],        # 最近一轮的到期判定结果
    "strategy": None,     # 最近一轮的策略结果 {"entries": [...], "exits": [...], "counts": {...}}
    "error": "",
    "round_ts": None,     # 最近一轮结束时间（UTC ISO）
    "totals": {},         # 累计：settled / entries / exits
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_round(*, settled: list | None = None, strategy: dict | None = None,
              error: str = "") -> None:
    """记录一轮结果（策略线程每轮末尾调用）。"""
    with _LOCK:
        if settled is not None:
            _STATE["settled"] = list(settled)
        if strategy is not None:
            _STATE["strategy"] = strategy
        _STATE["error"] = str(error or "")
        _STATE["round_ts"] = _utc_now()


def bump(key: str, n: int = 1) -> None:
    """累计计数（settled / entries / exits）。"""
    with _LOCK:
        totals = _STATE.setdefault("totals", {})
        totals[key] = int(totals.get(key) or 0) + int(n)


def snapshot() -> dict:
    """读当前状态（浅拷贝，调用方可安全遍历）。"""
    with _LOCK:
        return {
            "settled": list(_STATE.get("settled") or []),
            "strategy": _STATE.get("strategy"),
            "error": _STATE.get("error") or "",
            "round_ts": _STATE.get("round_ts"),
            "totals": dict(_STATE.get("totals") or {}),
        }


def reset() -> None:
    """清空（测试用）。"""
    with _LOCK:
        _STATE.update({"settled": [], "strategy": None, "error": "",
                       "round_ts": None, "totals": {}})


__all__ = ["set_round", "bump", "snapshot", "reset"]
