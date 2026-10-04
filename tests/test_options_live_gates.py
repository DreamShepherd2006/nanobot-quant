"""期权线 F1 环境闸门接线（2026-10-04 批 1，默认关）—— 零行为变更 + 按家族方向。

口径：默认关 ⇒ 即使传入极端 F1 也不产生任何拦截（逐笔行为与接线前一致）；
开启后按 ``params["f1_gate"][family]`` 的方向/阈值判定，样本不足或未配置 ⇒ 放行。
"""
from nanobot_quant import okx_options_strategy as oss

SIG = {"setup_buy": 9, "cd_buy": 0, "setup_sell": 0, "cd_sell": 0,
       "recommendation": "HOLD", "price": 100.0,
       "tdst_support": 0.0, "tdst_resistance": 0.0, "score": 0, "rvol": 1.0}
FAM = "SOL-USD_UM"


def _sel_empty(monkeypatch):
    monkeypatch.setattr(oss, "select_puts", lambda *a, **k: {"candidates": []})


def test_gate_off_by_default_never_blocks(monkeypatch):
    """默认关：极端 F1 也不拦截（零行为变更）。"""
    _sel_empty(monkeypatch)
    dec, note = oss.evaluate_entry(FAM, td_signal=SIG, params={"entry_setup": 9}, f1=9.9)
    assert dec is None and "F1 闸门" not in note


def test_block_low_ok(monkeypatch):
    p = {"entry_setup": 9, "f1_gate_enabled": True,
         "f1_gate": {FAM: {"threshold": 1.0, "direction": "low_ok"}}}
    dec, note = oss.evaluate_entry(FAM, td_signal=SIG, params=p, f1=1.2)
    assert dec is None and "F1 闸门" in note and "波动扩张区" in note


def test_block_high_ok(monkeypatch):
    p = {"entry_setup": 9, "f1_gate_enabled": True,
         "f1_gate": {FAM: {"threshold": 1.0, "direction": "high_ok"}}}
    dec, note = oss.evaluate_entry(FAM, td_signal=SIG, params=p, f1=0.8)
    assert dec is None and "F1 闸门" in note and "波动收缩区" in note


def test_missing_sample_fail_open(monkeypatch):
    """闸门开但无样本 ⇒ 放行（不静默：日志端由策略层 _log 输出）。"""
    _sel_empty(monkeypatch)
    p = {"entry_setup": 9, "f1_gate_enabled": True, "f1_gate": {FAM: {"threshold": 1.0}}}
    dec, note = oss.evaluate_entry(FAM, td_signal=SIG, params=p, f1=None)
    assert dec is None and "无合格候选" in note          # 已越过闸门、卡在选档


def test_unconfigured_family_fail_open(monkeypatch):
    _sel_empty(monkeypatch)
    p = {"entry_setup": 9, "f1_gate_enabled": True,
         "f1_gate": {"BTC-USD_UM": {"threshold": 1.0}}}
    dec, note = oss.evaluate_entry(FAM, td_signal=SIG, params=p, f1=None)
    assert dec is None and "无合格候选" in note


def test_pass_case_gets_through_gate(monkeypatch):
    """F1 低于阈值 ⇒ 通过闸门（卡在选档，说明未被拦）。"""
    _sel_empty(monkeypatch)
    p = {"entry_setup": 9, "f1_gate_enabled": True,
         "f1_gate": {FAM: {"threshold": 1.0, "direction": "low_ok"}}}
    dec, note = oss.evaluate_entry(FAM, td_signal=SIG, params=p, f1=0.9)
    assert dec is None and "无合格候选" in note
