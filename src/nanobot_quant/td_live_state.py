"""TD live 实时状态共享（内存 dict + 事件文件持久化）。

TD live 循环（gatekeeper 进程内 StrategyExecutor）每轮把各标的的
TD Sequential 计算结果写入内存 LIVE_STATE；/config/td-table 的
「实时监控」tab（同进程）直接读取渲染，页面 JS 按 exec_params
``td_ui_refresh_s`` 轮询自动刷新（2026-08-11 方案 A：内存共享）。

有信号事件（LONG/SELL/EXIT/SKIP/FAIL）时追加到事件文件（append-only
JSONL，与 exec_params.json 同一 credentials 目录），重启保留。
事件写入仅在 TD live 模式启用（策略 ``live_mode=True``）——回测/
纸交易进程不写文件，避免污染生产事件历史。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

#: 内存共享状态（唯一写入方：TD live 策略循环；唯一读取方：td-table live tab）
LIVE_STATE: dict = {
    "running": False,
    "next_iteration": None,
    "strategy_variant": None,  # 运行中循环实际使用的策略变体（td_live 启动时写入）
    "symbols": {},
    "updated_at": None,
}

_lock = threading.Lock()

#: 停止请求标志（2026-08-21 延迟停止方案）。td_live.stop() 设置；
#: 策略 on_trading_iteration 开头检查（孤儿 job 防护——主循环 break 前
#: lumibot 可能重建 scheduler，新 scheduler 的 job 会在 interval 后再次
#: 调 on_trading_iteration，stop_requested 置位后直接 return 防止空跑/
#: 误下单）。start() 启动新循环时 clear()。
stop_requested = threading.Event()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def events_path() -> Path:
    """事件文件路径（与 exec_params.json 同一 credentials 目录）。"""
    for root in ("/data", "/mnt/workspace"):
        d = Path(root) / "legion" / "credentials"
        try:
            if d.exists():
                return d / "td_live_events.jsonl"
        except OSError:
            continue
    return Path.home() / ".td_live_events.jsonl"


def update_symbol(symbol: str, data: dict, scene: str = "") -> None:
    """每轮更新某标的状态（TD 策略 _evaluate_symbol 计算后调用）。

    2026-08-21（页面场景化 B1）：LIVE_STATE['symbols'] 按场景嵌套
    {scene: {symbol: {...}}}——多场景同标的互不覆盖；scene 缺省
    （非场景模式/回测/旧调用方）归入 "default"。
    """
    key = scene or "default"
    with _lock:
        syms = LIVE_STATE["symbols"].setdefault(key, {})
        syms[symbol] = {
            "setup_buy": data.get("setup_buy", 0) or 0,
            "setup_sell": data.get("setup_sell", 0) or 0,
            "cd_buy": data.get("cd_buy", 0) or 0,
            "cd_sell": data.get("cd_sell", 0) or 0,
            "score": data.get("score", 0) or 0,
            "price": data.get("price", 0) or 0,
            "signal": data.get("signal", "HOLD"),
            "note": data.get("note", ""),
            "time": data.get("time", ""),
            "updated_at": _now_iso(),
        }
        LIVE_STATE["updated_at"] = syms[symbol]["updated_at"]


def set_loop(running: bool, next_iteration: str | None = None) -> None:
    """更新循环运行状态（TD live 线程启动/退出时调用）。"""
    with _lock:
        LIVE_STATE["running"] = bool(running)
        LIVE_STATE["next_iteration"] = next_iteration
        LIVE_STATE["updated_at"] = _now_iso()


def set_next_due(next_iteration: str | None) -> None:
    """更新「下一轮」时间（策略场景调度每轮计算后调用，2026-08-21）。"""
    with _lock:
        LIVE_STATE["next_iteration"] = next_iteration


def set_strategy(name: str) -> None:
    """记录运行中循环实际使用的策略变体（TD live 启动时调用）。

    2026-08-19：/config/exec 页需要区分 strategy.json 目标值与运行中
    实际值——切换策略后不重启 TD 循环，两者会不一致。
    """
    with _lock:
        LIVE_STATE["strategy_variant"] = str(name or "")
        LIVE_STATE["updated_at"] = _now_iso()


def get_state() -> dict:
    """供 td-table「实时监控」tab 读取（同进程，无 IO）。"""
    with _lock:
        return {
            "running": bool(LIVE_STATE["running"]),
            "next_iteration": LIVE_STATE["next_iteration"],
            "strategy_variant": LIVE_STATE.get("strategy_variant"),
            "updated_at": LIVE_STATE["updated_at"],
            "symbols": dict(LIVE_STATE["symbols"]),
        }


def append_event(event: dict) -> None:
    """追加信号事件到文件（LONG/SELL/EXIT/SKIP/FAIL）。"""
    row = {
        "ts": _now_iso(),
        **{k: v for k, v in event.items() if k != "ts"},
    }
    try:
        path = events_path()
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 事件写入失败不阻塞交易循环


def load_events(n: int = 20) -> list[dict]:
    """读最近 n 条事件（文件尾部分行读取，供 live tab 展示）。"""
    path = events_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events
_STABLE_SYMS = ("USDC", "USDT", "USDG")


def compute_actual_price(d0: dict) -> float | None:
    """从 swap_status 确认数据算实际成交价（稳定币计价规则）。

    2026-08-13 方案 B：系统交易恒以稳定币计价（broker quote=USDC）——
    找 input/output 里的稳定币（USDC/USDT/USDG）作分子、另一侧数量作
    分母 → 价格 = 稳定币金额 / 数量。无稳定币或两侧均稳定币（方向无法
    唯一确定）→ 返回 None。
    """
    try:
        if not isinstance(d0, dict):
            return None
        stab_amt, other_amt = 0.0, 0.0
        n_stab = n_other = 0
        for key in ("input", "output"):
            v = d0.get(key)
            if not isinstance(v, list):
                continue
            for item in v:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                try:
                    amt = float(item.get("amount") or 0)
                except (TypeError, ValueError):
                    continue
                if not name or amt <= 0:
                    continue
                if any(s in name.upper() for s in _STABLE_SYMS):
                    stab_amt += amt
                    n_stab += 1
                else:
                    other_amt += amt
                    n_other += 1
        if n_stab == 1 and n_other == 1 and stab_amt > 0 and other_amt > 0:
            return stab_amt / other_amt
        return None
    except Exception:  # noqa: BLE001
        return None
