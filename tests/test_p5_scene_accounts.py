"""P5：场景子账号池按执行通道给候选（2026-10-29）。

背景：`/config/exec` 的场景卡「子账号池」候选此前一律来自 Gate（`gate_botN`），
而 okx_cex 通道的已配置账号是 `DreamShepherdbotN` ⇒ 场景池里存的值永远匹配不上，
运行时整批忽略（日志「场景池 [...] 不属于该通道已配置子账号，已忽略」），
用户无法为场景指定用哪几个 OKX 子账号。

本测试锁住：
  ① okx_cex 通道 → 候选 = 已配置的 OKX 现货子账号（排除期权占用账号）；
  ② gate 通道 → 沿用 Gate 逻辑（不受影响）；
  ③ 池中含不属于当前通道的账号 → 场景卡显示显式提示（零静默）。
"""
from __future__ import annotations

import pytest

from nanobot_quant import exec_params_handlers as h


@pytest.fixture()
def fake_okx(monkeypatch):
    """三个已配置 + 一个缺字段的 OKX 子账号；期权占用 bot1。"""
    rows = [
        {"name": "DreamShepherdbot1", "configured": True},
        {"name": "DreamShepherdbot2", "configured": True},
        {"name": "DreamShepherdbot3", "configured": True},
        {"name": "DreamShepherdbot4", "configured": False},
    ]
    monkeypatch.setattr("nanobot_quant.okx_cex_credentials.list_sub_accounts",
                        lambda creds=None: rows)
    monkeypatch.setattr("nanobot_quant.okx_cex_credentials.option_account_name",
                        lambda: "DreamShepherdbot1")


def test_okx_cex_candidates_exclude_option_account(fake_okx):
    got = h._channel_accounts("okx_cex")
    assert got == ["DreamShepherdbot2", "DreamShepherdbot3"], got
    # 期权账号与未配置完整的账号都不应出现
    assert "DreamShepherdbot1" not in got
    assert "DreamShepherdbot4" not in got


def test_gate_channel_keeps_gate_candidates(monkeypatch):
    monkeypatch.setattr(h, "_gate_accounts", lambda: ["gate_bot1", "gate_bot2"])
    assert h._channel_accounts("gate") == ["gate_bot1", "gate_bot2"]


def test_dex_channel_falls_back_without_crash(monkeypatch):
    monkeypatch.setattr(h, "_gate_accounts", lambda: ["gate_bot1"])
    assert h._channel_accounts("okx_dex") == ["gate_bot1"]


def test_invalid_pool_names_detects_cross_channel_values():
    assert h._invalid_pool_names(
        ["gate_bot1", "gate_bot2"], ["DreamShepherdbot2"]) == ["gate_bot1", "gate_bot2"]
    assert h._invalid_pool_names(
        ["DreamShepherdbot2"], ["DreamShepherdbot2", "DreamShepherdbot3"]) == []
    # 候选为空（配置读不出来）时不做判断，避免误报整池无效
    assert h._invalid_pool_names(["gate_bot1"], []) == []


def test_scene_card_warns_when_pool_belongs_to_other_channel():
    scene = {"sub_accounts": ["gate_bot1", "gate_bot2"], "symbols": ["SOL"]}
    html = h._scene_card_html("high", scene, {}, "cex",
                              ["DreamShepherdbot2", "DreamShepherdbot3"], 1,
                              candidates=["DreamShepherdbot2", "DreamShepherdbot3"])
    assert "不属于当前执行通道" in html
    assert "gate_bot1" in html


def test_scene_card_no_warning_when_pool_matches_channel():
    scene = {"sub_accounts": ["DreamShepherdbot2"], "symbols": ["SOL"]}
    html = h._scene_card_html("high", scene, {}, "cex",
                              ["DreamShepherdbot2"], 1,
                              candidates=["DreamShepherdbot2"])
    assert "不属于当前执行通道" not in html
