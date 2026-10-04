"""台账展示层打磨（2026-10-04 批次）的标记断言。

逐条对应：
①a 「账号」列统一显示子账号名（UID→名字映射，UID 放 tooltip）
①b 台账新增「结算价」「毛赔付(USD)」两列（判定时已写入台账行）
①c 手续费列在 settled 行合并显示「开仓费 + 行权费」（新增 settle_fee 落盘）
①d 表格标题「卖 put 台账」→「期权台账」（表内含 put/call 两类）
①e 「成交价」列补 tooltip 说明与官方账单的口径差
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "src" / "nanobot_quant" / "okx_options_page.html").read_text(encoding="utf-8")
TRADE = (ROOT / "src" / "nanobot_quant" / "okx_options_trade.py").read_text(encoding="utf-8")


def test_ledger_title_covers_calls():
    assert "<h3>🧾 期权台账</h3>" in PAGE
    assert "卖 put 台账" not in PAGE


def test_account_column_uses_name_mapping():
    assert "let ACCT_LABEL = {};" in PAGE
    assert "function acctLabel(acc)" in PAGE
    assert "ACCT_LABEL[String(a.uid)] = a.name" in PAGE
    assert "acctLabel(e.account)" in PAGE
    assert "'</td><td>' + esc(e.account || \"\") + '</td>" not in PAGE


def test_settle_columns_present():
    assert ">结算价</th>" in PAGE
    assert ">毛赔付(USD)</th>" in PAGE
    assert "e.settle_px" in PAGE and "e.settle_payout" in PAGE


def test_settle_fee_merged_into_fee_column():
    assert "function feeUsd(e)" in PAGE and "e.settle_fee" in PAGE
    # 后端两处（判定 + 历史回填）都要落 settle_fee
    assert TRADE.count('"settle_fee": (abs(_f(row.get("fee")))') == 2


def test_filled_px_tooltip_mentions_bill_basis():
    assert "官方账单「成交价」列是交易所原值" in PAGE
