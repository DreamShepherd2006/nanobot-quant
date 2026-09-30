"""事件重复抑制（2026-09-30）。

规则：同类「无动作」事件（skipped_* / failed / pending_confirm / dry_run* /
no_action / no_signal / cycle_wait）按 (inst_id, type, status) 去重 —— 首条必记，
之后最少间隔 THROTTLE_S（1 小时）才再记一条；真实动作不受限制。
"""

from nanobot_quant import event_log


def _ev(status="skipped_expired", inst="SOL-USD_UM-260930-123-P", ev_type="exit"):
    return {"ts": "2026-09-30T03:28:03Z", "type": ev_type, "inst_id": inst,
            "sz": 1, "reason": "expired", "status": status,
            "note": "已到期，不提交买回（等台账到期判定闭回）"}


def test_same_no_action_event_suppressed_within_window():
    assert event_log.should_log_event(_ev()) is True    # 首条必记
    assert event_log.should_log_event(_ev()) is False   # 同类重复 → 抑制
    assert event_log.should_log_event(_ev()) is False


def test_status_change_is_logged():
    assert event_log.should_log_event(_ev(status="skipped_expired")) is True
    assert event_log.should_log_event(_ev(status="failed")) is True
    # 再回到旧状态仍受窗口约束（键含 status，各状态独立计时）
    assert event_log.should_log_event(_ev(status="skipped_expired")) is False


def test_different_contract_or_type_not_collapsed():
    assert event_log.should_log_event(_ev()) is True
    assert event_log.should_log_event(_ev(inst="SOL-USD_UM-261002-114-P")) is True
    assert event_log.should_log_event(_ev(ev_type="entry")) is True


def test_real_actions_never_suppressed():
    for status in ("sold", "bought_back", "settled_itm", "settled_otm",
                   "settled_review", "covered"):
        for _ in range(3):
            assert event_log.should_log_event(_ev(status=status)) is True


def test_dry_run_intents_throttled():
    assert event_log.should_log_event(_ev(status="dry_run(would_sell)")) is True
    assert event_log.should_log_event(_ev(status="dry_run(would_sell)")) is False


def test_throttle_expires_after_window(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(event_log.time, "time", lambda: clock["now"])
    assert event_log.should_log_event(_ev()) is True
    assert event_log.should_log_event(_ev()) is False
    clock["now"] += event_log.THROTTLE_S - 1
    assert event_log.should_log_event(_ev()) is False
    clock["now"] += 2                                   # 超过窗口 → 续记一条
    assert event_log.should_log_event(_ev()) is True
    assert event_log.should_log_event(_ev()) is False


def test_reset_clears_memory():
    assert event_log.should_log_event(_ev()) is True
    assert event_log.should_log_event(_ev()) is False
    event_log.reset()
    assert event_log.should_log_event(_ev()) is True
