"""期权到期巡检 daemon（C23 S3，2026-09-07）。

复用 TD live 机制形态：daemon 线程 + 参数文件启停 + LIVE_STATE + append-only 事件文件。
每轮跑 ot.settle_expired_puts()（OTM 自动关账 / ITM 记赔付 / 矛盾挂 settled_review
fail-closed / 账单未出保持 open 下轮重试），判定结果逐笔 append 事件文件（跨重启保留）
并更新进程内 LIVE_STATE 供页面「到期处理区」轮询回看。

启停：option_params.json 的 live 字段 {"enabled": bool, "interval_s": int}——
期权页顶部「到期巡检」开关（WebUI 手动开启，AI 不自行开启；与现货 td_enabled 同规则）。
心跳周期默认 60s、范围 10–3600，WebUI 可配；interval 变更经 sync() 重启线程生效。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import okx_options_trade as ot

_LIVE_EVENTS_NAME = "okx_options_live_events.jsonl"

DEFAULT_INTERVAL_S = 60
MIN_INTERVAL_S = 10
MAX_INTERVAL_S = 3600

# 进程内 LIVE_STATE（页面轮询巡检状态/最近判定；事件文件为持久化权威）
_state = {
    "running": False,
    "last_run": None,      # ISO UTC
    "last_settled": [],    # 最近一轮判定结果
    "last_error": "",      # 最近一轮异常（无则为空）
    "total_settled": 0,    # 累计判定笔数（自进程启动）
}
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()


# ── 事件文件（append-only JSONL，与台账同目录持久化）───────

def _storage_dir() -> Path:
    from .credential_registry import _get_storage_dir
    return Path(_get_storage_dir())


def events_path() -> Path:
    return _storage_dir() / _LIVE_EVENTS_NAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_event(ev: dict) -> None:
    p = events_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def load_events(limit: int = 50) -> list[dict]:
    """从事件文件尾部读取最近 N 条 settle 判定事件（倒序返回，最新在前）。"""
    p = events_path()
    if not p.exists():
        return []
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            back = min(size, 65536)
            f.seek(size - back)
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if back < size:
        lines = lines[1:]  # 块首行可能被截断
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    # settled_itm 事件 enrich covered：台账存在同现货对（期权基础币 -USD/-USDC）的
    # filled spot_cover 行 ⇒ 该笔到期已现货补买（前端据此不再显示「补买」入口）。
    covers: set[str] = set()
    try:
        for r in ot.load_ledger():
            if r.get("kind") == "spot_cover" and r.get("status") == "filled":
                covers.add(str(r.get("inst_id") or ""))
    except Exception:
        covers = set()
    for ev in out:
        if ev.get("status") == ot.STATUS_SETTLED_ITM:
            base = str(ev.get("inst_id") or "").split("-")[0]
            ev["covered"] = any(c in covers for c in (f"{base}-USD", f"{base}-USDC"))
    return out


# ── 配置（option_params.json 的 live 字段）─────────────────

def live_config() -> dict:
    d = (ot.load_option_params().get("live") or {})
    try:
        enabled = bool(d.get("enabled", False))
    except (TypeError, ValueError):
        enabled = False
    try:
        interval_s = int(d.get("interval_s", DEFAULT_INTERVAL_S))
    except (TypeError, ValueError):
        interval_s = DEFAULT_INTERVAL_S
    interval_s = min(max(interval_s, MIN_INTERVAL_S), MAX_INTERVAL_S)
    return {"enabled": enabled, "interval_s": interval_s}


def save_live_config(enabled: bool | None = None,
                     interval_s: int | None = None) -> dict:
    cur = live_config()
    if enabled is not None:
        cur["enabled"] = bool(enabled)
    if interval_s is not None:
        try:
            cur["interval_s"] = min(max(int(interval_s), MIN_INTERVAL_S),
                                    MAX_INTERVAL_S)
        except (TypeError, ValueError):
            pass
    ot.save_option_params(live=cur)
    return cur


# ── 单轮巡检（线程与测试共用）──────────────────────────────

def run_once() -> dict:
    """一轮到期判定：settle_expired_puts → 逐笔 append 事件 + 更新 LIVE_STATE。
    任何异常不外抛（巡检自愈：本轮错误记 last_error，下轮继续）。"""
    settled = []
    error = ""
    try:
        settled = ot.settle_expired_puts()
    except Exception as e:  # noqa: BLE001 —— daemon 巡检不容许线程猝死
        error = f"{type(e).__name__}: {e}"
    with _lock:
        _state["last_run"] = _utc_now()
        _state["last_settled"] = settled
        _state["last_error"] = error
        _state["total_settled"] += len(settled)
    if settled:
        by_id = {e.get("id"): e for e in ot.load_ledger()}
        for s in settled:
            row = by_id.get(s.get("id")) or {}
            _append_event({
                "ts": _utc_now(),
                "type": "settle",
                "id": s.get("id"),
                "inst_id": s.get("inst_id"),
                "account": row.get("account") or "",
                "strike": row.get("strike"),
                "sz": row.get("sz"),
                "exp_ms": row.get("exp_ms"),
                "status": s.get("status"),
                "settle_px": s.get("settle_px"),
                "settle_pnl": s.get("settle_pnl"),
                "note": s.get("note", ""),
            })
    return {"settled": settled, "error": error}


# ── 线程生命周期 ──────────────────────────────────────────

def _loop() -> None:
    while not _stop.wait(live_config()["interval_s"]):
        run_once()


def live_state() -> dict:
    cfg = live_config()
    with _lock:
        return {
            "config": cfg,
            "running": bool(_thread is not None and _thread.is_alive()),
            "last_run": _state["last_run"],
            "last_settled": _state["last_settled"],
            "last_error": _state["last_error"],
            "total_settled": _state["total_settled"],
        }


def stop() -> None:
    """停止巡检线程（幂等）。不打断正在执行的一轮——等它自然结束。"""
    global _thread
    with _lock:
        t = _thread
        _stop.set()
    if t is not None:
        t.join(timeout=10)
    with _lock:
        if _thread is t:
            _thread = None
    _stop.clear()


def _start_thread(cfg: dict) -> None:
    global _thread
    _stop.clear()
    t = threading.Thread(target=_loop, name="okx-options-live", daemon=True)
    t._live_cfg = cfg  # type: ignore[attr-defined]
    with _lock:
        _thread = t
    t.start()


def sync() -> dict:
    """按 option_params live 配置启/停/重启线程（幂等）。
    运行中且配置未变 → 不动；运行中配置变（interval）→ 重启；enabled=false → 停。"""
    cfg = live_config()
    with _lock:
        t = _thread
        alive = t is not None and t.is_alive()
        last_cfg = getattr(t, "_live_cfg", None) if t is not None else None
    if alive and last_cfg == cfg:
        return live_state()
    if alive:
        stop()  # 需要重启或停止——等当前一轮自然结束后再操作
    if cfg["enabled"]:
        _start_thread(cfg)
    return live_state()
