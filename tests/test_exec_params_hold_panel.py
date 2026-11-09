"""EOD 豁免批次的持仓勾选面板（2026-11-09，展示层）。

文本框仍是唯一真相源：后端只负责渲染台账快照行 + 手填框；
勾选 ↔ 文本框的同步在前端 JS（此处用结构断言锁定，逻辑靠页面实测）。
"""

from __future__ import annotations

import nanobot_quant.batches as batches_mod
from nanobot_quant.exec_params_handlers import (
    _PAGE_HTML,
    _field_html,
    _holdings_for_scene,
)

_LOTS = {
    "CRCL": [
        {"slot": 6, "lot": {"qty": 0.0598, "entry_price": 66.02,
                            "entry_time": "2026-11-08T22:10:00+00:00"}},
        {"slot": 1, "lot": {"qty": 0.0599, "entry_price": 76.14,
                            "entry_time": "2026-11-07T12:20:00+00:00"}},
    ],
    "BNB": [
        {"slot": 1, "lot": {"qty": 0.00578, "entry_price": 684.1,
                            "entry_time": "2026-11-08T22:02:00+00:00"}},
    ],
}


def _patch_ledger(monkeypatch):
    def fake_load(*, symbol, channel=None, scene=None):
        if symbol not in _LOTS:
            return None

        class _BM:
            def open_slots(self):
                return _LOTS[symbol]

        return _BM()

    monkeypatch.setattr(batches_mod.BatchManager, "load", staticmethod(fake_load))


def test_holdings_sorted_and_symbol_exempt_flag(monkeypatch):
    _patch_ledger(monkeypatch)
    rows = _holdings_for_scene(
        "high",
        {"symbols": ["CRCL", "BNB", "ETH"], "eod_exempt_symbols": ["BNB"]},
        "okx_cex",
    )
    # 按 标的 → slot 升序；无台账的标的（ETH）静默跳过
    assert [(r["symbol"], r["slot"]) for r in rows] == [
        ("BNB", 1), ("CRCL", 1), ("CRCL", 6)]
    assert rows[0]["symbol_exempt"] is True
    assert rows[1]["symbol_exempt"] is False
    assert rows[1]["entry_time"] == "2026-11-07 12:20"
    assert rows[1]["qty"] == 0.0599 and rows[1]["entry_price"] == 76.14


def test_holdings_empty_when_ledger_unreadable(monkeypatch):
    def boom(*, symbol, channel=None, scene=None):
        raise OSError("FUSE jitter")

    monkeypatch.setattr(batches_mod.BatchManager, "load", staticmethod(boom))
    assert _holdings_for_scene("high", {"symbols": ["CRCL"]}, "okx_cex") == []


def test_field_renders_hold_panel():
    holdings = [
        {"symbol": "BNB", "slot": 1, "qty": 0.00578, "entry_price": 684.1,
         "entry_time": "2026-11-08 22:02", "symbol_exempt": True},
        {"symbol": "CRCL", "slot": 6, "qty": 0.0598, "entry_price": 66.02,
         "entry_time": "2026-11-08 22:10", "symbol_exempt": False},
    ]
    out = _field_html(
        "eod_exempt_slots", "CRCL:6", None, "cex", "scenes_high_",
        field_name="eod_exempt_slots", holdings=holdings,
    )
    # 手填框仍是唯一真相源（name/id 不变 → collect() 与保存逻辑零改动）
    assert 'name="scenes_high_eod_exempt_slots"' in out
    assert 'value="CRCL:6"' in out
    # 勾选面板
    assert 'data-hold-for="scenes_high_eod_exempt_slots"' in out
    assert 'data-key="CRCL:6"' in out and 'data-key="BNB:1"' in out
    assert "hold-tbl" in out and "hold-cb" in out
    # 定点显示（不用科学计数法）与提示
    assert "0.0598" in out and "66.02" in out and "0.00578" in out
    assert "已在标的豁免" in out
    assert "刷新持仓" in out


def test_field_renders_empty_state():
    out = _field_html("eod_exempt_slots", "", None, "cex", "scenes_mid_",
                      field_name="eod_exempt_slots", holdings=[])
    assert "当前无 open 批次（台账快照）" in out
    assert "hold-row" not in out


def test_page_carries_hold_panel_js_and_css():
    """模板是 .replace() 渲染：JS 里绝不能出现双花括号转义。"""
    for token in (
        "initHoldPanels", "parseSlotSpec", "writeSpec", "renderOrphans",
        ".hold-tbl", ".hold-row-na", ".orphan-del", ".hold-refresh",
    ):
        assert token in _PAGE_HTML, token
    assert "{{" not in _PAGE_HTML
