"""TD Sequential 可视化分析页（/config/td-table）— 实时快照 + 历史区间分析。

定位：辅助分析工具（看趋势演变、验证 9 信号质量），不构成买卖交易、
不模拟成交。双 tab：

- Tab ① 实时快照：最近 N 根 K 线的 setup/countdown/TDST/score 轨迹，
  高亮信号行（setup 达 entry_setup/exit_setup 阈值）与 setup 启动行（count == 1）。
- Tab ② 历史区间分析：任意起止区间内**所有** K 线 + 9 信号回溯统计
  （每个 count==setup 信号未来 3/5/10 根涨跌，聚合方向胜率）。

数据流：ticker → resolve_token()（L0-L4 统一解析，tokens.json 从
credentials 目录读）→ fetch_kline / fetch_kline_range（OnchainOS CLI）
→ 当前 strategy.json 选中策略的引擎 run_all() → 服务端渲染表格。

可选股票数据源（?source=stock）：yfinance.download 拉真实美股 K 线，
用于 RWA 股票代币对应真实股票的长历史分析（方案 A：标的直接填美股
代码，不建映射表）。列名/时区统一为 OnchainOS 同形，TD 引擎零改动。

与 run_td_sequential 共用同一套参数（td_params.json 按策略独立保存）。
"""

from __future__ import annotations

import html as _html
import json
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf
from fastapi import Request
from fastapi.responses import HTMLResponse

from nanobot_quant.onchainos_cli import resolve_token, token_json_path
from nanobot_quant.onchainos_data import fetch_kline, fetch_kline_range
from nanobot_quant.strategies.registry import get_strategy, load_selected, resolve_engine_cls
from nanobot_quant.td_params import load_td_params

_TEMPLATE_PATH = Path(__file__).with_name("td_table_page.html")
_TZ = "Asia/Shanghai"
_BARS = ["1m", "5m", "15m", "1H", "4H", "1D", "1W"]
_DEFAULT_TICKER = "SOL"
_DEFAULT_LIMIT = 60
_DEFAULT_HISTORY_DAYS = 90

# EastMoney (primary stock source) klt codes + window spans.
_EM_KLTS = {"1m": "1", "5m": "5", "15m": "15", "1H": "60", "1D": "101", "1W": "102"}
# yfinance (fallback) interval map — no 4h in either source.
_YF_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1H": "60m", "1D": "1d", "1W": "1wk"}
_SPAN = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "1D": 86400, "1W": 604800}
_SOURCES = ("onchainos", "stock")


# ── 数据获取 ──────────────────────────────────────────────────────────


def _resolve_for_table(ticker: str) -> dict:
    """Resolve ticker → address/chain; returns error envelope on failure."""
    try:
        tokens = token_json_path()
        tokens_json = None
        if tokens.is_file():
            import json as _json
            tokens_json = _json.loads(tokens.read_text(encoding="utf-8"))
    except OSError:
        tokens_json = None
    resolved = resolve_token(ticker, tokens_json=tokens_json)
    # resolve_token echoes back the resolved chain (tokens.json entry wins,
    # default "solana"); keep the fallback for older callers.
    resolved.setdefault("chain", "solana")
    return resolved


def _fetch_stock_kline(
    ticker: str,
    bar: str = "1D",
    limit: int = 60,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Fetch real-stock candles, normalised to the OnchainOS DataFrame shape
    (lowercase ohlcv columns, naive DatetimeIndex).

    ``ticker`` is the US stock symbol itself (方案 A — no token→stock map).

    Primary source is EastMoney (push2his.eastmoney.com, no API key, works
    from datacenter IPs); Yahoo Finance (yfinance) is the fallback because
    Yahoo rate-limits datacenter IPs (429). 4H is unsupported for stocks.
    """
    errors: list[str] = []
    try:
        return _fetch_stock_kline_eastmoney(ticker, bar=bar, limit=limit, start=start, end=end)
    except Exception as exc:
        errors.append("东财: %s" % exc)
    try:
        return _fetch_stock_kline_yahoo(ticker, bar=bar, limit=limit, start=start, end=end)
    except Exception as exc:
        errors.append("yfinance: %s" % exc)
    raise RuntimeError("；".join(errors) or "股票数据获取失败")


def _stock_secid(ticker: str) -> str:
    """Map a symbol to an EastMoney secid.

    6-digit numeric codes are treated as A-shares (SSE ``1.`` / SZSE
    ``0.``); anything else is treated as a US symbol (``105.`` NYSE).
    5-prefix codes are SSE ETF (510/511/560/561/588 etc.); SZSE ETF use
    1/2/3-prefix (e.g. 159xxx) so they keep the ``0.`` branch.
    """
    if ticker.isdigit() and len(ticker) == 6:
        return f"1.{ticker}" if ticker.startswith(("6", "9", "5")) else f"0.{ticker}"
    return f"105.{ticker}"


def _yf_symbol(ticker: str) -> str:
    """Map a symbol to yfinance format: 6-digit codes get .SS/.SZ suffix."""
    if ticker.isdigit() and len(ticker) == 6:
        return f"{ticker}.SS" if ticker.startswith(("6", "9", "5")) else f"{ticker}.SZ"
    return ticker


def _fetch_stock_kline_eastmoney(
    ticker: str,
    bar: str = "1D",
    limit: int = 60,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """EastMoney kline API → normalised DataFrame.

    Response klines: "date,open,close,high,low,volume" (fields2
    f51..f56). US symbols use secid=105.<SYMBOL>; 6-digit codes are
    A-shares (secid 1./0.). 4H has no klt code.
    """
    klt = _EM_KLTS.get(bar)
    if klt is None:
        raise ValueError(f"股票数据源暂不支持 {bar} 周期（支持 1m/5m/15m/1H/1D/1W）")
    if start is None:
        now = end or datetime.now()
        span = _SPAN.get(bar, 86400) * max(limit, 10) * 2
        start = now - timedelta(seconds=span)
        end = now
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={_stock_secid(ticker)}&fields1=f1,f2,f3,f4,f5&"
        "fields2=f51,f52,f53,f54,f55,f56&"
        f"klt={klt}&fqt=1&beg={start.strftime('%Y%m%d')}&end={end.strftime('%Y%m%d')}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        payload = json.loads(r.read().decode("utf-8"))
    klines = (payload.get("data") or {}).get("klines") or []
    if not klines:
        raise RuntimeError(f"东财无数据: {ticker}")
    rows = []
    for line in klines:
        p = line.split(",")
        rows.append({"time": p[0], "open": float(p[1]), "close": float(p[2]),
                     "high": float(p[3]), "low": float(p[4]), "volume": float(p[5])})
    df = pd.DataFrame(rows).set_index("time")
    df.index = pd.to_datetime(df.index)
    # EastMoney timestamp semantics (verified 2026-08-05 against live API):
    #   A-share (secid 1./0.)      → Asia/Shanghai
    #   US daily   (klt=101)       → America/New_York (dates are US trading days)
    #   US intraday (klt=5/15/60)  → Asia/Shanghai (US 16:00 close = 04:00 Beijing)
    if ticker.isdigit() and len(ticker) == 6:
        em_tz = "Asia/Shanghai"
    else:
        # _EM_KLTS values are strings ("101", "60", ...)
        em_tz = "America/New_York" if klt == "101" else "Asia/Shanghai"
    df.index = df.index.tz_localize(em_tz)
    df.index.name = "time"
    df = df[["open", "high", "low", "close", "volume"]]
    if limit and len(df) > limit:
        df = df.tail(limit)
    return df


def _fetch_stock_kline_yahoo(
    ticker: str,
    bar: str = "1D",
    limit: int = 60,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """yfinance fallback — Yahoo rate-limits datacenter IPs (429)."""
    interval = _YF_INTERVALS.get(bar)
    if interval is None:
        raise ValueError(f"股票数据源暂不支持 {bar} 周期（支持 1m/5m/15m/1H/1D/1W）")
    if start is None:
        now = end or datetime.now()
        span = _SPAN.get(bar, 86400) * max(limit, 10) * 2
        start = now - timedelta(seconds=span)
        end = now
    # yfinance `end` is exclusive; extend by one day so the requested end
    # date is included. 2026-08-11 修复：start/end 以日期字符串（%Y-%m-%d）
    # 传给 yfinance（时分丢失），分钟周期（1m/5m/15m/1H）在 start/end 落
    # 同一天时区间为空（如 21:59 查 AAPL 5m 60 根 → start=end=今天 →
    # "yfinance 无数据"）。统一 +1 天，多余行由 tail(limit) 截断。
    end = end + timedelta(days=1)
    df = yf.download(
        _yf_symbol(ticker),
        start=start.strftime("%Y-%m-%d") if hasattr(start, "strftime") else start,
        end=end.strftime("%Y-%m-%d") if hasattr(end, "strftime") else end,
        interval=interval,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if df is None or df.empty:
        raise RuntimeError(f"yfinance 无数据: {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        # yfinance ≥1.x wraps columns as (Close, NVDA), (High, NVDA) …
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[cols].dropna(subset=["close"])
    # Keep the exchange tz (e.g. America/New_York, Asia/Shanghai for
    # .SS/.SZ) — the display layer tz_convert()s to local/UTC.
    df.index.name = "time"
    if limit and len(df) > limit:
        df = df.tail(limit)
    return df


def _engine_run(df: pd.DataFrame, strategy_name: str, params: dict) -> pd.DataFrame:
    """Normalise column names (lowercase → Title), run the engine, return
    the per-bar sequence DataFrame (same preprocessing as ``calculate``)."""
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    engine = resolve_engine_cls(strategy_name)(df, params)
    engine.run_all()
    return engine.df


def _display(df: pd.DataFrame, fallback_tz: str = "UTC") -> pd.DataFrame:
    """Add Asia/Shanghai + UTC display-time columns and pct-change.

    ``fallback_tz`` is used only when the index is naive (defensive; all
    real data sources now annotate their native tz).
    """
    idx = df.index
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize(fallback_tz)
    out = df.copy()
    out["_time"] = idx.tz_convert(_TZ).strftime("%Y-%m-%d %H:%M")
    out["_time_utc"] = idx.tz_convert("UTC").strftime("%Y-%m-%d %H:%M")
    out["_pct"] = df["Close"].pct_change() * 100
    return out


def _trade_signal_row(row, entry_setup: int, exit_setup: int, exit_cd: int) -> str:
    """镜像执行层（td_sequential_strategy）的信号判定：setup >= 入场/出场阈值。

    方案A（2026-08-12）：td-table 展示层与实盘行为一致——LONG 入场
    setup_buy >= entry_setup；LONG 出场 setup_sell >= exit_setup 或
    cd_sell >= exit_countdown（只做多，无做空）。
    """
    try:
        sb = int(row.get("buy_setup_count", 0) or 0)
        ss = int(row.get("sell_setup_count", 0) or 0)
        cds = int(row.get("sell_countdown_count", 0) or 0)
    except (TypeError, ValueError):
        return "HOLD"
    if sb >= entry_setup:
        return "BUY (Setup Complete)"
    if ss >= exit_setup or cds >= exit_cd:
        return "SELL (Setup Complete)"
    return "HOLD"


def _apply_trade_signal(disp, entry_setup: int, exit_setup: int, exit_cd: int):
    """覆盖 recommendation 列为执行层口径（td-table 展示与实盘一致）。"""
    if "recommendation" in disp.columns:
        disp["recommendation"] = disp.apply(
            lambda r: _trade_signal_row(r, entry_setup, exit_setup, exit_cd), axis=1)
    return disp


def _fmt_price(v) -> str:
    """Compact price formatting (2 dp for typical values, up to 6 for tiny)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if f == 0:
        return "0"
    return f"{f:.6g}"


# ── 信号回溯统计（Tab ② 核心增量） ───────────────────────────────────


def signal_stats(df: pd.DataFrame, setup: int) -> tuple[list[dict], dict]:
    """Collect every count==setup signal and its forward returns.

    Each signal: direction (BUY = falling setup, SELL = rising setup),
    trigger time/price, and close price + pct change 3/5/10 bars later
    (None when insufficient data at the range end). Returns (rows, agg)
    where agg[direction][n] = {"n", "win", "rate"} or None.

    Win definition: BUY signal wins if price is higher n bars later
    (reversal succeeded); SELL wins if price is lower.
    """
    closes = df["Close"].to_numpy()
    times = df.index
    rows: list[dict] = []
    for i in range(len(df)):
        for direction, cnt in (
            ("BUY", int(df["buy_setup_count"].iloc[i])),
            ("SELL", int(df["sell_setup_count"].iloc[i])),
        ):
            if cnt != setup:
                continue
            price = float(closes[i])
            t = times[i]
            if getattr(getattr(t, "tz", None), "utcoffset", None) is None:
                t = pd.Timestamp(t).tz_localize("UTC")
            rec = {
                "time": pd.Timestamp(t).tz_convert(_TZ).strftime("%Y-%m-%d %H:%M"),
                "direction": direction,
                "price": price,
            }
            for n in (3, 5, 10):
                j = i + n
                if j < len(df):
                    p = float(closes[j])
                    rec[f"p{n}"] = p
                    rec[f"pct{n}"] = (p / price - 1) * 100 if price else None
                else:
                    rec[f"p{n}"] = None
                    rec[f"pct{n}"] = None
            rows.append(rec)

    agg: dict = {"BUY": {}, "SELL": {}}
    for n in (3, 5, 10):
        for d in ("BUY", "SELL"):
            vals = [r[f"pct{n}"] for r in rows
                    if r["direction"] == d and r[f"pct{n}"] is not None]
            if not vals:
                agg[d][n] = None
                continue
            wins = sum(1 for v in vals if (v > 0 if d == "BUY" else v < 0))
            agg[d][n] = {"n": len(vals), "win": wins,
                         "rate": round(wins / len(vals) * 100, 1)}
    return rows, agg


# ── 渲染 ──────────────────────────────────────────────────────────────


def _esc(s) -> str:
    return _html.escape(str(s), quote=True)


def _setup_cell(df: pd.DataFrame, i: int, col: str, setup: int) -> str:
    v = int(df[col].iloc[i])
    if v == 0:
        return '<td class="muted"></td>'
    cls = "buy" if col == "buy_setup_count" else "sell"
    return f'<td class="setup {cls}">{v}</td>'


def _build_rows(df: pd.DataFrame, setup: int) -> str:
    """Render one <tr> per bar with per-variant columns (empty for variants
    that don't compute countdown/TDST)."""
    has_cd = "buy_countdown_count" in df.columns and df["buy_countdown_count"].abs().sum() > 0
    has_tdst = "tdst_support" in df.columns and df["tdst_support"].notna().any()
    has_score = "combined_score" in df.columns
    rows = []
    prev = None
    for i in range(len(df)):
        rec = df.iloc[i]
        row_cls = []
        if "BUY" in str(rec["recommendation"]):
            row_cls.append("sig-buy")
        elif "SELL" in str(rec["recommendation"]):
            row_cls.append("sig-sell")
        elif (int(rec["buy_setup_count"]) == 1 or int(rec["sell_setup_count"]) == 1):
            row_cls.append("flip")
        cls = f' class="{" ".join(row_cls)}"' if row_cls else ""
        pct = rec["_pct"]
        pct_cls = "up" if pct > 0 else ("dn" if pct < 0 else "")
        pct_txt = "" if pd.isna(pct) else f"{pct:+.2f}%"

        td = ["<tr%s>" % cls,
              f'<td class="time">{_esc(rec["_time"])}</td>',
              f'<td class="time utc">{_esc(rec["_time_utc"])}</td>',
              f'<td class="num">{_fmt_price(rec["Close"])}</td>',
              f'<td class="num {pct_cls}">{pct_txt}</td>',
              _setup_cell(df, i, "buy_setup_count", setup),
              _setup_cell(df, i, "sell_setup_count", setup)]
        if has_cd:
            cd = int(rec["buy_countdown_count"])
            td.append(f'<td class="num">{cd if cd else ""}</td>')
        if has_tdst:
            sup, res = rec["tdst_support"], rec["tdst_resistance"]
            td.append(f'<td class="num">{_fmt_price(sup) if pd.notna(sup) else ""}</td>')
            td.append(f'<td class="num">{_fmt_price(res) if pd.notna(res) else ""}</td>')
        if has_score:
            sc = rec["combined_score"]
            td.append(f'<td class="num">{f"{sc:.2f}" if pd.notna(sc) else ""}</td>')

        sig = str(rec["recommendation"])
        if "BUY" in sig:
            sig_html = f'<td class="sig buy">{_esc(sig)}</td>'
        elif "SELL" in sig:
            sig_html = f'<td class="sig sell">{_esc(sig)}</td>'
        else:
            sig_html = '<td class="sig hold">—</td>'
        td.append(sig_html + "</tr>")
        rows.append("".join(td))
        prev = i
    return "\n".join(rows)


def _build_headers(has_cd: bool, has_tdst: bool, has_score: bool) -> str:
    heads = ["时间", "UTC 时间", "收盘", "涨跌%", "Buy Setup", "Sell Setup"]
    if has_cd:
        heads.append("Countdown")
    if has_tdst:
        heads += ["TDST 支撑", "TDST 阻力"]
    if has_score:
        heads.append("Score")
    heads.append("信号")
    return "".join(f"<th>{h}</th>" for h in heads)


def _render_status(df: pd.DataFrame, setup: int, strategy_label: str,
                   entry_setup: int = 9, exit_setup: int = 9, exit_cd: int = 13) -> str:
    last = df.iloc[-1]
    b, s = int(last["buy_setup_count"]), int(last["sell_setup_count"])
    price = _fmt_price(last["Close"])
    sig = str(last["recommendation"])
    parts = [
        f"<b>{_esc(strategy_label)}</b>",
        f"最新收盘 <b>{price}</b>",
        f"Buy Setup <b class=\"setup buy\">{b}/{setup}</b>（≥{entry_setup} 触发）",
        f"Sell Setup <b class=\"setup sell\">{s}/{setup}</b>（≥{exit_setup} 或 CD≥{exit_cd} 触发）",
        f"当前信号 <b>{_esc(sig)}</b>" if sig != "HOLD" else "当前信号 <b>—</b>",
    ]
    return '<div class="status">' + " ｜ ".join(parts) + "</div>"


def _render_stats_table(rows: list[dict], agg: dict) -> str:
    """9 信号回溯统计：信号列表 + 方向胜率卡。"""
    out = ['<div class="stats-grid">']
    for d in ("BUY", "SELL"):
        cells = []
        for n in (3, 5, 10):
            a = agg[d][n]
            if a is None:
                cells.append(f"<td>—</td>")
            else:
                cells.append(f"<td>{a['rate']}%<br><span class=\"muted\">{a['win']}/{a['n']}</span></td>")
        label = "BUY（下跌 9 后反弹胜率）" if d == "BUY" else "SELL（上涨 9 后回落胜率）"
        out.append(
            f'<div class="stat-card"><h4>{label}</h4>'
            f'<table><tr><th></th><th>3 根后</th><th>5 根后</th><th>10 根后</th></tr>'
            f'<tr><th>胜率</th>{"".join(cells)}</tr></table></div>'
        )
    out.append("</div>")

    if rows:
        rows_html = []
        for r in rows:
            cls = "buy" if r["direction"] == "BUY" else "sell"
            t = str(r["time"])[:16].replace("T", " ")
            pct3 = "" if r["pct3"] is None else f"{r['pct3']:+.2f}%"
            pct5 = "" if r["pct5"] is None else f"{r['pct5']:+.2f}%"
            pct10 = "" if r["pct10"] is None else f"{r['pct10']:+.2f}%"
            rows_html.append(
                f'<tr><td class="time">{_esc(t)}</td>'
                f'<td class="sig {cls}">{r["direction"]}</td>'
                f'<td class="num">{_fmt_price(r["price"])}</td>'
                f'<td class="num">{pct3}</td><td class="num">{pct5}</td><td class="num">{pct10}</td></tr>'
            )
        out.insert(0,
            '<div class="stat-card" style="grid-column:1/-1"><h4>9 信号回溯（区间内每个 count == Setup 周期）</h4>'
            '<table><tr><th>触发时间</th><th>方向</th><th>触发价</th><th>3 根后</th><th>5 根后</th><th>10 根后</th></tr>'
            + "\n".join(rows_html) + "</table></div>")
    return "\n".join(out)


# ── 页面 ──────────────────────────────────────────────────────────────


def _load_template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def _query_int(q: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(q.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _form(tab: str, ticker: str, bar: str, limit: int, start: str, end: str, source: str = "onchainos") -> str:
    bar_opts = "".join(
        f'<option value="{b}"{" selected" if b == bar else ""}>{b}</option>'
        for b in _BARS
    )
    source_opts = (
        '<label>数据源</label><select name="source">'
        '<option value="onchainos"%s>链上 DEX (OnchainOS)</option>'
        '<option value="stock"%s>股票 (东财/yfinance)</option></select>'
        % (" selected" if source == "onchainos" else "", " selected" if source == "stock" else "")
    )
    placeholder = "NVDA / 601127" if source == "stock" else "SOL / BTC"
    if tab == "live":
        return ""  # 实时监控无表单，自动轮询
    if tab == "history":
        return (
            '<form class="inline" method="get" action="/config/td-table">'
            '<input type="hidden" name="tab" value="history">'
            '%s'
            '<label>标的</label><input name="ticker" value="%s" size="8" placeholder="%s">'
            '<label>周期</label><select name="bar">%s</select>'
            '<label>起始</label><input type="date" name="start" value="%s">'
            '<label>结束</label><input type="date" name="end" value="%s">'
            '<button>分析</button></form>'
            % (source_opts, _esc(ticker), placeholder, bar_opts, _esc(start), _esc(end))
        )
    return (
        '<form class="inline" method="get" action="/config/td-table">'
        '<input type="hidden" name="tab" value="snapshot">'
        '%s'
        '<label>标的</label><input name="ticker" value="%s" size="8" placeholder="%s">'
        '<label>周期</label><select name="bar">%s</select>'
        '<label>K 线数</label><input type="number" name="limit" value="%d" min="20" max="300" style="width:70px">'
        '<button>刷新</button></form>'
        % (source_opts, _esc(ticker), placeholder, bar_opts, limit)
    )


def td_table_page(request: Request) -> HTMLResponse:
    """GET /config/td-table — render the double-tab page.

    Query params: tab=snapshot|history, ticker, bar, limit | start/end, source.
    """
    q = dict(request.query_params)
    tab = q.get("tab", "snapshot")
    ticker = (q.get("ticker") or _DEFAULT_TICKER).strip().upper()
    bar = q.get("bar") or "1D"
    if bar not in _BARS:
        bar = "1D"
    limit = _query_int(q, "limit", _DEFAULT_LIMIT, 20, 300)
    source = q.get("source") or "onchainos"
    if source not in _SOURCES:
        source = "onchainos"
    today = datetime.now()
    end = (q.get("end") or today.strftime("%Y-%m-%d")).strip()
    start = (q.get("start") or (today - timedelta(days=_DEFAULT_HISTORY_DAYS)).strftime("%Y-%m-%d")).strip()

    strategy_name = load_selected()
    strategy_label = get_strategy(strategy_name).label
    params = load_td_params(strategy_name)
    setup = int(params.get("setup_period", 9))
    entry_setup = int(params.get("entry_setup", 9))
    exit_setup = int(params.get("exit_setup", 9))
    exit_cd = int(params.get("exit_countdown", 13))

    banner = ""
    content = ""
    banner = ""
    content = ""
    frag = q.get("frag") == "1"
    if tab == "history":
        content = _render_history(ticker, bar, start, end, strategy_name, params, setup, entry_setup, exit_setup, exit_cd, source)
    elif tab == "live":
        content = _render_live(with_script=not frag, tq=q, entry_setup=entry_setup, exit_setup=exit_setup, exit_cd=exit_cd)
    else:
        content = _render_snapshot(ticker, bar, limit, strategy_name, params, setup, entry_setup, exit_setup, exit_cd, source)

    if isinstance(content, tuple):  # (error_html,)
        banner = content[0]
        content = ""

    if frag:
        # 「实时监控」tab 轮询：只返回内容片段（含脚本会重复注册 setInterval）
        return HTMLResponse(content)

    page = _load_template()
    page = page.replace("{strategy_label}", _esc(strategy_label))
    page = page.replace("{snap_active}", "active" if tab not in ("history", "live") else "")
    page = page.replace("{hist_active}", "active" if tab == "history" else "")
    page = page.replace("{live_active}", "active" if tab == "live" else "")
    page = page.replace("{refresh_s}", str(_live_refresh_s()))
    page = page.replace("{ticker}", _esc(ticker))
    page = page.replace("{bar}", _esc(bar))
    page = page.replace("{limit}", str(limit))
    page = page.replace("{start}", _esc(start))
    page = page.replace("{end}", _esc(end))
    page = page.replace("{form}", _form(tab, ticker, bar, limit, start, end, source))
    page = page.replace("{banner}", banner)
    page = page.replace("{content}", content)
    return HTMLResponse(page)


def _live_refresh_s() -> int:
    """「实时监控」自动刷新间隔（exec_params.td_ui_refresh_s，默认 10s）。"""
    try:
        from nanobot_quant.exec_params import load_exec_params
        return int(load_exec_params().get("td_ui_refresh_s", 10) or 10)
    except Exception:  # noqa: BLE001
        return 10


def _live_cell(v: int, threshold: int) -> str:
    """setup/cd 进度单元格：临近阈值橙色、达到/超过绿色高亮。"""
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        v = 0
    if v >= threshold:
        return f'<b style="color:#1b7f3d">{v}</b>'
    if v >= max(threshold - 2, 1):
        return f'<b style="color:#e8890c">{v}</b>'
    return str(v)


# 交易记录事件集合：事件 → (方向, 状态)
_TRADE_EVENTS = {
    "LONG": ("buy", "ok"), "LONG_PENDING": ("buy", "pending"),
    "BUY_FAIL": ("buy", "fail"),
    "EXIT": ("sell", "ok"), "EXIT_PENDING": ("sell", "pending"),
    "EXIT_FAIL": ("sell", "fail"),
    "EXIT_SKIP": ("sell", "skip"), "EXIT_SHRINK": ("sell", "shrink"),
}

_STATUS_BADGE = {
    "ok": "✅", "pending": "⏳", "fail": "❌", "skip": "⏭", "shrink": "↩️",
}

# 链 → 交易浏览器 URL 模板（tx_hash 点击跳转）
_TX_EXPLORERS = {
    "solana": "https://solscan.io/tx/{tx}",
    "sol": "https://solscan.io/tx/{tx}",
    "501": "https://solscan.io/tx/{tx}",
    "eth": "https://etherscan.io/tx/{tx}",
    "op_eth": "https://optimistic.etherscan.io/tx/{tx}",
    "bnb": "https://bscscan.com/tx/{tx}",
    "56": "https://bscscan.com/tx/{tx}",
    "xlayer": "https://explorer.xlayer.tech/tx/{tx}",
    "196": "https://explorer.xlayer.tech/tx/{tx}",
    "arb": "https://arbiscan.io/tx/{tx}",
    "base": "https://basescan.org/tx/{tx}",
    "polygon": "https://polygonscan.com/tx/{tx}",
    "trx": "https://tronscan.org/#/transaction/{tx}",
}


def _tx_cell(tx, chain: str = "") -> str:
    """tx_hash 单元格：真实链上 hash → 可点击浏览器链接，占位符/空 → 纯文本。

    2026-08-11：占位 UUID（32 位 hex，上链前由 relayer 返回）不是真实
    hash，点击会打开无效链接，故只对非 hex 长串（base58）生成链接。
    """
    tx = str(tx or "")
    if not tx:
        return '<span class="muted">—</span>'
    if len(tx) <= 32 and all(c in "0123456789abcdefABCDEF" for c in tx):
        return f'<span title="{_esc(tx)}">{_esc(tx[:8])}</span>'
    chain = (chain or "").lower()
    url = _TX_EXPLORERS.get(chain)
    if url is None:
        for k, v in _TX_EXPLORERS.items():
            if k and (k in chain or chain in k):
                url = v
                break
    if url is None:
        url = _TX_EXPLORERS["solana"]
    return (
        f'<a href="{url.format(tx=tx)}" target="_blank" rel="noopener" '
        f'title="{_esc(tx)}">{_esc(tx[:8])} ↗</a>'
    )


def _trade_rows(events: list[dict], tq: dict | None = None) -> list[dict]:
    """从事件流过滤交易记录（最新在前），支持查询条件。

    2026-08-11 方案 B：成交事件（LONG/EXIT/各 PENDING/FAIL/SKIP）独立
    成表，携带 slot/qty/tx_hash 细节；查询条件 tq_sym/tq_dir/tq_st。
    """
    tq = tq or {}
    sym = (tq.get("tq_sym") or "").strip().upper()
    qdir = (tq.get("tq_dir") or "").strip()
    qst = (tq.get("tq_st") or "").strip()
    rows: list[dict] = []
    for e in events:
        ev = str(e.get("event", ""))
        meta = _TRADE_EVENTS.get(ev)
        if meta is None:
            continue
        direction, status = meta
        if qdir and direction != qdir:
            continue
        if qst and status != qst:
            continue
        if sym and str(e.get("symbol", "")).upper() != sym:
            continue
        rows.append({**e, "direction": direction, "status": status})
    rows.reverse()  # 最新在前
    return rows


def _fmt_qty(q) -> str:
    """交易数量格式化：无/零显示 —，否则 6 位有效数字。"""
    try:
        v = float(q or 0)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "—"
    return f"{v:.6g}"


def _render_live(with_script: bool = True, tq: dict | None = None,
                 entry_setup=None, exit_setup=None, exit_cd=None) -> str:
    """「实时监控」tab：TD live 每轮状态 + 最近信号事件。

    2026-08-11 方案 A：内存共享——TD live 循环（gatekeeper 进程内
    StrategyExecutor）每轮写 LIVE_STATE，本 handler 同进程直接读取；
    信号事件从事件文件（append-only JSONL）读最近 20 条。页面 JS 按
    exec_params.td_ui_refresh_s 轮询 `?tab=live&frag=1` 刷新内容片段。
    2026-08-12 方案A：setup 进度高亮阈值改读 entry_setup/exit_setup/
    exit_countdown（与执行层一致）。
    """
    if entry_setup is None:
        p = load_td_params(load_selected())
        entry_setup = int(p.get("entry_setup", 9))
        exit_setup = int(p.get("exit_setup", 9))
        exit_cd = int(p.get("exit_countdown", 13))
    try:
        from nanobot_quant import td_live_state
        st = td_live_state.get_state()
        events = td_live_state.load_events(500)  # 交易记录查询需要更多历史
    except Exception:  # noqa: BLE001
        st = {"running": False, "symbols": {}, "updated_at": None, "next_iteration": None}
        events = []

    run_txt = "🟢 运行中" if st.get("running") else "⏹ 已停止"
    upd = _esc(str(st.get("updated_at") or "—"))
    nxt = _esc(str(st.get("next_iteration") or "—"))

    rows = ""
    for sym, d in sorted(st.get("symbols", {}).items()):
        sb = d.get("setup_buy", 0)
        ss = d.get("setup_sell", 0)
        cdb = d.get("cd_buy", 0)
        cds = d.get("cd_sell", 0)
        score = d.get("score", 0)
        price = d.get("price", 0)
        signal = _esc(str(d.get("signal", "HOLD")))
        note = _esc(str(d.get("note", "")))
        if note:
            note = f'<span class="muted"> · {note}</span>'
        sig_cls = {
            "LONG": "sig buy", "EXIT": "sig sell", "BUY_FAIL": "sig sell",
            "EXIT_FAIL": "sig sell", "SKIP": "sig hold",
            "EXIT_SKIP": "sig hold", "EXIT_SHRINK": "sig sell",
        }.get(signal, "sig hold")
        rows += (
            f'<tr><td><b>{_esc(sym)}</b></td>'
            f'<td>{_live_cell(sb, entry_setup)}</td>'
            f'<td>{_live_cell(ss, exit_setup)}</td>'
            f'<td>{_live_cell(cdb, 13)}</td>'
            f'<td>{_live_cell(cds, exit_cd)}</td>'
            f'<td class="num">{float(score or 0):.1f}</td>'
            f'<td class="num">{float(price or 0):.4f}</td>'
            f'<td class="sig {sig_cls}">{signal}</td>'
            f'<td class="time">{_esc(str(d.get("time", "")))}</td>'
            f'<td>{note}</td></tr>'
        )
    if not rows:
        rows = '<tr><td colspan="10" class="muted" style="text-align:left">暂无数据——TD live 循环未运行或尚未产生第一轮结果</td></tr>'

    # ── 📜 信号历史（最近 N 条，最新在上；支持 sq_n/sq_sym/sq_ev 过滤）──
    try:
        sq_n = min(max(int((tq or {}).get("sq_n") or 20), 1), 200)
    except (TypeError, ValueError):
        sq_n = 20
    sq_sym = (tq.get("sq_sym") or "").strip().upper()
    sq_ev = (tq.get("sq_ev") or "").strip()
    events_sig = [e for e in events
                  if (not sq_sym or str(e.get("symbol", "")).upper() == sq_sym)
                  and (not sq_ev or str(e.get("event", "")) == sq_ev)]
    events_sig = events_sig[::-1][:sq_n]
    ev_rows = ""
    for e in events_sig:
        ev_cls = ""
        if e.get("event") in ("LONG",):
            ev_cls = ' style="color:#1b7f3d"'
        elif e.get("event") in ("EXIT",):
            ev_cls = ' style="color:#c62828"'
        elif e.get("event") in ("BUY_FAIL", "EXIT_FAIL"):
            ev_cls = ' style="color:#b3261e"'
        ev_rows += (
            f'<tr><td class="time">{_esc(str(e.get("ts", "")))}</td>'
            f'<td><b>{_esc(str(e.get("symbol", "")))}</b></td>'
            f'<td{ev_cls}><b>{_esc(str(e.get("event", "")))}</b></td>'
            f'<td class="num">{float(e.get("price", 0) or 0):.4f}</td>'
            f'<td class="num">{float(e.get("score", 0) or 0):.1f}</td>'
            f'<td>{_esc(str(e.get("note", "")))}</td></tr>'
        )
    if not ev_rows:
        ev_rows = '<tr><td colspan="6" class="muted" style="text-align:left">暂无信号事件</td></tr>'

    # ── 📊 交易记录（2026-08-11 方案 B）──
    trade_rows = _trade_rows(events, tq)
    try:
        tr_n = min(max(int((tq or {}).get("tq_n") or 20), 1), 100)
    except (TypeError, ValueError):
        tr_n = 20
    trade_rows = trade_rows[:tr_n]
    st_color = {"ok": "#1b7f3d", "pending": "#e8890c", "fail": "#b3261e",
                "skip": "#666", "shrink": "#1565c0"}
    tr_rows = ""
    for e in trade_rows:
        badge = _STATUS_BADGE.get(e["status"], "·")
        color = st_color.get(e["status"], "#333")
        dir_txt = "🟢 买" if e["direction"] == "buy" else "🔴 卖"
        tx_cell = _tx_cell(e.get("tx_hash"), str(e.get("chain") or ""))
        slot = e.get("slot")
        slot_txt = _esc(str(slot)) if slot not in (None, "") else "—"
        tr_rows += (
            f'<tr><td>{tx_cell}</td>'
            f'<td class="time">{_esc(str(e.get("ts", "")))}</td>'
            f'<td><b>{_esc(str(e.get("symbol", "")))}</b></td>'
            f'<td>{dir_txt}</td>'
            f'<td class="num">{_fmt_qty(e.get("qty"))}</td>'
            f'<td class="num">{float(e.get("price", 0) or 0):.2f}</td>'
            f'<td class="num">{_actual_price_cell(e.get("actual_price"))}</td>'
            f'<td class="num">{_slip_cell(e.get("actual_price"), e.get("price"))}</td>'
            f'<td class="num">{slot_txt}</td>'
            f'<td style="color:{color}"><b>{badge} {_esc(str(e.get("event", "")))}</b></td>'
            f'<td class="note">{_esc(str(e.get("note", "")))}</td></tr>'
        )
    if not tr_rows:
        tr_rows = ('<tr><td colspan="11" class="muted" style="text-align:left">'
                   '暂无交易记录（买卖信号出现后显示）</td></tr>')

    tq = tq or {}
    tq_sel = lambda name, opts, cur: '<select name="%s">%s</select>' % (name, "".join(
        f'<option value="{v}"{" selected" if v == cur else ""}>{lab}</option>'
        for v, lab in opts))
    tq_dir_opts = [("", "全部方向"), ("buy", "买"), ("sell", "卖")]
    tq_st_opts = [("", "全部状态"), ("ok", "✅ 成功"), ("pending", "⏳ 待确认"),
                  ("fail", "❌ 失败"), ("skip", "⏭ 跳过"), ("shrink", "↩️ 缩量")]
    tq_n_opts = [("20", "20 条"), ("50", "50 条"), ("100", "100 条")]
    trade_form = (
        '<form class="inline" id="trade-form" onsubmit="return applyTQ()">'
        '<label>标的</label><input name="tq_sym" value="%s" size="6" placeholder="全部">'
        '<label>方向</label>%s<label>状态</label>%s<label>条数</label>%s'
        '<button>查询</button></form>'
        % (_esc(tq.get("tq_sym", "") or ""),
           tq_sel("tq_dir", tq_dir_opts, tq.get("tq_dir", "") or ""),
           tq_sel("tq_st", tq_st_opts, tq.get("tq_st", "") or ""),
           tq_sel("tq_n", tq_n_opts, tq.get("tq_n", "") or "20"))
    )

    sq_ev_opts = [("", "全部"), ("LONG", "买"), ("LONG_PENDING", "买待确认"),
                  ("EXIT", "卖"), ("EXIT_PENDING", "卖待确认"),
                  ("EXIT_FAIL", "卖失败"), ("EXIT_SKIP", "卖跳过"), ("EXIT_SHRINK", "缩量"),
                  ("BUY_FAIL", "买失败"), ("SKIP", "跳过")]
    sq_sel = lambda name, opts, cur: '<select name="%s">%s</select>' % (name, "".join(
        f'<option value="{v}"{" selected" if v == cur else ""}>{lab}</option>'
        for v, lab in opts))
    sq_n_opts = [("20", "20 条"), ("50", "50 条"), ("100", "100 条")]
    sig_form = (
        '<form class="inline" id="sig-form" onsubmit="return applySQ()">'
        '<label>标的</label><input name="sq_sym" value="%s" size="6" placeholder="全部">'
        '<label>事件</label>%s<label>条数</label>%s'
        '<button>查询</button></form>'
        % (_esc(sq_sym), sq_sel("sq_ev", sq_ev_opts, sq_ev),
           sq_sel("sq_n", sq_n_opts, str(sq_n)))
    )

    html = (
        '<div class="status">'
        f'<span>循环：<b>{run_txt}</b></span>'
        f'<span>下一轮：{nxt}</span>'
        f'<span>更新时间：{upd}</span>'
        f'<span class="muted">自动刷新 {_live_refresh_s()}s · 颜色：橙=临近信号 · 绿=达到/超过阈值</span>'
        '</div>'
        '<div id="live-wrap">'
        '<table>'
        '<tr><th>标的</th><th>Buy Setup</th><th>Sell Setup</th><th>CD Buy</th>'
        '<th>CD Sell</th><th>Score</th><th>价格</th><th>信号</th><th>最后 bar</th><th>备注</th></tr>'
        f'{rows}'
        '</table>'
        f'<h4 style="margin:18px 0 8px">📊 交易记录（最近 {tr_n} 条）</h4>'
        f'{trade_form}'
        '<table>'
        '<tr><th>tx_hash</th><th>时间</th><th>标的</th><th>方向</th><th>数量</th><th>策略价</th><th>成交价</th><th>滑点</th><th>slot</th><th>状态</th><th>原因</th></tr>'
        f'{tr_rows}'
        '</table>'
        f'<h4 style="margin:18px 0 8px">📜 信号历史（最近 {sq_n} 条）</h4>'
        f'{sig_form}'
        '<table>'
        '<tr><th>时间</th><th>标的</th><th>事件</th><th>价格</th><th>Score</th><th>备注</th></tr>'
        f'{ev_rows}'
        '</table>'
        '</div>'
    )
    if with_script:
        html += (
            '<script>'
            '(function(){'
            '  var secs = ' + str(_live_refresh_s()) + ';'
            '  window.TQ = window.TQ || (function(){'
            r'    var q = location.search.replace(/^\?/, "").split("&");'
            '    var o = {};'
            '    for (var i = 0; i < q.length; i++){'
            '      var p = q[i].split("=");'
            '      if (p[0].indexOf("tq_") === 0 || p[0].indexOf("sq_") === 0) o[p[0]] = decodeURIComponent(p[1] || "");'
            '    }'
            '    return o;'
            '  })();'
            '  function tqQS(){'
            '    var p = [];'
            '    for (var k in window.TQ) if (window.TQ[k]) p.push(k + "=" + encodeURIComponent(window.TQ[k]));'
            '    return p.length ? "&" + p.join("&") : "";'
            '  }'
            '  function poll(){'
            "    fetch('/config/td-table?tab=live&frag=1' + tqQS())"
            '      .then(function(r){ return r.text(); })'
            '      .then(function(html){'
            "        var el = document.getElementById('live-wrap');"
            "        if (el && html) el.innerHTML = html;"
            '      }).catch(function(){});'
            '  }'
            '  window.applyTQ = function(){'
            '    var f = document.getElementById("trade-form");'
            '    if (f){'
            '      ["tq_sym", "tq_dir", "tq_st", "tq_n"].forEach(function(k){'
            '        if (f.elements[k] && f.elements[k].value) window.TQ[k] = f.elements[k].value;'
            '        else if (f.elements[k]) delete window.TQ[k];'
            '      });'
            '    }'
            '    var p = [];'
            '    for (var k in window.TQ) if (window.TQ[k]) p.push(k + "=" + encodeURIComponent(window.TQ[k]));'
            '    var q = p.length ? "?" + p.join("&") : "";'
            '    try { history.replaceState(null, "", "/config/td-table?tab=live" + q); } catch(e){}'
            '    poll();'
            '    return false;'
            '  };'
            '  window.applySQ = function(){'
            '    var f = document.getElementById("sig-form");'
            '    if (f){'
            '      ["sq_sym", "sq_ev", "sq_n"].forEach(function(k){'
            '        if (f.elements[k] && f.elements[k].value) window.TQ[k] = f.elements[k].value;'
            '        else if (f.elements[k]) delete window.TQ[k];'
            '      });'
            '    }'
            '    var p = [];'
            '    for (var k in window.TQ) if (window.TQ[k]) p.push(k + "=" + encodeURIComponent(window.TQ[k]));'
            '    var q = p.length ? "?" + p.join("&") : "";'
            '    try { history.replaceState(null, "", "/config/td-table?tab=live" + q); } catch(e){}'
            '    poll();'
            '    return false;'
            '  };'
            '  setInterval(poll, secs * 1000);'
            '  poll();'
            '})();'
            '</script>'
        )
    return html


def _render_snapshot(ticker, bar, limit, strategy_name, params, setup,
                     entry_setup=None, exit_setup=None, exit_cd=None,
                     source="onchainos"):
    if source == "stock":
        try:
            df = _fetch_stock_kline(ticker, bar=bar, limit=limit)
        except Exception as exc:
            return ('<div class="banner err">股票数据获取失败：%s</div>' % _esc(exc)), None
        if df.empty:
            return ('<div class="banner err">%s 无 %s 股票 K 线数据（yfinance）。</div>'
                    % (_esc(ticker), _esc(bar))), None
        src_label = "股票（%s）" % _esc(ticker)
    else:
        resolved = _resolve_for_table(ticker)
        if not resolved.get("ok"):
            return ('<div class="banner err">标的解析失败：%s（%s）——可在「📊 业务管理」→ 代币管理添加或确认。</div>'
                    % (_esc(resolved.get("issue") or "unknown"), _esc(resolved.get("category") or ""))), None
        try:
            df = fetch_kline(resolved["chain"], resolved["address"], bar=bar, limit=limit)
        except Exception as exc:  # CLI failure (e.g. missing credentials)
            return ('<div class="banner err">K 线获取失败：%s</div>' % _esc(exc)), None
        if df.empty:
            return ('<div class="banner err">%s 无 %s K 线数据（可能链上无交易或区间过短）。</div>'
                    % (_esc(ticker), _esc(bar))), None
        src_label = "OnchainOS（%s/%s）" % (_esc(resolved["chain"]), _esc(resolved["address"]))

    seq = _engine_run(df, strategy_name, params)
    disp = _display(seq)
    if entry_setup is None:
        entry_setup = int(params.get("entry_setup", 9))
        exit_setup = int(params.get("exit_setup", 9))
        exit_cd = int(params.get("exit_countdown", 13))
    disp = _apply_trade_signal(disp, entry_setup, exit_setup, exit_cd)
    has_cd = "buy_countdown_count" in disp.columns and disp["buy_countdown_count"].abs().sum() > 0
    has_tdst = "tdst_support" in disp.columns and disp["tdst_support"].notna().any()
    has_score = "combined_score" in disp.columns

    status = _render_status(disp, setup, strategy_name, entry_setup, exit_setup, exit_cd)
    table = ('<table><thead><tr>%s</tr></thead><tbody>\n%s\n</tbody></table>'
             % (_build_headers(has_cd, has_tdst, has_score),
                _build_rows(disp, setup)))
    hint = ('<div class="banner info">最近 %d 根 %s · 数据来源 %s。'
            '当前策略参数来自 td_params.json（%s 独立保存）。</div>'
            % (len(disp), _esc(bar), src_label, _esc(strategy_name)))
    return status + hint + table


def _render_history(ticker, bar, start, end, strategy_name, params, setup,
                    entry_setup=None, exit_setup=None, exit_cd=None,
                    source="onchainos"):
    try:
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
    except ValueError:
        return '<div class="banner err">日期格式错误（应为 YYYY-MM-DD）。</div>', None
    if source == "stock":
        try:
            df = _fetch_stock_kline(ticker, bar=bar, start=start_dt, end=end_dt)
        except Exception as exc:
            return ('<div class="banner err">股票数据获取失败：%s</div>' % _esc(exc)), None
        if df.empty:
            return ('<div class="banner err">%s 在 %s ~ %s 无 %s 股票 K 线数据（yfinance）。</div>'
                    % (_esc(ticker), _esc(start), _esc(end), _esc(bar))), None
        src_label = "股票（%s）" % _esc(ticker)
    else:
        resolved = _resolve_for_table(ticker)
        if not resolved.get("ok"):
            return ('<div class="banner err">标的解析失败：%s（%s）</div>'
                    % (_esc(resolved.get("issue") or "unknown"), _esc(resolved.get("category") or ""))), None
        try:
            df = fetch_kline_range(resolved["chain"], resolved["address"],
                                   start=start_dt, end=end_dt, bar=bar)
        except Exception as exc:
            return ('<div class="banner err">K 线获取失败：%s</div>' % _esc(exc)), None
        if df.empty:
            return ('<div class="banner err">%s 在 %s ~ %s 无 %s K 线数据。</div>'
                    % (_esc(ticker), _esc(start), _esc(end), _esc(bar))), None
        src_label = "OnchainOS（%s/%s）" % (_esc(resolved["chain"]), _esc(resolved["address"]))

    seq = _engine_run(df, strategy_name, params)
    disp = _display(seq)
    if entry_setup is None:
        entry_setup = int(params.get("entry_setup", 9))
        exit_setup = int(params.get("exit_setup", 9))
        exit_cd = int(params.get("exit_countdown", 13))
    disp = _apply_trade_signal(disp, entry_setup, exit_setup, exit_cd)
    has_cd = "buy_countdown_count" in disp.columns and disp["buy_countdown_count"].abs().sum() > 0
    has_tdst = "tdst_support" in disp.columns and disp["tdst_support"].notna().any()
    has_score = "combined_score" in disp.columns

    rows, agg = signal_stats(seq, setup)
    table = ('<table><thead><tr>%s</tr></thead><tbody>\n%s\n</tbody></table>'
             % (_build_headers(has_cd, has_tdst, has_score), _build_rows(disp, setup)))
    stats = _render_stats_table(rows, agg)
    hint = ('<div class="banner info">%s ~ %s 共 %d 根 %s K 线 · 数据来源 %s · %d 信号 %d 个 · 胜率统计仅含区间内可观察完整后续的信号。</div>'
            % (_esc(start), _esc(end), len(disp), _esc(bar), src_label, setup, len(rows)))
    return hint + table + stats


# ── 路由注册（legion gatekeeper 调用） ───────────────────────────────


def register_td_table_routes(app, gatekeeper) -> None:
    """Mount /config/td-table routes (called from nanobot-legion)."""
    app.get("/config/td-table")(td_table_page)
def _actual_price_cell(ap) -> str:
    """实际成交价单元格：无/无效 → —。"""
    try:
        f = float(ap)
    except (TypeError, ValueError):
        return "—"
    if f <= 0:
        return "—"
    return _fmt_price(f)


def _slip_cell(ap, strat_p) -> str:
    """滑点单元格：(实际价 − 策略价) / 策略价 × 100，带符号；口径含市场波动。"""
    try:
        ap_f = float(ap)
        sp_f = float(strat_p or 0)
    except (TypeError, ValueError):
        return "—"
    if ap_f <= 0 or sp_f <= 0:
        return "—"
    return f"{(ap_f - sp_f) / sp_f * 100:+.2f}%"
