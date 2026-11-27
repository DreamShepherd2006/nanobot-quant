"""A 股 ETF 期权模拟账本 —— 历史数据离线回放（页面 / CLI 共用同一份实现）。

为什么在模块里而不是只放 scripts/
---------------------------------
原先只有 ``scripts/ashare_paper_replay.py``（自测工具）。2026-11-27 起页面
``/config/ashare`` 也要跑历史回放 —— 若两处各写一套，选档 / 出场 / 记账
必然漂移（口径分叉是本项目反复踩过的坑）。因此：**计算全部在这一份**，
CLI 脚本只做薄封装，HTTP handler 只做转发与状态。

边界（读数前必看）
------------------
- **只读**：不写实时账本 ``state.json``、不下单、不碰券商；结果单独落
  ``ashare_paper/replay/<run_id>.json``（保留最近 :data:`KEEP_RESULTS` 份）。
- **不是回测引擎**：bid 用 1m close 代理（免费通道没有历史盘口），不做参数
  扫描、不产出策略结论。
- **确定性**：无随机、无网络（数据全部来自 CSV），同输入重跑逐位一致。

数据依赖（QMT 导出，见 skill ``qmt-option-data``）
--------------------------------------------------
- ETF 1m：``stime,close,high,low,open,time,...``（时间列 CST）
- 期权 1m：``ts,code,open,high,low,close,volume,amount``（多合约同分片）
- 合约清单：``undl,code,month,type,strike,expire,mult,tick,...``
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import fields as dc_fields
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from nanobot_quant import credential_registry as creg
from nanobot_quant.ashare.paper_ledger import PaperLedger
from nanobot_quant.ashare.paper_strategy import (
    ARM_A,
    ARM_B,
    ARM_LABELS,
    PaperParams,
    can_open,
    pick_put,
    should_close,
    should_close_high9,
)
from nanobot_quant.ashare.td_panel import setup_by_session

LOG_PREFIX = "[ASHARE-REPLAY]"
DIR_NAME = "replay"                 # {paper_dir}/replay/<run_id>.json
KEEP_RESULTS = 10                   # 自动清理：只留最近 N 份
PROGRESS_EVERY_BARS = 2000          # 进度回调节流（按 bar 数）
DATASET_CACHE_S = 60.0              # 数据集扫描缓存（页面会反复刷新）
DEFAULT_UNDERLYING = "510050.SH"
DEFAULT_INITIAL = 500_000.0
ARMS_ALL = ("a", "b", "both")

DATA_ROOT_CANDIDATES = (
    "/data/legion/ashare_data",
    "/mnt/workspace/legion/ashare_data",
)


# ── 路径 / 小工具 ──────────────────────────────────────────


def paper_dir() -> Path:
    """与 ``paper_runner.paper_dir()`` 同一目录（同源，不另建一套）。"""
    return Path(creg._get_storage_dir()) / "ashare_paper"


def results_dir() -> Path:
    return paper_dir() / DIR_NAME


def data_root() -> Path:
    """数据根目录：env 覆盖 > 存在的候选 > 第一个（不存在时由上层报错）。"""
    env = os.environ.get("NANOBOT_ASHARE_DATA")
    if env:
        return Path(env)
    for p in DATA_ROOT_CANDIDATES:
        pp = Path(p)
        if pp.is_dir():
            return pp
    return Path(DATA_ROOT_CANDIDATES[0])


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


def _head_line(path: Path, skip: int = 0) -> str:
    """第 ``skip+1`` 条非空行（QMT CSV 首行是表头，取数据行要 ``skip=1``）。"""
    seen = 0
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            if seen >= skip:
                return line.rstrip("\r\n")
            seen += 1
    return ""


def _tail_line(path: Path, nbytes: int = 8192) -> str:
    """读文件末尾一段，取最后一条非空行（CSV 首列即时间，够用且不必全文读）。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - nbytes))
            chunk = f.read().decode("utf-8-sig", errors="replace")
    except OSError:
        return ""
    lines = [x for x in chunk.splitlines() if x.strip()]
    return lines[-1] if lines else ""


def _fmt_ts(t: str | None) -> str:
    """``20261127113000`` → ``2026-11-27 11:30``（展示用，异常原样返回）。"""
    if not t or len(str(t)) < 12:
        return str(t or "")
    s = str(t)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"


def _sym_of_etf_file(name: str) -> str:
    m = re.match(r"^(\d{6})_([A-Z]{2})_1m\.csv$", name, re.I)
    if m:
        return f"{m.group(1)}.{m.group(2).upper()}"
    return name.replace("_1m.csv", "").replace("_", ".")


# ── 数据集扫描（页面下拉的数据来源） ──────────────────────

_DS_CACHE: dict = {"ts": 0.0, "root": "", "data": None}


def list_datasets(root: str | Path | None = None, *, force: bool = False) -> dict:
    """扫 ``{root}/qmt-etf-1m/run_*`` 与 ``{root}/qmt-option-1m/*``。

    只读，返回结构供页面下拉；结果缓存 :data:`DATASET_CACHE_S` 秒。
    目录缺失不报错（返回空列表 + ``notes``），让页面能打开并说明原因。
    """
    base = Path(root) if root else data_root()
    key = str(base)
    now = time.time()
    if (not force and _DS_CACHE["data"] is not None
            and _DS_CACHE["root"] == key
            and now - _DS_CACHE["ts"] < DATASET_CACHE_S):
        return _DS_CACHE["data"]

    notes: list[str] = []
    etf_runs: list[dict] = []
    opt_runs: list[dict] = []
    contracts: list[dict] = []

    etf_base = base / "qmt-etf-1m"
    if etf_base.is_dir():
        for run in sorted(etf_base.glob("run_*"), reverse=True):
            files = sorted(run.glob("*_1m.csv"))
            if not files:
                continue
            items = []
            for f in files:
                items.append({
                    "symbol": _sym_of_etf_file(f.name),
                    "file": f.name,
                    "first": _head_line(f, 1).split(",")[0],
                    "last": _tail_line(f).split(",")[0],
                    "size_mb": round(f.stat().st_size / 1048576, 2),
                })
            etf_runs.append({"name": run.name, "path": str(run), "files": items})
    else:
        notes.append(f"未找到 ETF 数据目录：{etf_base}")

    opt_base = base / "qmt-option-1m"
    if opt_base.is_dir():
        for run in sorted(opt_base.glob("run_*"), reverse=True):
            shards = sorted(run.glob("opt1m_*.csv"))
            if not shards:
                continue
            firsts, lasts = [], []
            for f in shards:
                a = _head_line(f, 1).split(",")[0]
                b = _tail_line(f).split(",")[0]
                if a:
                    firsts.append(a)
                if b:
                    lasts.append(b)
            pm = re.findall(r"opt1m_([0-9]{6}[A-Z]{2})_", " ".join(x.name for x in shards))
            opt_runs.append({
                "name": run.name,
                "path": str(run),
                "shards": len(shards),
                "underlyings": sorted(set(pm)),
                "first": min(firsts) if firsts else "",
                "last": max(lasts) if lasts else "",
            })
    else:
        notes.append(f"未找到期权数据目录：{opt_base}")

    if opt_base.is_dir():
        for p in sorted(opt_base.glob("*/contracts_all.csv")):
            try:
                d = pd.read_csv(p, encoding="utf-8-sig")
                contracts.append({
                    "name": f"{p.parent.name}/{p.name}",
                    "path": str(p),
                    "rows": int(len(d)),
                    "months": sorted({str(x) for x in d.get("month", pd.Series(dtype=str)).unique()}),
                    "underlyings": sorted({str(x) for x in d.get("undl", pd.Series(dtype=str)).unique()}),
                })
            except Exception as e:  # noqa: BLE001 — 单个文件坏了不拖垮下拉
                notes.append(f"{p} 读取失败：{type(e).__name__}: {e}")

    data = {"root": str(base), "etf_runs": etf_runs, "opt_runs": opt_runs,
            "contracts": contracts, "notes": notes,
            "defaults": {"underlying": DEFAULT_UNDERLYING,
                         "initial": DEFAULT_INITIAL, "arms": "both"}}
    _DS_CACHE.update({"ts": now, "root": key, "data": data})
    return data


# ── 数据载入 ──────────────────────────────────────────────


def load_etf(path: str) -> pd.DataFrame:
    d = pd.read_csv(path, encoding="utf-8-sig")
    d = d.loc[:, ~d.columns.duplicated()]
    d["dt"] = pd.to_datetime(d["stime"].astype(str), format="%Y%m%d%H%M%S")
    # ``setup_by_session`` 按 ``date`` 分组（隔夜从 0 重数），引擎需要 O/H/L/C/V
    d["date"] = d["dt"].dt.date
    for c in ("open", "volume"):
        if c not in d.columns:
            d[c] = 0.0
    return (d[["dt", "date", "open", "high", "low", "close", "volume"]]
            .sort_values("dt").reset_index(drop=True))


def load_contracts(path: str, underlying: str) -> dict[str, dict]:
    d = pd.read_csv(path, encoding="utf-8-sig")
    d = d[d["undl"] == underlying]
    meta: dict[str, dict] = {}
    for _, r in d.iterrows():
        meta[str(r["code"])] = {
            "code": str(r["code"]),
            "underlying": str(r["undl"]),
            "type": str(r["type"]).upper(),
            "strike": float(r["strike"]),
            "expiry": str(int(r["expire"])),
            "mult": int(r["mult"]),
        }
    return meta


def load_option_closes(opt_dirs, underlying: str,
                       codes: set[str]) -> dict[str, pd.Series]:
    """把分片里的 1m close 汇成 ``code → Series(index=dt, close)``（已 ffill）。

    ``opt_dirs`` 支持**多个目录**：QMT 按批次导出（基线 + 每日增量各一个
    ``run_*``），只有合并多批才覆盖到最新交易日。同一合约在多批里出现时按
    时间去重、后到者胜（``keep="last"``）。
    """
    if isinstance(opt_dirs, (str, os.PathLike)):
        opt_dirs = [opt_dirs]
    dirs = [str(x) for x in opt_dirs]
    prefix = underlying.replace(".", "")
    files: list[str] = []
    for d in dirs:
        files.extend(sorted(glob.glob(os.path.join(d, f"opt1m_{prefix}_*.csv"))))
    if not files:
        raise ValueError(
            f"未找到期权分片：{prefix} 在 {dirs}（期望 opt1m_{prefix}_*.csv）")
    frames = []
    for f in files:
        d = pd.read_csv(f, encoding="utf-8-sig", usecols=["ts", "code", "close"])
        d = d[d["code"].isin(codes)]
        if len(d):
            frames.append(d)
    if not frames:
        return {}
    d = pd.concat(frames, ignore_index=True)
    d["dt"] = pd.to_datetime(d["ts"].astype(str), format="%Y%m%d%H%M%S")
    out: dict[str, pd.Series] = {}
    for code, g in d.groupby("code", sort=False):
        s = g.set_index("dt")["close"].sort_index()
        s = s[~s.index.duplicated(keep="last")].ffill()
        out[str(code)] = s
    return out


# ── 回放 ──────────────────────────────────────────────────


def run(etf: pd.DataFrame, contracts: dict[str, dict],
        closes: dict[str, pd.Series], *, underlying: str,
        initial: float, params: PaperParams, arm: str = "A",
        progress=None) -> dict:
    led = PaperLedger(initial_cash=initial, mult=params.mult,
                      fee_open=params.fee_open)
    puts = {c: m for c, m in contracts.items() if m["type"] == "P" and c in closes}
    if not puts:
        raise ValueError("合约表里没有可用的 put（检查 contracts 的 type 列与分片覆盖）")

    etf = setup_by_session(etf, cmp_length=4)
    etf["prev_buy"] = etf["buy_setup_count"].shift(1).fillna(0)
    etf["signal"] = (etf["buy_setup_count"] >= 9) & (etf["prev_buy"] < 9)

    # 「一浪」= 该标的从空仓到再次空仓；平仓后清位，允许下一浪再开
    wave_done: set[str] = set()
    stats = defaultdict(int)
    daily: list[dict] = []

    n = len(etf)
    for i in range(1, n):
        row = etf.iloc[i]
        t: datetime = row["dt"]
        spot = float(row["close"])
        today: date = t.date()
        ts = t.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        last_of_day = (i == n - 1) or (etf.iloc[i + 1]["dt"].date() != today)

        if progress is not None and i % PROGRESS_EVERY_BARS == 0:
            progress(i, n, arm)

        # 1) 到期结算（当日最后一根 bar，用收盘价作为结算参考）
        for code in list(led.shorts):
            sp = led.shorts[code]
            if today >= datetime.strptime(sp.expiry, "%Y%m%d").date() and last_of_day:
                r = led.settle_expiry(code=code, settle_spot=spot, ts=ts)
                if r["ok"]:
                    stats[r["fill"]["action"]] += 1
                    wave_done.discard(sp.underlying)
                continue

        # 2) 更新 mark（义务仓按期权现价、现货按 ETF 现价）
        for code in list(led.shorts):
            s = closes.get(code)
            if s is not None:
                px = s.get(t)
                if pd.notna(px) and px > 0:
                    led.mark_short(code, float(px))
        led.mark_etf(underlying, spot)

        # 3) 出场
        for code in list(led.shorts):
            sp = led.shorts[code]
            mark = sp.mark_px or sp.entry_px
            if arm == ARM_B:
                ok, why = should_close_high9(
                    sp.to_dict(), mark, today, params,
                    sell_setup=int(row.get("sell_setup_count") or 0))
            else:
                ok, why = should_close(sp.to_dict(), mark, today, params)
            if not ok:
                continue
            r = led.buy_close(code=code, bar_close=mark, ts=ts, reason=why)
            if r["ok"]:
                stats["buy_close"] += 1
                if why.startswith("take_profit"):
                    stats["tp"] += 1
                elif why.startswith("high9_profit"):
                    stats["high9"] += 1
                else:
                    stats["dte_force"] += 1
                wave_done.discard(sp.underlying)

        # 4) 信号 → 选档 → 开仓
        if bool(row["signal"]):
            stats["signal"] += 1
            ok_cnt, _ = can_open([s.to_dict() for s in led.shorts.values()],
                                 underlying, params)
            if not ok_cnt:
                stats["block_quota"] += 1
            elif params.max_per_wave and underlying in wave_done:
                stats["block_wave"] += 1
            else:
                chain = []
                for code, m in puts.items():
                    s = closes.get(code)
                    px = s.get(t) if s is not None else None
                    if px is None or pd.isna(px) or px <= 0:
                        continue
                    chain.append({"code": code, "underlying": m["underlying"],
                                  "strike": m["strike"], "expiry": m["expiry"],
                                  "bid": float(px)})
                pick = pick_put(chain, spot, today, params)
                if pick is None:
                    stats["block_no_candidate"] += 1
                else:
                    r = led.sell_open(code=pick["code"], underlying=underlying,
                                      strike=pick["strike"], expiry=pick["expiry"],
                                      qty=1, bar_close=float(pick["bid"]), ts=ts,
                                      reason=f"buy9 setup={int(row['buy_setup_count'])}")
                    if r["ok"]:
                        stats["sell_open"] += 1
                        wave_done.add(underlying)
                    else:
                        why = r.get("reason", "")
                        stats["block_price" if "成交价" in why else "block_funds"] += 1

        if last_of_day:
            snap = led.snapshot()
            daily.append({"date": today.isoformat(), "equity": snap["equity"],
                          "cash": snap["cash"], "open": len(led.shorts)})

    return {"ledger": led, "stats": dict(stats), "daily": daily}


def _summary(res: dict, params: PaperParams, arm: str = ARM_A,
             data_window: dict | None = None) -> str:
    led: PaperLedger = res["ledger"]
    st = res["stats"]
    snap = led.snapshot()
    cons = led.check_consistency()

    closes = [f for f in led.fills if f["action"] == "buy_close"]
    wins = [f for f in closes if (f.get("realized") or 0) > 0]
    assigns = [f for f in led.fills if f["action"] == "assign"]

    lines = [
        f"## 臂 {arm} — {ARM_LABELS.get(arm, arm)}",
        "",
        f"- 初始资金：{snap['initial_cash']:,.2f} 元",
        f"- 期末净值：{snap['equity']:,.2f} 元（{snap['return_pct']:+.4f}%）",
        f"- 已实现：{snap['realized']:,.2f} 元 · 未实现：{snap['unrealized']:,.2f} 元",
        f"- 担保占用：{snap['collateral_locked']:,.2f} 元 · 可用现金：{snap['available_cash']:,.2f} 元",
        f"- 净值一致性自检：{'✅ 通过' if cons['ok'] else '❌ 不符'}"
        f"（{cons['equity_minus_initial']:+.2f} vs {cons['realized_plus_unrealized']:+.2f}）",
        "",
        "### 动作统计",
        "",
        "| 项 | 次数 |",
        "|:--|--:|",
        f"| TD 买9 信号 | {st.get('signal', 0)} |",
        f"| 卖出开仓 | {st.get('sell_open', 0)} |",
        f"| 买入平仓 | {st.get('buy_close', 0)} |",
        (f"| ↳ 止盈 | {st.get('tp', 0)} |" if arm == ARM_A
         else f"| ↳ 高9 + 浮盈 | {st.get('high9', 0)} |"),
        f"| ↳ DTE 强平 | {st.get('dte_force', 0)} |",
        f"| 行权接货 | {st.get('assign', 0)} |",
        f"| 到期作废 | {st.get('settle_otm', 0)} |",
        f"| 拦：额度 | {st.get('block_quota', 0)} |",
        f"| 拦：同浪 | {st.get('block_wave', 0)} |",
        f"| 拦：无合适合约 | {st.get('block_no_candidate', 0)} |",
        f"| 拦：资金不足 | {st.get('block_funds', 0)} |",
        f"| 拦：成交价非正 | {st.get('block_price', 0)} |",
        "",
    ]
    if closes:
        avg = sum((f.get("realized") or 0) for f in closes) / len(closes)
        lines += [
            "### 平仓统计",
            "",
            f"- 平仓笔数：{len(closes)}（胜 {len(wins)} / 负 {len(closes) - len(wins)}，"
            f"胜率 {len(wins) / len(closes) * 100:.1f}%）",
            f"- 每笔已实现均值：{avg:+.2f} 元/张",
            "",
        ]
    if assigns:
        lines.append(f"- 接货合计：{sum(f['assign_cost'] for f in assigns):,.2f} 元的 ETF")
        lines.append("")
    lines += ["### 成交明细（前 20 条）", "",
              "| 时间 | 动作 | 合约 | 行权价 | 张数 | 价 | 已实现 | 原因 |",
              "|:--|:--|:--|--:|--:|--:|--:|:--|"]
    for f in led.fills[:20]:
        lines.append(
            f"| {f['ts'][:16]} | {f['action']} | {f['code']} | "
            f"{f.get('strike', '')} | {f.get('qty', '')} | {f.get('px', '')} | "
            f"{f.get('realized', '')} | {f.get('reason') or f.get('note', '')} |")
    lines += ["", f"（共 {len(led.fills)} 条成交，此处截取前 20 条）", ""]
    if res["daily"]:
        lines += ["### 按日净值（末 10 个交易日）", "",
                  "| 日期 | 净值 | 现金 | 在仓 |", "|:--|--:|--:|--:|"]
        for d in res["daily"][-10:]:
            lines.append(f"| {d['date']} | {d['equity']:,.2f} | {d['cash']:,.2f} | {d['open']} |")
        lines.append("")
    return "\n".join(lines)


CAVEATS = [
    "- **bid 用 1m close 代理**：免费通道没有历史盘口，期权分钟线又是「无成交沿用上一价」，",
    "  流动性差的深虚值合约报价会偏乐观；实盘下单前必须看真盘口。",
    "- **到期窗口过滤是主要拦截源**：A 股 ETF 期权同时只有 4 个月份，",
    "  「拦：无合适合约」多数来自所选到期档不在 [min_dte, max_dte] 窗口内，",
    "  不是选档 bug —— 查看该时段实际在售月份的 DTE 即可确认。",
    "- **本报告不是策略结论**：只验证账本记账与规则链路，不做参数裁决。",
]


def _arm_row(arm: str, res: dict) -> dict:
    led: PaperLedger = res["ledger"]
    snap = led.snapshot()
    closes = [f for f in led.fills if f["action"] == "buy_close"]
    wins = [f for f in closes if (f.get("realized") or 0) > 0]
    return {
        "arm": arm,
        "label": ARM_LABELS.get(arm, arm),
        "equity": snap["equity"],
        "return_pct": snap["return_pct"],
        "realized": snap["realized"],
        "unrealized": snap["unrealized"],
        "initial_cash": snap["initial_cash"],
        "collateral_locked": snap["collateral_locked"],
        "available_cash": snap["available_cash"],
        "fills": len(led.fills),
        "closes": len(closes),
        "wins": len(wins),
        "win_rate": (len(wins) / len(closes) * 100.0) if closes else 0.0,
        "stats": res["stats"],
        "consistency": led.check_consistency(),
        "daily": res["daily"],
        "shorts": [s.to_dict() for s in led.shorts.values()],
        "fills_detail": list(led.fills),
    }


def _slice_days(etf: pd.DataFrame, closes: dict[str, pd.Series],
                start: str | None, end: str | None):
    """按日期裁剪（``YYYY-MM-DD``）；非法值 fail-closed 报错。"""
    def _parse(v, name):
        if v in (None, ""):
            return None
        try:
            return datetime.strptime(str(v)[:10], "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"{name} 日期格式非法（要 YYYY-MM-DD，收到 {v!r}）")

    s, e = _parse(start, "start"), _parse(end, "end")
    if s and e and s > e:
        raise ValueError(f"区间非法：start {start} 晚于 end {end}")
    if s is not None:
        etf = etf[etf["dt"] >= s]
    if e is not None:
        etf = etf[etf["dt"] < e + timedelta(days=1)]
    etf = etf.reset_index(drop=True)
    out_closes = {}
    for code, series in closes.items():
        x = series
        if s is not None:
            x = x[x.index >= s]
        if e is not None:
            x = x[x.index < e + timedelta(days=1)]
        # 裁剪窗口之前的最后价要保留，避免区间首日被 ffill 顶部截断成 NaN
        if s is not None and len(x) == 0:
            prior = series[series.index < s]
            if len(prior):
                x = prior.tail(1)
        out_closes[code] = x
    return etf, out_closes, s, e


def run_replay(*, etf_csv: str, opt_dirs, contracts_csv: str,
               underlying: str = DEFAULT_UNDERLYING,
               initial: float = DEFAULT_INITIAL, arms: str = "both",
               tp: float | None = None, force_dte: int | None = None,
               min_dte: int | None = None, max_dte: int | None = None,
               start: str | None = None, end: str | None = None,
               progress=None) -> dict:
    """高层入口：校验 → 载入 → 裁剪 → 跑臂 → 结构化结果 + markdown。

    参数 ``arms``：``a`` / ``b`` / ``both``。规则参数留空 = ``PaperParams`` 默认。
    """
    for p in [etf_csv, contracts_csv, *([opt_dirs] if isinstance(opt_dirs, str) else opt_dirs)]:
        if not p or not os.path.exists(str(p)):
            raise ValueError(f"路径不存在：{p}")
    if arms not in ARMS_ALL:
        raise ValueError(f"arms 只能是 {ARMS_ALL}（收到 {arms!r}）")
    try:
        initial = float(initial)
    except (TypeError, ValueError):
        raise ValueError(f"初始资金必须是数字（{initial!r}）")
    if initial <= 0:
        raise ValueError("初始资金必须为正")

    kwargs = {}
    if tp is not None:
        kwargs["take_profit_pct"] = float(tp)
    if force_dte is not None:
        kwargs["force_close_dte"] = int(force_dte)
    if min_dte is not None:
        kwargs["min_dte"] = int(min_dte)
    if max_dte is not None:
        kwargs["max_dte"] = int(max_dte)
    params = PaperParams(**kwargs)

    def _prog(stage, done=0, total=0, arm=""):
        if progress is not None:
            progress({"stage": stage, "done": done, "total": total, "arm": arm})

    _prog("load_etf")
    etf_all = load_etf(etf_csv)
    _prog("load_contracts")
    contracts = load_contracts(contracts_csv, underlying)
    codes = {c for c, m in contracts.items() if m["type"] == "P"}
    if not codes:
        raise ValueError(f"合约清单里没有 {underlying} 的 put（检查 undl 列）")
    _prog("load_options")
    closes_all = load_option_closes(opt_dirs, underlying, codes)
    if not closes_all:
        raise ValueError("选中的期权批次里没有该标的的 put 行情（检查 run 目录与标的）")

    etf, closes, s, e = _slice_days(etf_all, closes_all, start, end)
    if len(etf) < 20:
        raise ValueError(f"区间内 ETF 只有 {len(etf)} 根 1m bar，太短（至少 20）")

    opt_first = min(x.index.min() for x in closes.values() if len(x))
    opt_last = max(x.index.max() for x in closes.values() if len(x))
    window = {
        "etf_first": etf["dt"].iloc[0].strftime("%Y-%m-%d %H:%M"),
        "etf_last": etf["dt"].iloc[-1].strftime("%Y-%m-%d %H:%M"),
        "etf_bars": int(len(etf)),
        "opt_first": opt_first.strftime("%Y-%m-%d %H:%M"),
        "opt_last": opt_last.strftime("%Y-%m-%d %H:%M"),
        "opt_codes": len(closes),
        "overlap_first": max(etf["dt"].iloc[0], opt_first).strftime("%Y-%m-%d %H:%M"),
        "overlap_last": min(etf["dt"].iloc[-1], opt_last).strftime("%Y-%m-%d %H:%M"),
    }
    notes: list[str] = []
    if opt_first > etf["dt"].iloc[0]:
        notes.append(f"{window['etf_first']} ~ {window['opt_first']} 无期权行情："
                     "该段的买9 信号不会成交（计入「拦：无合适合约」）。")
    if opt_last < etf["dt"].iloc[-1]:
        notes.append(f"{window['opt_last']} ~ {window['etf_last']} 无期权行情："
                     "该段只按 ETF 行情估值，不会开新仓。")
    window["notes"] = notes

    arm_list = [ARM_A, ARM_B] if arms == "both" else ([ARM_B] if arms == "b" else [ARM_A])
    results: dict[str, dict] = {}
    md_parts: list[str] = []
    for arm in arm_list:
        _prog("replay", 0, len(etf), arm)
        res = run(etf, contracts, closes, underlying=underlying, initial=initial,
                  params=params, arm=arm,
                  progress=(lambda d, t, a: _prog("replay", d, t, a)) if progress else None)
        results[arm] = _arm_row(arm, res)
        md_parts.append(_summary(res, params, arm, window))

    head = [
        "# 模拟账本历史回放",
        "",
        f"- 标的：**{underlying}** · 初始资金：{initial:,.2f} 元",
        f"- 回放区间：{window['etf_first']} ~ {window['etf_last']}（{window['etf_bars']} 根 1m）",
        f"- 期权行情覆盖：{window['opt_first']} ~ {window['opt_last']}"
        f"（{window['opt_codes']} 个 put 合约有行情）",
        f"- 有效重叠区间：**{window['overlap_first']} ~ {window['overlap_last']}**",
        f"- 规则参数：止盈 {params.take_profit_pct:.0f}% · DTE 强平 ≤{params.force_close_dte} · "
        f"到期窗口 [{params.min_dte}, {params.max_dte}] · 每浪 {params.max_per_wave}",
        "",
    ]
    if notes:
        head += ["## ⚠ 数据窗口提示", ""] + [f"- {n}" for n in notes] + [""]
    if len(arm_list) == 2:
        head += ["## 两臂对照", "",
                 "两臂共用同一份 ETF 1m 与期权行情、同一套入场与选档规则，**只差止盈条件**：",
                 f"- A = {ARM_LABELS[ARM_A]}", f"- B = {ARM_LABELS[ARM_B]}", "",
                 "| 臂 | 净值 | 收益率 | 已实现 | 未实现 | 成交笔数 | 平仓笔数 | 胜 | 胜率 |",
                 "|:--|--:|--:|--:|--:|--:|--:|--:|--:|"]
        for arm in arm_list:
            r = results[arm]
            head.append(f"| {arm} | {r['equity']:,.2f} | {r['return_pct']:+.4f}% | "
                        f"{r['realized']:,.2f} | {r['unrealized']:,.2f} | {r['fills']} | "
                        f"{r['closes']} | {r['wins']} | {r['win_rate']:.1f}% |")
        head += ["", "---", ""]

    markdown = ("\n".join(head) + "\n" + "\n".join(md_parts) + "\n"
                + "## 口径与数据限制（读数前必看）\n\n" + "\n".join(CAVEATS) + "\n")
    return {
        "ok": True,
        "created": _now_iso(),
        "input": {
            "etf_csv": str(etf_csv),
            "opt_dirs": [str(x) for x in (opt_dirs if not isinstance(opt_dirs, str) else [opt_dirs])],
            "contracts_csv": str(contracts_csv),
            "underlying": underlying,
            "initial": initial,
            "arms": arms,
            "start": start or "",
            "end": end or "",
            "params": {f.name: getattr(params, f.name) for f in dc_fields(params)},
        },
        "data_window": window,
        "arms": results,
        "markdown": markdown,
    }


def resolve_inputs(body: dict, *, root: str | Path | None = None) -> dict:
    """把页面提交的路径校验成 :func:`run_replay` 的 kwargs。

    只允许 ``data_root`` 之下的 CSV（页面下拉本来就只给这些），任何越界路径
    fail-closed 拒绝 —— 端点只读，但没必要给一个能读任意文件的入口。
    """
    if not isinstance(body, dict):
        raise ValueError("请求体必须是对象")
    base = Path(root) if root else data_root()
    try:
        base_r = base.resolve(strict=True)
    except OSError:
        raise ValueError(f"数据根目录不存在：{base}（先上传 QMT 导出的 CSV）")

    def _under(p, what: str) -> Path:
        if not p:
            raise ValueError(f"缺少 {what}")
        pp = Path(str(p))
        if not pp.is_absolute():
            pp = base_r / pp
        try:
            rp = pp.resolve(strict=True)
        except OSError:
            raise ValueError(f"{what} 路径不存在：{pp}")
        if rp != base_r and base_r not in rp.parents:
            raise ValueError(f"{what} 必须位于数据根目录 {base_r} 之下（收到 {rp}）")
        return rp

    etf = _under(body.get("etf_csv"), "etf_csv")
    contracts = _under(body.get("contracts_csv"), "contracts_csv")
    opts = body.get("opt_dirs") or []
    if isinstance(opts, str):
        opts = [x for x in opts.split(",") if x.strip()]
    if not isinstance(opts, (list, tuple)) or not opts:
        raise ValueError("opt_dirs 不能为空（至少选一个期权批次）")
    out = {
        "etf_csv": str(etf), "contracts_csv": str(contracts),
        "opt_dirs": [str(_under(x, "opt_dirs")) for x in opts],
        "underlying": str(body.get("underlying") or DEFAULT_UNDERLYING).strip().upper(),
        "initial": body.get("initial", DEFAULT_INITIAL),
        "arms": str(body.get("arms") or "both").strip().lower(),
    }
    if not re.match(r"^\d{6}\.[A-Z]{2}$", out["underlying"]):
        raise ValueError(f"标的格式应为 510050.SH（收到 {out['underlying']!r}）")
    for k in ("start", "end"):
        v = body.get(k)
        if v not in (None, ""):
            out[k] = str(v)
    for k in ("tp", "force_dte", "min_dte", "max_dte"):
        v = body.get(k)
        if v not in (None, ""):
            try:
                out[k] = float(v) if k == "tp" else int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} 必须是数字（收到 {v!r}）")
    return out


# ── 结果落盘（保留最近 KEEP_RESULTS 份） ──────────────────


def save_result(payload: dict) -> str:
    rid = payload.get("run_id") or (
        "rp-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4])
    payload["run_id"] = rid
    _atomic_write_json(results_dir() / f"{rid}.json", payload)
    _prune_results()
    return rid


def _prune_results() -> None:
    files = sorted(results_dir().glob("rp-*.json"))
    extra = len(files) - KEEP_RESULTS
    if extra <= 0:
        return
    removed = []
    for p in files[:extra]:
        try:
            os.unlink(p)
            removed.append(p.name)
        except OSError as e:
            print(f"{LOG_PREFIX} 清理旧结果失败 {p.name}: {e}", file=sys.stderr, flush=True)
    if removed:
        print(f"{LOG_PREFIX} 已自动清理 {len(removed)} 份旧回放结果（保留最近 {KEEP_RESULTS} 份）",
              file=sys.stderr, flush=True)


def list_results() -> list[dict]:
    out = []
    for p in sorted(results_dir().glob("rp-*.json"), reverse=True):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            w = d.get("data_window") or {}
            arms = d.get("arms") or {}
            out.append({
                "run_id": d.get("run_id") or p.stem,
                "created": d.get("created") or "",
                "underlying": (d.get("input") or {}).get("underlying"),
                "window": f"{w.get('overlap_first', '')} ~ {w.get('overlap_last', '')}",
                "arms": {k: {"equity": v.get("equity"), "return_pct": v.get("return_pct"),
                             "closes": v.get("closes")} for k, v in arms.items()},
            })
        except Exception as e:  # noqa: BLE001 — 单个文件坏了不拖垮列表
            out.append({"run_id": p.stem, "error": f"{type(e).__name__}: {e}"})
    return out


def load_result(run_id: str) -> dict:
    rid = str(run_id or "")
    if not re.match(r"^rp-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$", rid):
        raise ValueError(f"run_id 非法（{rid!r}）")
    p = results_dir() / f"{rid}.json"
    if not p.exists():
        raise ValueError(f"找不到回放结果 {rid}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ── 后台任务（页面「开始回放」） ─────────────────────────

_JOB_LOCK = threading.Lock()
_JOB: dict = {"running": False, "stage": "idle"}


def job_state() -> dict:
    with _JOB_LOCK:
        j = dict(_JOB)
    j["results"] = list_results()[:KEEP_RESULTS]
    return j


def start_job(**kw) -> dict:
    """启动后台回放线程；同一时刻只允许一个（重复点击 fail-closed 报错）。"""
    with _JOB_LOCK:
        if _JOB.get("running"):
            raise ValueError(f"已有回放在跑（{_JOB.get('run_id') or 'queued'}），请等它结束")
        _JOB.clear()
        _JOB.update({"running": True, "stage": "queued", "started": _now_iso(),
                     "started_mono": time.monotonic(), "done": 0, "total": 0,
                     "arm": "", "error": "", "run_id": "", "elapsed_s": 0.0})
    t = threading.Thread(target=_run_job, kwargs=kw, daemon=True,
                         name="ashare-replay")
    t.start()
    return job_state()


def _run_job(**kw) -> None:
    def _progress(ev: dict):
        with _JOB_LOCK:
            _JOB.update({"stage": ev.get("stage") or _JOB.get("stage"),
                         "done": int(ev.get("done") or 0),
                         "total": int(ev.get("total") or 0),
                         "arm": ev.get("arm") or "",
                         "elapsed_s": round(time.monotonic() - _JOB.get("started_mono", 0), 1)})

    started = time.monotonic()
    try:
        payload = run_replay(progress=_progress, **kw)
        rid = save_result(payload)
        with _JOB_LOCK:
            _JOB.update({"running": False, "stage": "done", "run_id": rid,
                         "elapsed_s": round(time.monotonic() - started, 1),
                         "summary": {k: {"equity": v["equity"], "return_pct": v["return_pct"],
                                         "closes": v["closes"], "wins": v["wins"]}
                                     for k, v in (payload.get("arms") or {}).items()}})
        print(f"{LOG_PREFIX} 回放完成 {rid}（{_JOB['elapsed_s']}s）", file=sys.stderr, flush=True)
    except Exception as e:  # noqa: BLE001 — 失败必须可见，不静默
        with _JOB_LOCK:
            _JOB.update({"running": False, "stage": "error",
                         "error": f"{type(e).__name__}: {e}",
                         "elapsed_s": round(time.monotonic() - started, 1)})
        print(f"{LOG_PREFIX} 回放失败: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


# ── CLI（薄封装：所有计算在模块内，脚本只解析参数） ──────


def _cli_progress(ev: dict) -> None:
    if ev.get("stage") == "replay" and ev.get("done"):
        print(f"[replay] {ev.get('arm')} {ev['done']}/{ev['total']}",
              file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A 股 ETF 期权模拟账本历史回放")
    ap.add_argument("--etf-csv", required=True)
    ap.add_argument("--opt-dir", default="", help="期权分片目录（可多次给，或改用 --opt-dirs）")
    ap.add_argument("--opt-dirs", default="", help="逗号分隔的多批目录（基线 + 增量）")
    ap.add_argument("--contracts", required=True)
    ap.add_argument("--underlying", default=DEFAULT_UNDERLYING)
    ap.add_argument("--initial", type=float, default=DEFAULT_INITIAL)
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--force-dte", type=int, default=None)
    ap.add_argument("--min-dte", type=int, default=None)
    ap.add_argument("--max-dte", type=int, default=None)
    ap.add_argument("--start", default="", help="YYYY-MM-DD（含）")
    ap.add_argument("--end", default="", help="YYYY-MM-DD（含）")
    ap.add_argument("--out", default="", help="写 markdown 到该路径（默认打印）")
    ap.add_argument("--arm", choices=list(ARMS_ALL), default="a")
    a = ap.parse_args(argv)

    dirs = [d for d in (a.opt_dirs or "").split(",") if d.strip()] or [a.opt_dir]
    try:
        payload = run_replay(
            etf_csv=a.etf_csv, opt_dirs=dirs, contracts_csv=a.contracts,
            underlying=a.underlying, initial=a.initial, arms=a.arm,
            tp=a.tp, force_dte=a.force_dte, min_dte=a.min_dte, max_dte=a.max_dte,
            start=a.start or None, end=a.end or None,
            progress=_cli_progress)
    except ValueError as e:
        print(f"[replay] {e}", file=sys.stderr)
        return 2
    w = payload["data_window"]
    print(f"[replay] ETF bars={w['etf_bars']} 期权 {w['opt_first']}~{w['opt_last']} "
          f"({w['opt_codes']} put)", file=sys.stderr)
    md = payload["markdown"]
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"[replay] 已写入 {a.out}", file=sys.stderr)
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARMS_ALL", "DEFAULT_INITIAL", "DEFAULT_UNDERLYING", "KEEP_RESULTS",
    "data_root", "job_state", "list_datasets", "list_results", "load_contracts",
    "load_etf", "load_option_closes", "load_result", "main", "paper_dir",
    "resolve_inputs", "results_dir", "run", "run_replay", "save_result", "start_job",
]
