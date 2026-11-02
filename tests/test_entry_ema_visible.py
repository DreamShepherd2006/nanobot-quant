"""EMA 折价入场门拦截可见性（2026-11-02）。

背景：该门原先只有 ``print(stderr)``，页面信号历史完全看不到 ——
「有信号没买」在 UI 上等于静默，与周期门控/波锁/资金不足（都写
WAIT/SKIP 事件）不一致。实测案例：ETH setup_buy=10、price=2306.35、
ema610=2281.15、需≤2278.87（还需跌 1.19%）→ 被拦且无任何页面痕迹。
"""

from pathlib import Path

from nanobot_quant import event_log

SRC = Path(__file__).resolve().parents[1] / "src" / "nanobot_quant"
STRATEGY = SRC / "strategies" / "td_sequential_strategy.py"


def test_ema_block_writes_wait_event():
    """拦截分支必须写事件（不是只有 print）。"""
    src = STRATEGY.read_text(encoding="utf-8")
    idx = src.find("entry_ema_blocked = True")
    assert idx > 0, "EMA 折价门分支不见了"
    block = src[idx:idx + 1400]
    assert 'self._record(' in block, "EMA 折价门拦截未写事件"
    assert '"WAIT"' in block
    assert 'status="ema_blocked"' in block, "缺少独立降噪键，会与其他 WAIT 互相挤掉"


def test_ema_block_note_reports_gap():
    """文案要给出「还需跌 X%」，否则看了也不知道差多少。"""
    src = STRATEGY.read_text(encoding="utf-8")
    idx = src.find("entry_ema_blocked = True")
    block = src[idx:idx + 1400]
    assert "EMA 折价门" in block
    assert "还需跌" in block
    assert "_gap_pct" in block


def test_ema_blocked_status_is_throttled():
    """ema_blocked 属「无动作」→ 1 小时内同类只记一条（防 1m 刷屏）。"""
    event_log.reset()
    ev = {"inst_id": "ETH-USDT", "type": "WAIT", "status": "ema_blocked"}
    assert event_log.should_log_event(dict(ev)) is True    # 首条必记
    assert event_log.should_log_event(dict(ev)) is False   # 同小时抑制


def test_ema_blocked_does_not_swallow_other_wait():
    """不同 status 的 WAIT 各占一个降噪键，互不挤掉。"""
    event_log.reset()
    ema = {"inst_id": "ETH-USDT", "type": "WAIT", "status": "ema_blocked"}
    cycle = {"inst_id": "ETH-USDT", "type": "WAIT", "status": "ok"}
    assert event_log.should_log_event(dict(ema)) is True
    assert event_log.should_log_event(dict(cycle)) is True
    assert event_log.should_log_event(dict(ema)) is False
    assert event_log.should_log_event(dict(cycle)) is True  # ok 不在前缀表 → 永远落盘
