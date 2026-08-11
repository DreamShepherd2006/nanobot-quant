"""TD Sequential 可视化分析页（/config/td-table）— 实时快照 + 历史区间分析。

定位：辅助分析工具（看趋势演变、验证 9 信号质量），不构成买卖交易、
不模拟成交。双 tab：

- Tab ① 实时快照：最近 N 根 K 线的 setup/countdown/TDST/score 轨迹，
  高亮信号行（count == setup_period）与 setup 启动行（count == 1）。
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
    """
    if ticker.isdigit() and len(ticker) == 6:
        return f"1.{ticker}" if ticker.startswith(("6", "9")) else f"0.{ticker}"
    return f"105.{ticker}"


def _yf_symbol(ticker: str) -> str:
    """Map a symbol to yfinance format: 6-digit codes get .SS/.SZ suffix."""
    if ticker.isdigit() and len(ticker) == 6:
        return f"{ticker}.SS" if ticker.startswith(("6", "9")) else f"{ticker}.SZ"
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
    # date is included.
    end = end + timedelta(days=1) if bar in ("1D", "1W") else end
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


def _render_status(df: pd.DataFrame, setup: int, strategy_label: str) -> str:
    last = df.iloc[-1]
    b, s = int(last["buy_setup_count"]), int(last["sell_setup_count"])
    price = _fmt_price(last["Close"])
    sig = str(last["recommendation"])
    parts = [
        f"<b>{_esc(strategy_label)}</b>",
        f"最新收盘 <b>{price}</b>",
        f"Buy Setup <b class=\"setup buy\">{b}/{setup}</b>",
        f"Sell Setup <b class=\"setup sell\">{s}/{setup}</b>",
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

    banner = ""
    content = ""
    banner = ""
    content = ""
    frag = q.get("frag") == "1"
    if tab == "history":
        content = _render_history(ticker, bar, start, end, strategy_name, params, setup, source)
    elif tab == "live":
        content = _render_live(with_script=not frag)
    else:
        content = _render_snapshot(ticker, bar, limit, strategy_name, params, setup, source)

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


def _render_live(with_script: bool = True) -> str:
    """「实时监控」tab：TD live 每轮状态 + 最近信号事件。

    2026-08-11 方案 A：内存共享——TD live 循环（gatekeeper 进程内
    StrategyExecutor）每轮写 LIVE_STATE，本 handler 同进程直接读取；
    信号事件从事件文件（append-only JSONL）读最近 20 条。页面 JS 按
    exec_params.td_ui_refresh_s 轮询 `?tab=live&frag=1` 刷新内容片段。
    """
    try:
        from nanobot_quant import td_live_state
        st = td_live_state.get_state()
        events = td_live_state.load_events(20)
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
            f'<td>{_live_cell(sb, 9)}</td>'
            f'<td>{_live_cell(ss, 9)}</td>'
            f'<td>{_live_cell(cdb, 13)}</td>'
            f'<td>{_live_cell(cds, 13)}</td>'
            f'<td class="num">{float(score or 0):.1f}</td>'
            f'<td class="num">{float(price or 0):.4f}</td>'
            f'<td class="sig {sig_cls}">{signal}</td>'
            f'<td class="time">{_esc(str(d.get("time", "")))}</td>'
            f'<td>{note}</td></tr>'
        )
    if not rows:
        rows = '<tr><td colspan="10" class="muted" style="text-align:left">暂无数据——TD live 循环未运行或尚未产生第一轮结果</td></tr>'

    ev_rows = ""
    for e in events:
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
        '<h4 style="margin:18px 0 8px">📜 信号历史（最近 20 条）</h4>'
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
            '  function poll(){'
            "    fetch('/config/td-table?tab=live&frag=1')"
            '      .then(function(r){ return r.text(); })'
            '      .then(function(html){'
            "        var el = document.getElementById('live-wrap');"
            "        if (el && html) el.innerHTML = html;"
            '      }).catch(function(){});'
            '  }'
            '  setInterval(poll, secs * 1000);'
            '})();'
            '</script>'
        )
    return html


def _render_snapshot(ticker, bar, limit, strategy_name, params, setup, source="onchainos"):
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
    has_cd = "buy_countdown_count" in disp.columns and disp["buy_countdown_count"].abs().sum() > 0
    has_tdst = "tdst_support" in disp.columns and disp["tdst_support"].notna().any()
    has_score = "combined_score" in disp.columns

    status = _render_status(disp, setup, strategy_name)
    table = ('<table><thead><tr>%s</tr></thead><tbody>\n%s\n</tbody></table>'
             % (_build_headers(has_cd, has_tdst, has_score),
                _build_rows(disp, setup)))
    hint = ('<div class="banner info">最近 %d 根 %s · 数据来源 %s。'
            '当前策略参数来自 td_params.json（%s 独立保存）。</div>'
            % (len(disp), _esc(bar), src_label, _esc(strategy_name)))
    return status + hint + table


def _render_history(ticker, bar, start, end, strategy_name, params, setup, source="onchainos"):
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
    has_cd = "buy_countdown_count" in disp.columns and disp["buy_countdown_count"].abs().sum() > 0
    has_tdst = "tdst_support" in disp.columns and disp["tdst_support"].notna().any()
    has_score = "combined_score" in disp.columns

    rows, agg = signal_stats(seq, setup)
    table = ('<table><thead><tr>%s</tr></thead><tbody>\n%s\n</tbody></table>'
             % (_build_headers(has_cd, has_tdst, has_score), _build_rows(disp, setup)))
    stats = _render_stats_table(rows, agg)
    hint = ('<div class="banner info">%s ~ %s 共 %d 根 %s K 线 · 数据来源 %s · 9 信号 %d 个 · 胜率统计仅含区间内可观察完整后续的信号。</div>'
            % (_esc(start), _esc(end), len(disp), _esc(bar), src_label, len(rows)))
    return hint + table + stats


# ── 路由注册（legion gatekeeper 调用） ───────────────────────────────


def register_td_table_routes(app, gatekeeper) -> None:
    """Mount /config/td-table routes (called from nanobot-legion)."""
    app.get("/config/td-table")(td_table_page)
