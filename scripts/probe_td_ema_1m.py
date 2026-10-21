#!/usr/bin/env python3
"""Read-only probe: TD buy9 x EMA144 on OKX 1m (6 symbols, N days).

C73 阶段 0 探针（只读，不接生产、不改任何交易路径）

统计口径（预先写死，勿看结果后改）：
  · 数据：OKX /api/v5/market/history-candles, bar=1m, confirm=='1' 才用
  · 预热：评估窗口之前额外 1500 根（用于 EMA 收敛实测对比）
  · TD：nanobot_quant.strategies.td_sequential.calculate_series(DEFAULT_TD_PARAMS)
  · buy9 事件：buy_setup_count >= 9 且前一根 < 9（first_cross）
  · 趋势变量：EMA144（同周期同序列），slope3 = (EMA_t - EMA_{t-3}) / (3*ATR20_t)
  · 闸门：slope3 > 0
  · 出场：sell_setup_count >= 9 或 sell_countdown_count >= 13，或持仓达 480 根（8h）
  · 撮合约定：信号 bar 收盘价成交（乐观口径，回测同款）
  · 成本：单次往返 0.3%（0.1%x2 手续费 + 0.1% 滑点）
  · 暴露：持仓根数 / 评估根数（同一时刻每标的只持一仓）
  · EMA 验收：|EMA_500 − EMA_1500| / ATR20 在评估窗口内的最大值

输出：JSON + Markdown 到 --out 目录。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

from nanobot_quant.strategies.td_sequential import calculate_series  # noqa: E402
from nanobot_quant.td_params import DEFAULT_TD_PARAMS  # noqa: E402

BASE = "https://www.okx.com/api/v5/market/history-candles"
MIN_INTERVAL = 1.0 / 8.0  # 8 req/s（OKX history-candles 上限 20/2s，留余量）
_RL = threading.Lock()
_LAST = [0.0]


def _throttle() -> None:
    with _RL:
        dt = time.time() - _LAST[0]
        if dt < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - dt)
        _LAST[0] = time.time()


def fetch_page(inst: str, after_ms: int, limit: int = 100, retries: int = 4):
    url = f"{BASE}?instId={inst}&bar=1m&limit={limit}&after={after_ms}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for k in range(retries):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read().decode())
            if str(d.get("code")) != "0":
                raise RuntimeError(f"code={d.get('code')} msg={d.get('msg')}")
            return d.get("data", [])
        except Exception as e:  # noqa: BLE001
            last_err = e
            if k == retries - 1:
                raise
            time.sleep(1.5 * (k + 1))
    raise last_err  # pragma: no cover


def download(inst: str, start_ms: int, cache_path: str, log) -> pd.DataFrame:
    if os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path)
            if len(df) > 1000 and int(df["ts"].min()) <= start_ms + 120_000:
                log(f"{inst}: cache hit ({len(df)} bars)")
                return df
        except Exception:  # noqa: BLE001
            log(f"{inst}: bad cache, refetch")

    after = int(time.time() * 1000) + 60_000
    rows: list = []
    n_req = 0
    t0 = time.time()
    while True:
        page = fetch_page(inst, after)
        n_req += 1
        if not page:
            break
        rows.extend(page)
        oldest = int(page[-1][0])
        if oldest <= start_ms or len(page) < 100:
            break
        after = oldest
        if n_req % 200 == 0:
            rate = n_req / max(time.time() - t0, 1e-9)
            log(f"{inst}: {n_req} req ({rate:.1f}/s) oldest={_fmt_ms(oldest)}")

    arr = pd.DataFrame(rows, columns=[
        "ts", "Open", "High", "Low", "Close", "Volume",
        "volCcy", "volCcyQuote", "confirm",
    ])
    for c in ("ts", "confirm"):
        arr[c] = arr[c].astype(str) if c == "confirm" else arr[c].astype("int64")
    for c in ("Open", "High", "Low", "Close", "Volume"):
        arr[c] = arr[c].astype(float)
    arr = arr[arr["confirm"] == "1"]
    arr = arr[arr["ts"] >= start_ms]
    arr = arr.drop(columns=["confirm", "volCcy", "volCcyQuote"])
    arr = arr.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    arr.to_csv(cache_path, index=False, compression="gzip")
    log(f"{inst}: done {n_req} req, {len(arr)} bars")
    return arr


def _fmt_ms(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ms) / 1000))


def build_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    out.index = pd.to_datetime(out["ts"], unit="ms", utc=True)
    out[["Open", "High", "Low", "Close", "Volume"]] = out[
        ["Open", "High", "Low", "Close", "Volume"]].astype(float)
    return out


def ema_series(close: pd.Series, n: int, start: int | None = None) -> pd.Series:
    s = close if start is None else close.iloc[start:]
    return s.ewm(span=n, adjust=False).mean()


def atr_series(df: pd.DataFrame, n: int = 20) -> pd.Series:
    prev = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev).abs(),
        (df["Low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def simulate(entry_mask: pd.Series, sell_setup: pd.Series, sell_cd: pd.Series,
             close: pd.Series, eval_mask: pd.Series, max_hold: int = 480):
    """One position at a time; entry at signal bar close, exit at exit bar close."""
    n = len(close)
    in_pos = False
    entry_i = 0
    pos_bars = 0
    trades = []
    for i in range(n):
        if not bool(eval_mask.iloc[i]):
            continue
        if in_pos:
            held = i - entry_i
            if (int(sell_setup.iloc[i]) >= 9 or int(sell_cd.iloc[i]) >= 13
                    or held >= max_hold):
                pos_bars += held
                ret = float(close.iloc[i]) / float(close.iloc[entry_i]) - 1.0 - 0.003
                trades.append({"entry_ts": int(entry_i), "exit_ts": i,
                               "held": held, "ret": ret})
                in_pos = False
        elif bool(entry_mask.iloc[i]):
            in_pos = True
            entry_i = i
    if in_pos:
        pos_bars += (n - 1 - entry_i)
    return pos_bars, trades


def analyze(inst: str, df: pd.DataFrame, eval_start_ms: int, warmup: int) -> dict:
    frame = build_frame(df)
    n = len(frame)
    idx_eval = int((frame["ts"] >= eval_start_ms).values.argmax()) if (frame["ts"] >= eval_start_ms).any() else n
    eval_mask = pd.Series(False, index=frame.index)
    eval_mask.iloc[idx_eval:] = True

    td = calculate_series(
        frame[["Open", "High", "Low", "Close", "Volume"]].copy(),
        params=DEFAULT_TD_PARAMS,
    )
    bs = td["buy_setup_count"].astype(int)
    ss = td["sell_setup_count"].astype(int)
    cds = td["sell_countdown_count"].astype(int)
    buy9 = (bs >= 9) & (bs.shift(1).fillna(0).astype(int) < 9)

    close = frame["Close"]
    atr = atr_series(frame)
    ema = ema_series(close, 144)
    slope3 = (ema - ema.shift(3)) / (3.0 * atr.replace(0.0, np.nan))
    gate = slope3 > 0

    # EMA 收敛实测：EMA500（从 eval_start-500 起算）vs EMA（全量 1500 预热）
    start_trunc = max(idx_eval - warmup, 0)
    ema_trunc = ema_series(close, 144, start=start_trunc)
    ema_err = (ema_trunc - ema).abs() / atr.replace(0.0, np.nan)
    err_window = ema_err.iloc[idx_eval: idx_eval + 300]
    max_err_atr = float(err_window.max()) if len(err_window) else float("nan")

    ev = buy9 & eval_mask
    n_events = int(ev.sum())
    n_pass = int((ev & gate).sum())
    pass_rate = (n_pass / n_events) if n_events else float("nan")

    pos_base, trades_base = simulate(ev, ss, cds, close, eval_mask)
    pos_gate, trades_gate = simulate(ev & gate, ss, cds, close, eval_mask)
    n_eval = int(eval_mask.sum())

    def _stats(trades):
        if not trades:
            return {"n": 0, "mean_ret": None, "median_ret": None,
                    "win_rate": None, "mean_hold": None}
        r = np.array([t["ret"] for t in trades])
        h = np.array([t["held"] for t in trades])
        return {"n": len(trades), "mean_ret": float(r.mean()),
                "median_ret": float(np.median(r)),
                "win_rate": float((r > 0).mean()), "mean_hold": float(h.mean())}

    gaps = np.diff(frame["ts"].values) // 1000
    return {
        "symbol": inst,
        "bars": n,
        "eval_bars": n_eval,
        "span": [int(frame["ts"].iloc[0]), int(frame["ts"].iloc[-1])],
        "gap_count_gt60s": int((gaps > 60).sum()),
        "gap_max_s": int(gaps.max()) if len(gaps) else 0,
        "ema_err_max_atr": max_err_atr,
        "events_raw": n_events,
        "events_pass": n_pass,
        "pass_rate": pass_rate,
        "exposure_base": pos_base / n_eval if n_eval else float("nan"),
        "exposure_gate": pos_gate / n_eval if n_eval else float("nan"),
        "trades_base": _stats(trades_base),
        "trades_gate": _stats(trades_gate),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--pre", type=int, default=1500, help="eval window 之前的预热根数")
    ap.add_argument("--symbols", default="SOL,BTC,ETH,XRP,DOGE,ADA")
    ap.add_argument("--out", default="probe_td_ema_1m")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    log_path = os.path.join(args.out, "run.log")

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S', time.gmtime())}] {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    now_ms = int(time.time() * 1000)
    eval_start_ms = now_ms - args.days * 86400 * 1000
    fetch_start_ms = eval_start_ms - args.pre * 60_000
    log(f"eval_start={_fmt_ms(eval_start_ms)} fetch_start={_fmt_ms(fetch_start_ms)}")

    insts = [f"{s.strip().upper()}-USDT"
             for s in args.symbols.split(",") if s.strip()]

    # 阶段 1：并行下载（I/O bound，共享全局限速器）
    frames = {}
    with ThreadPoolExecutor(max_workers=len(insts) or 1) as ex:
        def _dl(inst):
            cache = os.path.join(args.out, f"{inst}_1m.csv.gz")
            return inst, download(inst, fetch_start_ms, cache, log)
        for fut in [ex.submit(_dl, i) for i in insts]:
            try:
                inst, raw = fut.result()
                frames[inst] = raw
                log(f"{inst}: downloaded {len(raw)} bars")
            except Exception as e:  # noqa: BLE001
                log(f"download FAILED: {type(e).__name__}: {e}")

    # 阶段 2：串行分析（CPU bound，避免抢核）
    results = []
    for inst in insts:
        if inst not in frames:
            results.append({"symbol": inst, "error": "download failed"})
            continue
        try:
            log(f"{inst}: analyzing {len(frames[inst])} bars ...")
            res = analyze(inst, frames[inst], eval_start_ms, args.pre)
            results.append(res)
            log(f"{inst}: events={res['events_raw']} pass={res['events_pass']} "
                f"rate={res['pass_rate']:.3f} expo {res['exposure_base']:.3f}->"
                f"{res['exposure_gate']:.3f} ema_err={res['ema_err_max_atr']:.4f}ATR")
        except Exception as e:  # noqa: BLE001
            log(f"{inst}: FAILED {type(e).__name__}: {e}")
            results.append({"symbol": inst, "error": f"{type(e).__name__}: {e}"})

    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
               "days": args.days, "pre": args.pre,
               "eval_start_ms": eval_start_ms, "results": results}
    with open(os.path.join(args.out, "result.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log("done -> result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
