"""期权链页展示层打磨（2026-10-01 六条）的标记断言。

逐条对应：
① 表格上方补「报价单位 = USD/1 名义币，每张 = 报价 × 面值」
② Call 侧补「卖方实收/张(bid) + 卖方年化(bid)」两列（后端不再只算 put）
③ 「实值」两列统一显示同一 itm-tag 标记（此前一列显示「实值」、一列显示「—」）
④ 策略事件历史行的「已到期」→「（现已到期）」（避免误读成当时已到期）
⑤ 本地台账新增「实际成交均价」列（filled_px，与「开仓价(限)」并列）
⑥ 到期提醒纳入轮询（setInterval(loadReminder)）
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "src" / "nanobot_quant" / "okx_options_page.html").read_text(encoding="utf-8")
DATA = (ROOT / "src" / "nanobot_quant" / "okx_options_data.py").read_text(encoding="utf-8")


def test_unit_note_added_above_chain():
    assert "报价单位 = <b>USD / 1 个名义" in PAGE
    assert "每张金额 = 报价 × 面值" in PAGE


def test_call_side_seller_columns_added():
    # 表头：Call 组扩到 5 列，并在 Call 段落后插入两个卖方列
    assert 'colspan="5" class="sec call"' in PAGE
    assert PAGE.count("卖方实收/张(bid)") >= 2, "call / put 两侧都应有该列"
    assert PAGE.count("卖方年化(bid)") >= 2
    # 数据行：call 侧渲染 bid_usd / bid_apr_pct
    assert "c.bid_usd" in PAGE and "c.bid_apr_pct" in PAGE


def test_call_premium_computed_in_backend():
    # 后端：卖方实收/年化不再只对 put 计算
    assert 'if side == "P" and lot and spot:' not in DATA
    assert 'if lot and spot and side in ("C", "P"):' in DATA


def test_itm_tag_consistent_both_columns():
    # 两个年化列都走同一个 itm-tag（不再一列「实值」一列「—」）
    assert "const ITM_TIP" in PAGE
    assert PAGE.count("? '<span class=\"itm-tag\" title=\"' + ITM_TIP + '\">实值</span>'") == 3, \
        "call 年化列 + put 两列年化列应共用同一标记"
    assert 'title="实值档不显示年化"' not in PAGE


def test_event_row_expired_wording():
    assert "（现已到期）" in PAGE
    assert ">已到期</span>" not in PAGE


def test_ledger_fill_price_column():
    assert "实际成交均价" in PAGE
    assert "e.filled_px" in PAGE


def test_reminder_polling_added():
    assert "setInterval(loadReminder" in PAGE
