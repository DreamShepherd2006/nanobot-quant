"""逐 bar 明细 CSV 导出的契约测试（2026-10-20）。

导出是纯展示层：列固定、口径与页面表一致、数值不 round。这里锁住：
- 表头（含 slope 列名跟随 k）
- 时间双列（UTC 原值 + CST=UTC+8，不经服务器本地时区）
- 闸门三态文案、Setup 0 留空、涨跌% 换算
- CSV 转义（动作文本里带逗号/引号不会串列）
- 旧记录（无 bars_rows）只出表头，不报错
"""
import csv
import io

from nanobot_quant.backtest_csv import bars_to_csv, bars_header

ROW = {
    "ts": "2026-10-16T16:00:00",
    "symbol": "SOL",
    "close": 103.77,
    "chg": -0.0002,
    "setup_buy": 12,
    "setup_sell": 0,
    "score": 1.33,
    "ema": 105.1712345678,
    "slope": -0.1234,
    "gate": "block",
    "action": "WAIT:EMA 趋势闸门拦截（slope≤0）",
    "fill": "",
}


def _parse(text: str):
    assert text.startswith("\ufeff"), "缺少 BOM（Excel 中文会乱码）"
    return list(csv.reader(io.StringIO(text[1:])))


def test_header_and_rows():
    text = bars_to_csv({"bars_rows": [ROW], "bars_meta": {"ema_k": 3}})
    rows = _parse(text)
    assert rows[0] == bars_header({"ema_k": 3})
    assert rows[0][9] == "slope3"
    body = rows[1]
    assert body[0] == "2026-10-17 00:00:00"      # CST = UTC+8
    assert body[1] == "2026-10-16 16:00:00"      # UTC 原值
    assert body[2] == "SOL"
    assert body[4] == "-0.0200"                  # 涨跌% 由小数换算
    assert body[5] == "12" and body[6] == ""     # setup_sell=0 留空
    assert body[8] == "105.1712346"              # 10 位有效数字、不 round
    assert body[9] == "-0.1234"
    assert body[10] == "拦"
    assert body[11] == "WAIT:EMA 趋势闸门拦截（slope≤0）"
    assert body[12] == ""


def test_slope_header_follows_k():
    assert bars_to_csv({"bars_rows": [], "bars_meta": {"ema_k": 12}}).split("\r\n")[0][-200:].count("slope12") == 1


def test_gate_tristate_text():
    cases = {"pass": "放行", "na": "预热不足", "block": "拦", None: "", "": ""}
    rows = []
    for g in cases:
        r = dict(ROW, gate=g)
        rows.append(r)
    out = _parse(bars_to_csv({"bars_rows": rows}))
    assert [r[10] for r in out[1:]] == list(cases.values())


def test_csv_escaping_with_comma_and_quote():
    r = dict(ROW, action='WAIT:含逗号, 与"引号" 的文本', fill="LONG buy 10U, qty=0.095")
    out = _parse(bars_to_csv({"bars_rows": [r]}))
    assert out[1][11] == 'WAIT:含逗号, 与"引号" 的文本'
    assert out[1][12] == "LONG buy 10U, qty=0.095"


def test_tz_aware_ts_is_converted():
    r = dict(ROW, ts="2026-10-16T16:00:00+00:00")
    out = _parse(bars_to_csv({"bars_rows": [r]}))
    assert out[1][0] == "2026-10-17 00:00:00"
    assert out[1][1] == "2026-10-16 16:00:00"


def test_missing_rows_yields_header_only():
    for payload in ({}, {"bars_rows": None}, {"bars_rows": []}):
        out = _parse(bars_to_csv(payload))
        assert len(out) == 1
        assert out[0] == bars_header({})


def test_none_values_do_not_break_columns():
    r = {"ts": None, "symbol": None, "close": None, "chg": None, "setup_buy": None,
         "setup_sell": None, "score": None, "ema": None, "slope": None,
         "gate": None, "action": None, "fill": None}
    out = _parse(bars_to_csv({"bars_rows": [r]}))
    assert len(out[1]) == 13
    assert all(c == "" for c in out[1])


def test_all_rows_exported_regardless_of_page_filters():
    rows = [dict(ROW, setup_buy=i + 1) for i in range(25)]
    out = _parse(bars_to_csv({"bars_rows": rows}))
    assert len(out) - 1 == 25


def test_merge_csv_aligned_on_finest_period():
    """多周期对齐 CSV（2026-10-21 方案 A）：行基准 = 最细周期，缺 bar 的格子留空。"""
    from nanobot_quant.backtest_csv import bars_merge_to_csv

    def row(ts, setup_buy=0, gate="", action="", fill="", close=100.0):
        return {
            "ts": ts, "symbol": "SOL", "close": close, "chg": 0.001,
            "setup_buy": setup_buy, "setup_sell": 0, "score": 0.0,
            "ema": 101.5, "slope": 0.0123, "gate": gate,
            "action": action, "fill": fill,
        }

    result = {
        "periods": ["1m", "3m"],
        "period_results": {
            "1m": {
                "bars_meta": {"ema_k": 3},
                "bars_rows": [
                    row("2026-10-17T00:00:00", 12, "block", "WAIT:EMA", ""),
                    row("2026-10-17T00:01:00", 13, "block", "WAIT:EMA", ""),
                    row("2026-10-17T00:02:00", 14, "block", "WAIT:EMA", ""),
                    row("2026-10-17T00:03:00", 0, "", "", "", 100.5),
                ],
            },
            "3m": {
                "bars_meta": {"ema_k": 3},
                "bars_rows": [row("2026-10-17T00:00:00", 4, "pass", "", "BUY 0.0952")],
            },
        },
    }
    text = bars_merge_to_csv(result)
    assert text.startswith("\ufeff")
    lines = [ln for ln in text.replace("\ufeff", "").split("\r\n") if ln]
    head = lines[0].split(",")
    # 5 固定列 + 2 周期 × 7 列
    assert len(head) == 5 + 14
    assert head[5:12] == ["1m Buy Setup", "1m Sell Setup", "1m EMA", "1m slope3",
                          "1m 闸门", "1m 动作/原因", "1m 成交"]
    assert head[12] == "3m Buy Setup"
    # 行数 = 最细周期 4 行
    assert len(lines) == 1 + 4
    # 00:00 行：两档都有值
    r0 = lines[1].split(",")
    assert r0[5] == "12" and r0[9] == "拦" and r0[12] == "4" and r0[16] == "放行"
    # 00:01 行：3m 档无 bar → 7 列全空（不能写 0）
    r1 = lines[2].split(",")
    assert r1[12:] == [""] * 7
