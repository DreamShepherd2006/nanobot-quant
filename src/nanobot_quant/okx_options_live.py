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
    "last_strategy": None,  # 最近一轮策略决策（entries/exits/notes）
    "last_error": "",      # 最近一轮异常（无则为空）
    "total_settled": 0,    # 累计判定笔数（自进程启动）
    "total_entries": 0,    # 累计卖 put 笔数（含 dry_run）
    "total_exits": 0,      # 累计止盈买回笔数（含 dry_run）
}

# 策略默认参数（option_params.json 的 live.strategy 段；用户可在页面改）
# 取向（2026-09-15 拍板）：先跑通闭环，参数用真实样本迭代——所以
#   - dry_run 默认 True：循环跑起来也只「说它想做什么」，不下单；
#   - iv_min_percentile 默认 0（关闭）：历史 IV 分位需样本积累，不假装有依据。
DEFAULT_STRATEGY: dict = {
    "account": "",                      # 期权子账号（空 = 默认）
    "families": ["SOL-USD_UM"],         # 标的家族
    "td_period": "5m",                  # 信号周期（5m 为实证主力周期）
    "td_bars": 120,                      # K 线窗口
    "entry_setup": 9,                    # 买 9 阈值
    "entry_countdown": 13,               # countdown 13 阈值
    "iv_min_percentile": 0,              # IV 环境闸门（0 = 关）
    "take_profit_pct": 50,               # 权利金回落止盈线（%）
    "max_contracts_per_family": 1,
    "max_contracts_total": 3,
    "dry_run": True,
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
    # 到期事件 enrich：台账存在同现货对（期权基础币 -USD/-USDC）的 filled 行
    #   put  → filled spot_cover ⇒ 已现货补买（covered=True，前端不再显示「补买」入口）
    #   call → filled spot_exit  ⇒ 已现货出货（exited=True，前端不再显示「出货」入口）
    covers: set[str] = set()
    exits: set[str] = set()
    try:
        for r in ot.load_ledger():
            if r.get("status") != "filled":
                continue
            if r.get("kind") == "spot_cover":
                covers.add(str(r.get("inst_id") or ""))
            elif r.get("kind") == "spot_exit":
                exits.add(str(r.get("inst_id") or ""))
    except Exception:
        covers, exits = set(), set()
    for ev in out:
        if ev.get("status") == ot.STATUS_SETTLED_ITM:
            base = str(ev.get("inst_id") or "").split("-")[0]
            pairs = (f"{base}-USD", f"{base}-USDC")
            is_call = (str(ev.get("opt_type") or "").upper() == "C"
                       or str(ev.get("inst_id") or "").endswith("-C"))
            if is_call:
                ev["exited"] = any(c in exits for c in pairs)
            else:
                ev["covered"] = any(c in covers for c in pairs)
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
    strat = dict(DEFAULT_STRATEGY)
    raw = d.get("strategy")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in DEFAULT_STRATEGY or k == "selector":
                strat[k] = v
    return {"enabled": enabled, "interval_s": interval_s, "strategy": strat}


def save_live_config(enabled: bool | None = None,
                     interval_s: int | None = None,
                     strategy: dict | None = None) -> dict:
    cur = live_config()
    if enabled is not None:
        cur["enabled"] = bool(enabled)
    if interval_s is not None:
        try:
            cur["interval_s"] = min(max(int(interval_s), MIN_INTERVAL_S),
                                    MAX_INTERVAL_S)
        except (TypeError, ValueError):
            pass
    if isinstance(strategy, dict):
        strat = dict(cur["strategy"])
        for k, v in strategy.items():
            if k in DEFAULT_STRATEGY or k == "selector":
                strat[k] = v
        cur["strategy"] = strat
    ot.save_option_params(live=cur)
    return cur


# ── 策略轮次（卖 put + 权利金回落止盈）──────────────────────

def _strategy_params(cfg: dict) -> dict:
    """live_config()['strategy'] → evaluate_entry 需要的 params 形状。"""
    return dict(cfg.get("strategy") or {})


def _td_signal_for(base: str, bar: str, bars: int) -> dict | None:
    """标的最新 TD 信号（Gate CEX 同所 K 线 → 原版 TD 引擎）。"""
    from .data_sources import gate_cex
    from .strategies.td_sequential import calculate
    df = gate_cex.fetch_kline(base, bar=bar, limit=int(bars))
    if df is None or len(df) == 0:
        return None
    return calculate(df)


def _sell_put(account: str, inst_id: str, sz: int) -> dict:
    """卖开 put：IOC + 保护线 px（C22b）——期权不支持市价单。"""
    sug = ot.suggest_px_for_order(inst_id, "sell", sz=sz)
    px = (sug or {}).get("px")
    if not px or float(px) <= 0:
        raise RuntimeError(f"无有效保护价（{inst_id}）——fail-closed 不下单")
    return ot.open_put(account, inst_id=inst_id, sz=sz, ord_type="ioc", px=float(px))


def _buy_back(account: str, inst_id: str, sz: int) -> dict:
    """买回平仓：IOC + ask 侧保护价。"""
    sug = ot.suggest_px_for_order(inst_id, "buy", sz=sz)
    px = (sug or {}).get("px")
    if not px or float(px) <= 0:
        raise RuntimeError(f"无有效保护价（{inst_id}）——fail-closed 不买回")
    return ot.close_put(account, inst_id=inst_id, sz=sz, ord_type="ioc", px=float(px))


def strategy_round(cfg: dict | None = None) -> dict:
    """一轮策略决策：先入场（TD 衰竭 + 闸门 + 张数上限），再止盈巡检。

    ``dry_run=True``（默认）时**只记录决策、不下单**——这是「AI 不能自行授权
    实盘」在闭环里的结构性落实：循环跑起来也不会真下单，需用户手动关掉。
    决策失败/异常一律入 notes，不向外抛（daemon 不容许线程猝死）。
    """
    from . import okx_options_strategy as st

    cfg = cfg or live_config()
    s = _strategy_params(cfg)
    account = str(s.get("account") or "")
    dry = bool(s.get("dry_run", True))
    notes: list[str] = []
    entries: list[dict] = []
    exits: list[dict] = []

    # 持仓（张数上限与止盈共用一次查询）
    try:
        positions = ot.open_puts(account)
    except Exception as e:  # noqa: BLE001 —— 持仓查不到则本轮不动作
        return {"dry_run": dry, "entries": [], "exits": [],
                "notes": [f"持仓查询失败：{type(e).__name__}: {e}"]}

    counts = st.contracts_by_family(positions)
    total_contracts = sum(counts.values())

    # ① 入场（逐个家族）
    for family in s.get("families") or []:
        base = str(family).split("-")[0]
        try:
            sig = _td_signal_for(base, str(s.get("td_period") or "5m"),
                                 int(s.get("td_bars") or 120))
        except Exception as e:  # noqa: BLE001
            notes.append(f"{base}: K 线/TD 失败 {type(e).__name__}: {e}")
            continue
        if sig is None:
            notes.append(f"{base}: 无 K 线数据")
            continue
        dec, note = st.evaluate_entry(
            family, td_signal=sig, params=s,
            open_contracts=counts.get(base, 0), total_contracts=total_contracts)
        if dec is None:
            notes.append(f"{base}: {note}")
            continue
        rec: dict = {**dec.to_event(), "dry_run": dry}
        if dry:
            rec["status"] = "dry_run(would_sell)"
        else:
            try:
                res = _sell_put(account, dec.inst_id, dec.sz)
                rec["status"] = "sold"
                rec["order"] = {k: res.get(k) for k in ("ord_id", "status", "avg_px", "px")
                                if k in res}
                counts[base] = counts.get(base, 0) + dec.sz
                total_contracts += dec.sz
            except Exception as e:  # noqa: BLE001 —— 失败必须可见
                rec["status"] = "failed"
                rec["error"] = f"{type(e).__name__}: {e}"
        entries.append(rec)

    # ② 止盈（权利金回落）
    try:
        tp = float(s.get("take_profit_pct") or 0)
    except (TypeError, ValueError):
        tp = 0.0
    for x in st.evaluate_exits(positions, tp_pct=tp):
        rec = {**x.to_event(), "dry_run": dry}
        if dry:
            rec["status"] = "dry_run(would_buy_back)"
        else:
            try:
                res = _buy_back(account, x.inst_id, x.sz)
                rec["status"] = "bought_back"
                rec["order"] = {k: res.get(k) for k in ("ord_id", "status", "avg_px", "px")
                                if k in res}
            except Exception as e:  # noqa: BLE001
                rec["status"] = "failed"
                rec["error"] = f"{type(e).__name__}: {e}"
        exits.append(rec)

    return {"dry_run": dry, "entries": entries, "exits": exits,
            "notes": notes, "contracts": counts}


# ── 单轮巡检（线程与测试共用）─────────────────────────────

def run_once() -> dict:
    """一轮到期判定：settle_expired_puts（put+call）→ 逐笔 append 事件 + 更新 LIVE_STATE。
    任何异常不外抛（巡检自愈：本轮错误记 last_error，下轮继续）。"""
    settled = []
    error = ""
    try:
        settled = ot.settle_expired_puts()
    except Exception as e:  # noqa: BLE001 —— daemon 巡检不容许线程猝死
        error = f"{type(e).__name__}: {e}"
    # 先落盘事件、后更新 LIVE_STATE：total_settled 语义 = 已落盘判定笔数，
    # 避免「计数已 +1 但事件未写完」的观测竞态（页面/测试按计数读事件会拿到空）。
    if settled:
        try:
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
                    "opt_type": s.get("opt_type") or ("C" if str(s.get("inst_id") or "").endswith("-C") else "P"),
                    "settle_px": s.get("settle_px"),
                    "settle_pnl": s.get("settle_pnl"),
                    "settle_payout": s.get("settle_payout"),
                    "note": s.get("note", ""),
                })
        except Exception as e:  # noqa: BLE001 —— 落盘失败不阻断状态更新
            error = (error + " | " if error else "") + f"event_append: {type(e).__name__}: {e}"
    with _lock:
        _state["last_run"] = _utc_now()
        _state["last_settled"] = settled
        _state["last_error"] = error
        _state["total_settled"] += len(settled)
    # ② 策略轮次（入场 + 止盈）：到期判定之后跑，互不阻断
    strat = None
    try:
        strat = strategy_round(live_config())
        for rec in (strat.get("entries") or []):
            _append_event({"ts": _utc_now(), "type": "entry", **rec})
        for rec in (strat.get("exits") or []):
            _append_event({"ts": _utc_now(), "type": "exit", **rec})
    except Exception as e:  # noqa: BLE001 —— 策略异常不得杀线程
        error = (error + " | " if error else "") + f"strategy: {type(e).__name__}: {e}"
    with _lock:
        _state["last_strategy"] = strat
        _state["total_entries"] += len((strat or {}).get("entries") or [])
        _state["total_exits"] += len((strat or {}).get("exits") or [])
        _state["last_error"] = error
    return {"settled": settled, "strategy": strat, "error": error}


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
            "last_strategy": _state["last_strategy"],
            "last_error": _state["last_error"],
            "total_settled": _state["total_settled"],
            "total_entries": _state["total_entries"],
            "total_exits": _state["total_exits"],
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
