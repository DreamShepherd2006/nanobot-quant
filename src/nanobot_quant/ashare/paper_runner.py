"""A 股模拟账本 runner（纸上交易，PR 2/5）。

只读行情 + 本地账本：**不连券商、不下单、不读 QMT 持仓**。数字与实盘物理隔开，
页面一律标注「模拟账本（不接券商）」，不得当实盘验收证据。

与 :mod:`nanobot_quant.ashare.tape`（盘口采集器）同构：``LiveRunnerBase`` 子类，
独立参数文件 + ``sync()`` 幂等启停 + append-only 事件文件。**默认关**。

一轮 = 一根 1m bar 的心跳：

1. 非交易时段直接跳过（零请求）
2. 每个标的拉 1m → 按交易日切分算 setup（复用 ``td_panel``，与 QMT 策略同源）
3. mark 更新（tape 盘口 mid 优先，取不到保留上次价）
4. 出场：到期结算 → 止盈 / DTE 强平（盘口 ask 优先，缺则用 mark 并标注）
5. 入场：低9 首穿 → 同浪门 → 额度门 → ``pick_put`` → ``sell_open``

两条口径（用户 2026-11-25 拍板）：

* 资金按 **100% 全损担保**（strike × 面值 × 张数）占用，不按平台 7–12% 保证金 ——
  否则资金效率虚高，模拟结果失去参考价值。
* 成交价 **盘口优先、bar 近似兜底**：有真实 bid/ask 就不该再叠一层 tick。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import fields as dc_fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nanobot_quant import credential_registry as creg
from nanobot_quant.ashare import contracts as ac
from nanobot_quant.ashare import paper_ledger as pl
from nanobot_quant.ashare import paper_strategy as ps
from nanobot_quant.ashare import tape
from nanobot_quant.ashare import td_panel as tp
from nanobot_quant.live_runner_base import LiveRunnerBase

CN = timezone(timedelta(hours=8))

DIR_NAME = "ashare_paper"
CFG_NAME = "config.json"
STATE_NAME = "state.json"

LOG_PREFIX = "[ASHARE-PAPER]"
DEFAULT_INTERVAL_S = 60
INTERVAL_RANGE = (30, 1800)
DEFAULT_SYMBOLS = ["510050.SH"]        # 收租基本盘；588000 要等 cd13 引擎（PR 4）
DEFAULT_INITIAL_CASH = 500_000.0
BARS = 640                             # 腾讯单次上限
ENTRY_COUNT = 9                        # 低9
FILLS_KEEP = 200                       # 页面/状态里带最近 N 条成交
SESSION_END_MIN = 15 * 60 + 1          # 15:01 之后当日收盘价才确定

PARAM_FIELDS = tuple(f.name for f in dc_fields(ps.PaperParams))


# ── 路径 ──────────────────────────────────────────────────

def paper_dir() -> Path:
    return Path(creg._get_storage_dir()) / DIR_NAME


def _cfg_path() -> Path:
    return paper_dir() / CFG_NAME


def _state_path() -> Path:
    return paper_dir() / STATE_NAME


def _atomic_write_json(path: Path, payload: dict) -> None:
    """原子写（同目录 tmp + replace）；失败显式抛错，不静默降级。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── 配置 ──────────────────────────────────────────────────

def default_params() -> dict:
    return {f.name: getattr(ps.PaperParams(), f.name) for f in dc_fields(ps.PaperParams)}


def default_config() -> dict:
    return {
        "enabled": False,
        "interval_s": DEFAULT_INTERVAL_S,
        "symbols": list(DEFAULT_SYMBOLS),
        "initial_cash": DEFAULT_INITIAL_CASH,
        "params": default_params(),
    }


def _pool_codes() -> list[str]:
    return [code for code, _ in tape.POOL_UNDERLYINGS]


def _normalize(cfg: dict | None) -> dict:
    """校验 + 夹取。非法值 fail-closed 报错（不静默回默认）。"""
    base = default_config()
    cfg = cfg if isinstance(cfg, dict) else {}

    out = dict(base)
    out["enabled"] = bool(cfg.get("enabled", base["enabled"]))

    lo, hi = INTERVAL_RANGE
    try:
        iv = int(cfg.get("interval_s") or DEFAULT_INTERVAL_S)
    except (TypeError, ValueError):
        raise ValueError(f"采样间隔必须是整数（{cfg.get('interval_s')!r}）")
    out["interval_s"] = max(lo, min(hi, iv))

    pool = _pool_codes()
    syms = cfg.get("symbols")
    if syms is None:
        syms = base["symbols"]
    if isinstance(syms, str):
        syms = [syms]
    if not isinstance(syms, (list, tuple)) or not syms:
        raise ValueError("标的池不能为空")
    cleaned: list[str] = []
    for s in syms:
        code = str(s).strip().upper()
        if code and code not in cleaned:
            cleaned.append(code)
    bad = [c for c in cleaned if c not in pool]
    if bad:
        raise ValueError(f"标的池含不支持的标的 {bad}，可选：{pool}")
    out["symbols"] = cleaned

    try:
        cash = float(cfg.get("initial_cash") or DEFAULT_INITIAL_CASH)
    except (TypeError, ValueError):
        raise ValueError(f"初始资金必须是数字（{cfg.get('initial_cash')!r}）")
    if cash <= 0:
        raise ValueError("初始资金必须为正")
    out["initial_cash"] = cash

    raw_params = cfg.get("params") if isinstance(cfg.get("params"), dict) else {}
    merged = {**default_params(), **{k: v for k, v in raw_params.items() if k in PARAM_FIELDS}}
    try:
        params = ps.PaperParams(**merged)
    except TypeError as e:      # noqa: BLE001
        raise ValueError(f"规则参数非法: {e}")
    out["params"] = {f.name: getattr(params, f.name) for f in dc_fields(ps.PaperParams)}
    return out


def load_config() -> dict:
    p = _cfg_path()
    if not p.exists():
        return default_config()
    try:
        with open(p, encoding="utf-8") as f:
            return _normalize(json.load(f))
    except Exception as e:      # noqa: BLE001 — 文件损坏不拖垮页面，回默认并留痕
        print(f"{LOG_PREFIX} 配置读取失败，回默认: {type(e).__name__}: {e}", flush=True)
        return default_config()


def save_config(**kw) -> dict:
    cfg = _normalize({**load_config(), **{k: v for k, v in kw.items() if v is not None}})
    _atomic_write_json(_cfg_path(), cfg)
    return cfg


# ── 账本状态（跨重启恢复） ────────────────────────────────

def _new_state(initial_cash: float) -> dict:
    led = pl.PaperLedger(initial_cash=float(initial_cash))
    return {
        "version": 1,
        "created": _now_ts(),
        "updated": None,
        "ledger": led.to_state(),
        "meta": {"last_bar": {}, "wave_open": {}, "rounds": 0, "last_round": None},
    }


def load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return _new_state(load_config()["initial_cash"])
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError("state 不是对象")
    except Exception as e:      # noqa: BLE001
        raise RuntimeError(f"模拟账本状态读取失败（{p}）: {type(e).__name__}: {e}")
    st.setdefault("ledger", pl.PaperLedger().to_state())
    st.setdefault("meta", {})
    meta = st["meta"]
    meta.setdefault("last_bar", {})
    meta.setdefault("wave_open", {})
    meta.setdefault("rounds", 0)
    return st


def save_state(state: dict) -> None:
    state["updated"] = _now_ts()
    _atomic_write_json(_state_path(), state)


def reset_state(initial_cash: float | None = None) -> dict:
    cfg = load_config()
    st = _new_state(float(initial_cash if initial_cash is not None else cfg["initial_cash"]))
    save_state(st)
    return st


def ledger_of(state: dict) -> pl.PaperLedger:
    return pl.PaperLedger.from_state(state.get("ledger"))


def _store_ledger(state: dict, led: pl.PaperLedger) -> None:
    state["ledger"] = led.to_state()


# ── 小工具 ────────────────────────────────────────────────

def _now_ts(now: datetime | None = None) -> str:
    return (now or datetime.now(CN)).strftime("%Y-%m-%dT%H:%M:%S")


def _expiry_date(expiry: str) -> date | None:
    try:
        s = str(expiry or "")
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8])) if len(s) == 8 else None
    except (TypeError, ValueError):
        return None


def _mid(q: dict) -> float:
    bid = float(q.get("bid") or 0.0)
    ask = float(q.get("ask") or 0.0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return bid or ask or float(q.get("last") or 0.0)


def _quotes(codes: list[str]) -> dict[str, dict]:
    """批量取盘口（失败返回空 dict —— 行情失败不该让整轮崩掉）。"""
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes:
        return {}
    try:
        rows, _ = tape.fetch_quotes(codes)
    except Exception as e:      # noqa: BLE001
        print(f"{LOG_PREFIX} 盘口获取失败: {type(e).__name__}: {e}", flush=True)
        return {}
    return {str(r.get("code")): r for r in rows or [] if r.get("code")}


# ── runner ────────────────────────────────────────────────

class PaperRunner(LiveRunnerBase):
    """模拟账本循环（纸上交易）。默认关，开关只在页面手动打开。"""

    LOG_PREFIX = LOG_PREFIX
    EVENTS_NAME = "ashare_paper_events.jsonl"
    DEFAULT_INTERVAL_S = DEFAULT_INTERVAL_S
    INTERVAL_RANGE = INTERVAL_RANGE

    # ── 基类钩子 ──
    def load_config(self) -> dict:
        return load_config()

    def config_changed(self, old: dict, new: dict) -> bool:
        a = {k: v for k, v in (old or {}).items() if k != "enabled"}
        b = {k: v for k, v in (new or {}).items() if k != "enabled"}
        return json.dumps(a, sort_keys=True, ensure_ascii=False) != \
            json.dumps(b, sort_keys=True, ensure_ascii=False)

    def storage_dir(self) -> Path:
        return paper_dir()

    # ── 单轮 ──
    def do_round(self, cfg: dict | None = None) -> dict:
        cfg = cfg or load_config()
        now = datetime.now(CN)
        if not tape.in_session(now):
            return {"skipped": "非交易时段"}

        st = load_state()
        led = ledger_of(st)
        meta = st["meta"]
        params = ps.PaperParams(**{k: v for k, v in (cfg.get("params") or {}).items()
                                   if k in PARAM_FIELDS})
        today = now.date()
        after_close = now.hour * 60 + now.minute >= SESSION_END_MIN
        ts = _now_ts(now)

        out: dict[str, Any] = {"actions": [], "symbols": {}, "equity": None, "ts": ts}

        # ① 行情（每个标的一次拉取）
        frames: dict[str, Any] = {}
        spots: dict[str, float] = {}
        for sym in cfg.get("symbols") or []:
            try:
                df = tp.fetch_klines(bar="m1", n=BARS, symbol=sym)
                df = tp.drop_unclosed(df, 60, now)
                if df is None or df.empty:
                    out["symbols"][sym] = {"error": "无行情"}
                    continue
                df = tp.setup_by_session(df)
                frames[sym] = df
                spots[sym] = float(df["close"].iloc[-1])
                out["symbols"][sym] = {"spot": round(spots[sym], 4),
                                       "setup": int(df["buy_setup_count"].iloc[-1])}
            except Exception as e:      # noqa: BLE001 — 单标的失败不拖垮其它标的
                out["symbols"][sym] = {"error": f"{type(e).__name__}: {e}"}

        # ② mark（盘口 mid 优先）
        open_codes = list(led.shorts.keys())
        qs = _quotes(open_codes)
        for code, sp in led.shorts.items():
            q = qs.get(code)
            if q:
                px = _mid(q)
                led.mark_short(code, px)
                sp.mark_px = px
        for sym, h in led.etf.items():
            if spots.get(sym):
                led.mark_etf(sym, spots[sym])

        # ③ 出场：到期结算 → 止盈 / DTE 强平
        for code in list(led.shorts.keys()):
            sp = led.shorts.get(code)
            if sp is None:
                continue
            exp = _expiry_date(sp.expiry)
            expired = exp is not None and today >= exp
            if expired:
                # 到期日当天等收盘（15:01 后）才结算；漏跑（today > exp）直接补结
                if exp == today and not after_close:
                    continue
                spot = spots.get(sp.underlying) or sp.mark_px
                if not spot or spot <= 0:
                    continue            # 拿不到标的价不结算，下轮重试
                r = led.settle_expiry(code=code, settle_spot=spot, ts=ts)
                if r.get("ok"):
                    out["actions"].append({"action": "settle", "code": code,
                                           "spot": round(float(spot), 4),
                                           "detail": r.get("fill")})
                    self._append_event({"type": "settle", "code": code,
                                        "spot": round(float(spot), 4)})
                else:
                    out["actions"].append({"action": "settle_fail", "code": code,
                                           "reason": r.get("reason")})
                continue

            mark = sp.mark_px
            need, reason = ps.should_close(
                {"entry_px": sp.entry_px, "expiry": sp.expiry}, mark, today, params)
            if not need:
                continue
            q = qs.get(code) or {}
            ask = float(q.get("ask") or 0.0)
            px_src = "quote" if ask > 0 else "mark"
            r = led.buy_close(code=code, ts=ts, reason=reason,
                              px=ask if ask > 0 else None, bar_close=mark)
            if r.get("ok"):
                out["actions"].append({"action": "buy_close", "code": code,
                                       "reason": reason, "px_src": px_src,
                                       "detail": r.get("fill")})
                self._append_event({"type": "buy_close", "code": code,
                                    "reason": reason, "px_src": px_src})
                self._log(f"BUY BACK {code} | {reason} | px_src={px_src}")

        # 同浪门：该标的已无持仓 → 这一浪结束，下个信号开新的一浪
        wave = meta.setdefault("wave_open", {})
        still_open = {sp.underlying for sp in led.shorts.values()}
        for sym in list(wave.keys()):
            if sym not in still_open:
                wave[sym] = False

        # ④ 入场：低9 首穿 → 同浪门 → 额度门 → 选档
        last_bar = meta.setdefault("last_bar", {})
        for sym, df in frames.items():
            sig = self._first_cross(df, last_bar, sym)
            if sig is None:
                continue
            spot = spots.get(sym) or 0.0
            if spot <= 0:
                continue
            if params.max_per_wave > 0 and wave.get(sym):
                out["actions"].append({"action": "skip", "symbol": sym,
                                       "reason": "同浪门：本浪已开过仓"})
                continue
            ok, why = ps.can_open([{"underlying": sp.underlying}
                                   for sp in led.shorts.values()], sym, params)
            if not ok:
                out["actions"].append({"action": "skip", "symbol": sym, "reason": why})
                continue
            chain = self._chain(sym, spot, today)
            pick = ps.pick_put(chain, spot, today, params)
            if not pick:
                out["actions"].append({"action": "skip", "symbol": sym,
                                       "reason": f"无合适合约（候选 {len(chain)}）"})
                continue
            r = led.sell_open(
                code=str(pick["code"]), underlying=sym, strike=float(pick["strike"]),
                expiry=str(pick["expiry"]), qty=1, ts=ts,
                reason=f"low9: setup={sig['count']} spot={spot:.3f}",
                px=float(pick.get("bid") or 0.0) or None,
            )
            if r.get("ok"):
                wave[sym] = True
                out["actions"].append({"action": "sell_open", "symbol": sym,
                                       "code": pick["code"],
                                       "yield_pct": pick.get("net_yield_pct"),
                                       "detail": r.get("fill")})
                self._append_event({"type": "sell_open", "code": pick["code"],
                                    "symbol": sym,
                                    "yield_pct": pick.get("net_yield_pct")})
                self._log(f"SELL OPEN {pick['code']} | setup={sig['count']} "
                          f"spot={spot:.3f} y={pick.get('net_yield_pct')}%")
            else:
                out["actions"].append({"action": "skip", "symbol": sym,
                                       "reason": r.get("reason")})

        _store_ledger(st, led)
        meta["rounds"] = int(meta.get("rounds") or 0) + 1
        meta["last_round"] = ts
        save_state(st)

        snap = led.snapshot()
        out["equity"] = snap["equity"]
        return out

    # ── 内部 ──
    def _first_cross(self, df, last_bar: dict, sym: str) -> dict | None:
        """低9 首穿（每根 bar 只判一次；``last_bar`` 用于跨轮去重）。"""
        try:
            row = df.iloc[-1]
            bar_key = str(row.get("dt"))
        except Exception:       # noqa: BLE001
            return None
        if not bar_key or last_bar.get(sym) == bar_key:
            return None
        last_bar[sym] = bar_key
        cnt = int(row.get("buy_setup_count") or 0)
        prev = int(df["buy_setup_count"].iloc[-2]) if len(df) >= 2 else 0
        if cnt >= ENTRY_COUNT and prev < ENTRY_COUNT:
            return {"count": cnt, "bar": bar_key, "close": float(row.get("close") or 0.0)}
        return None

    def _chain(self, sym: str, spot: float, today: date) -> list[dict]:
        """构造选档用的链（只取该标的的 put，按盘口 bid 填充）。"""
        try:
            items = ac.contracts(creg._get_storage_dir()).get("items") or []
        except Exception as e:      # noqa: BLE001
            self._log(f"合约清单获取失败: {type(e).__name__}: {e}")
            return []
        cands = [i for i in items if i.get("undl") == sym and i.get("right") == "P"]
        qs = _quotes([i["code"] for i in cands])
        chain: list[dict] = []
        for i in cands:
            q = qs.get(i["code"]) or {}
            bid = float(q.get("bid") or 0.0)
            chain.append({"code": i["code"], "underlying": sym,
                          "strike": float(i["strike"]), "expiry": str(i["expire"]),
                          "bid": bid})
        return chain


# ── 模块级单例 ────────────────────────────────────────────

RUNNER = PaperRunner()


def sync() -> dict | None:
    return RUNNER.sync()


def stop() -> dict:
    return RUNNER.stop()


def runner_state() -> dict:
    return RUNNER.status()


def state(limit_fills: int = FILLS_KEEP) -> dict:
    """页面 payload：配置 + 运行态 + 账本快照 + 成交 + 事件 + 一致性自检。"""
    try:
        st = load_state()
        led = ledger_of(st)
        err = ""
    except Exception as e:      # noqa: BLE001 — 状态坏了也要能打开页面
        st, led, err = {}, pl.PaperLedger(), f"{type(e).__name__}: {e}"
    snap = led.snapshot() if led else {}
    fills = list(led.fills)[-max(1, int(limit_fills)):] if led else []
    fills.reverse()             # 最新在前
    return {
        "label": "模拟账本（不接券商）",
        "config": load_config(),
        "runner": runner_state(),
        "ledger": snap,
        "consistency": led.check_consistency() if led else {"ok": False},
        "fills": fills,
        "events": RUNNER.load_events(20),
        "meta": {k: v for k, v in (st.get("meta") or {}).items() if k != "last_bar"},
        "error": err,
    }


def round_once() -> dict:
    """手动跑一轮（页面「立即跑一轮」；不改变开关状态）。"""
    cfg = load_config()
    return RUNNER.do_round(cfg)


__all__ = [
    "PaperRunner", "RUNNER", "state", "sync", "stop", "round_once",
    "load_config", "save_config", "default_config", "default_params",
    "load_state", "save_state", "reset_state", "paper_dir",
    "DEFAULT_SYMBOLS", "DEFAULT_INTERVAL_S", "INTERVAL_RANGE",
]
