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

与 run_td_sequential 共用同一套参数（td_params.json 按策略独立保存）。
"""

from __future__ import annotations

import html as _html
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
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
    return resolve_token(ticker, tokens_json=tokens_json)


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


def _display(df: pd.DataFrame) -> pd.DataFrame:
    """Add Asia/Shanghai display time + pct-change columns."""
    idx = df.index
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    out = df.copy()
    out["_time"] = idx.tz_convert(_TZ).strftime("%Y-%m-%d %H:%M")
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
    heads = ["时间", "收盘", "涨跌%", "Buy Setup", "Sell Setup"]
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


def _form(tab: str, ticker: str, bar: str, limit: int, start: str, end: str) -> str:
    bar_opts = "".join(
        f'<option value="{b}"{" selected" if b == bar else ""}>{b}</option>'
        for b in _BARS
    )
    if tab == "history":
        return (
            '<form class="inline" method="get" action="/config/td-table">'
            '<input type="hidden" name="tab" value="history">'
            '<label>标的</label><input name="ticker" value="%s" size="8">'
            '<label>周期</label><select name="bar">%s</select>'
            '<label>起始</label><input type="date" name="start" value="%s">'
            '<label>结束</label><input type="date" name="end" value="%s">'
            '<button>分析</button></form>' % (_esc(ticker), bar_opts, _esc(start), _esc(end))
        )
    return (
        '<form class="inline" method="get" action="/config/td-table">'
        '<input type="hidden" name="tab" value="snapshot">'
        '<label>标的</label><input name="ticker" value="%s" size="8">'
        '<label>周期</label><select name="bar">%s</select>'
        '<label>K 线数</label><input type="number" name="limit" value="%d" min="20" max="300" style="width:70px">'
        '<button>刷新</button></form>' % (_esc(ticker), bar_opts, limit)
    )


def td_table_page(request: Request) -> HTMLResponse:
    """GET /config/td-table — render the double-tab page.

    Query params: tab=snapshot|history, ticker, bar, limit | start/end.
    """
    q = dict(request.query_params)
    tab = q.get("tab", "snapshot")
    ticker = (q.get("ticker") or _DEFAULT_TICKER).strip().upper()
    bar = q.get("bar") or "1D"
    if bar not in _BARS:
        bar = "1D"
    limit = _query_int(q, "limit", _DEFAULT_LIMIT, 20, 300)
    today = datetime.now()
    end = (q.get("end") or today.strftime("%Y-%m-%d")).strip()
    start = (q.get("start") or (today - timedelta(days=_DEFAULT_HISTORY_DAYS)).strftime("%Y-%m-%d")).strip()

    strategy_name = load_selected()
    strategy_label = get_strategy(strategy_name).label
    params = load_td_params(strategy_name)
    setup = int(params.get("setup_period", 9))

    banner = ""
    content = ""
    if tab == "history":
        content = _render_history(ticker, bar, start, end, strategy_name, params, setup)
    else:
        content = _render_snapshot(ticker, bar, limit, strategy_name, params, setup)

    if isinstance(content, tuple):  # (error_html,)
        banner = content[0]
        content = ""

    page = _load_template()
    page = page.replace("{strategy_label}", _esc(strategy_label))
    page = page.replace("{snap_active}", "active" if tab != "history" else "")
    page = page.replace("{hist_active}", "active" if tab == "history" else "")
    page = page.replace("{ticker}", _esc(ticker))
    page = page.replace("{bar}", _esc(bar))
    page = page.replace("{limit}", str(limit))
    page = page.replace("{start}", _esc(start))
    page = page.replace("{end}", _esc(end))
    page = page.replace("{form}", _form(tab, ticker, bar, limit, start, end))
    page = page.replace("{banner}", banner)
    page = page.replace("{content}", content)
    return HTMLResponse(page)


def _render_snapshot(ticker, bar, limit, strategy_name, params, setup):
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

    seq = _engine_run(df, strategy_name, params)
    disp = _display(seq)
    has_cd = "buy_countdown_count" in disp.columns and disp["buy_countdown_count"].abs().sum() > 0
    has_tdst = "tdst_support" in disp.columns and disp["tdst_support"].notna().any()
    has_score = "combined_score" in disp.columns

    status = _render_status(disp, setup, strategy_name)
    table = ('<table><thead><tr>%s</tr></thead><tbody>\n%s\n</tbody></table>'
             % (_build_headers(has_cd, has_tdst, has_score),
                _build_rows(disp, setup)))
    hint = ('<div class="banner info">最近 %d 根 %s · 数据来源 OnchainOS（%s/%s）。'
            '当前策略参数来自 td_params.json（%s 独立保存）。</div>'
            % (len(disp), _esc(bar), _esc(resolved["chain"]), _esc(resolved["address"]), _esc(strategy_name)))
    return status + hint + table


def _render_history(ticker, bar, start, end, strategy_name, params, setup):
    resolved = _resolve_for_table(ticker)
    if not resolved.get("ok"):
        return ('<div class="banner err">标的解析失败：%s（%s）</div>'
                % (_esc(resolved.get("issue") or "unknown"), _esc(resolved.get("category") or ""))), None
    try:
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
    except ValueError:
        return '<div class="banner err">日期格式错误（应为 YYYY-MM-DD）。</div>', None
    try:
        df = fetch_kline_range(resolved["chain"], resolved["address"],
                               start=start_dt, end=end_dt, bar=bar)
    except Exception as exc:
        return ('<div class="banner err">K 线获取失败：%s</div>' % _esc(exc)), None
    if df.empty:
        return ('<div class="banner err">%s 在 %s ~ %s 无 %s K 线数据。</div>'
                % (_esc(ticker), _esc(start), _esc(end), _esc(bar))), None

    seq = _engine_run(df, strategy_name, params)
    disp = _display(seq)
    has_cd = "buy_countdown_count" in disp.columns and disp["buy_countdown_count"].abs().sum() > 0
    has_tdst = "tdst_support" in disp.columns and disp["tdst_support"].notna().any()
    has_score = "combined_score" in disp.columns

    rows, agg = signal_stats(seq, setup)
    table = ('<table><thead><tr>%s</tr></thead><tbody>\n%s\n</tbody></table>'
             % (_build_headers(has_cd, has_tdst, has_score), _build_rows(disp, setup)))
    stats = _render_stats_table(rows, agg)
    hint = ('<div class="banner info">%s ~ %s 共 %d 根 %s K 线 · 9 信号 %d 个 · 胜率统计仅含区间内可观察完整后续的信号。</div>'
            % (_esc(start), _esc(end), len(disp), _esc(bar), len(rows)))
    return hint + table + stats


# ── 路由注册（legion gatekeeper 调用） ───────────────────────────────


def register_td_table_routes(app, gatekeeper) -> None:
    """Mount /config/td-table routes (called from nanobot-legion)."""
    app.get("/config/td-table")(td_table_page)
