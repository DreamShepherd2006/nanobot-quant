"""信号周期门控（``okx_options_strategy.cycle_gate``）单元测试。

背景：期权线首版无门控，回测里同一个「setup_buy 9 → 10 → 11」衰竭波
连开三张，31 天 7 个独立信号被记成 12 次开仓（样本虚高 1.7 倍）。门控
抽成纯函数，实盘（策略类）与回测（driver）共用同一份实现。
"""

from nanobot_quant.okx_options_strategy import cycle_gate, cycle_mark_bought

P = {"entry_setup": 9, "entry_countdown": 13}


def _sig(setup=0, cd=0):
    return {"setup_buy": setup, "cd_buy": cd}


def test_first_signal_passes():
    state: dict = {}
    assert cycle_gate(state, "SOL-USD_UM", td_signal=_sig(9), params=P) is None


def test_same_cycle_after_buy_is_blocked():
    """setup 9 建仓后，累加期 10 / 11 / 12 全部拦下。"""
    state: dict = {}
    assert cycle_gate(state, "SOL-USD_UM", td_signal=_sig(9), params=P) is None
    cycle_mark_bought(state, "SOL-USD_UM")
    for su in (10, 11, 12):
        gate = cycle_gate(state, "SOL-USD_UM", td_signal=_sig(su), params=P)
        assert gate and "同周期已建仓" in gate, f"setup={su} 应被拦"


def test_reset_allows_new_cycle():
    """计数变小（reset）→ 新周期放行。"""
    state: dict = {}
    cycle_gate(state, "SOL-USD_UM", td_signal=_sig(12), params=P)
    cycle_mark_bought(state, "SOL-USD_UM")
    # 计数变小 → reset
    assert cycle_gate(state, "SOL-USD_UM", td_signal=_sig(2), params=P) is None
    # 新周期再数到 9 → 放行
    assert cycle_gate(state, "SOL-USD_UM", td_signal=_sig(9), params=P) is None


def test_cd_after_setup_buy_is_blocked():
    """setup 9 建仓后，同 countdown 周期的 cd13 不得补买。"""
    state: dict = {}
    cycle_gate(state, "SOL-USD_UM", td_signal=_sig(9), params=P)
    cycle_mark_bought(state, "SOL-USD_UM")
    # setup 翻转（计数变小）但 cd_buy 仍在累积 → cd_triggered 保持
    cycle_gate(state, "SOL-USD_UM", td_signal=_sig(1, cd=10), params=P)
    gate = cycle_gate(state, "SOL-USD_UM", td_signal=_sig(1, cd=13), params=P)
    assert gate and "countdown 周期" in gate


def test_cd_clears_when_countdown_completes():
    """setup 翻转 + cd 归 0 → cd_triggered 清位，之后新周期 cd13 可开。"""
    state: dict = {}
    cycle_gate(state, "SOL-USD_UM", td_signal=_sig(9), params=P)
    cycle_mark_bought(state, "SOL-USD_UM")
    cycle_gate(state, "SOL-USD_UM", td_signal=_sig(1, cd=0), params=P)  # reset + cd=0
    gate = cycle_gate(state, "SOL-USD_UM", td_signal=_sig(1, cd=13), params=P)
    assert gate is None, "cd 周期结束后应放行新周期的 cd13"


def test_no_signal_never_blocked():
    """没信号时门控不参与（返回 None），且不改变 bought 语义。"""
    state: dict = {}
    for su in range(0, 9):
        assert cycle_gate(state, "SOL-USD_UM", td_signal=_sig(su), params=P) is None


def test_restart_boundary_blocks_when_position_exists():
    """重启边界：已有持仓 → 视为本周期已建仓（保守不追）。"""
    state: dict = {}
    gate = cycle_gate(state, "SOL-USD_UM", td_signal=_sig(9), params=P,
                      has_position=True)
    assert gate and "同周期已建仓" in gate


def test_state_is_per_family():
    """状态按家族隔离：SOL 建仓不影响 BTC。"""
    state: dict = {}
    cycle_gate(state, "SOL-USD_UM", td_signal=_sig(9), params=P)
    cycle_mark_bought(state, "SOL-USD_UM")
    assert cycle_gate(state, "BTC-USD_UM", td_signal=_sig(9), params=P) is None


def test_cd_only_entry_still_works():
    """setup 从未到 9 时，cd13 独立触发仍可开仓（设计用途）。"""
    state: dict = {}
    for su in (1, 2, 3):
        cycle_gate(state, "SOL-USD_UM", td_signal=_sig(su, cd=su), params=P)
    assert cycle_gate(state, "SOL-USD_UM", td_signal=_sig(3, cd=13), params=P) is None


def test_mark_bought_on_unknown_family_is_noop():
    """对未初始化家族置位不报错（防御）。"""
    state: dict = {}
    cycle_mark_bought(state, "ETH-USD_UM")  # 不抛异常即可


def test_mark_bought_consumes_reset_flag():
    """建仓必须消费掉 reset —— 否则门控永久失效（首版实测的回归）。

    场景：先经历一次计数回落（reset 置位），然后新周期数到 9 建仓 ——
    建仓后 reset 必须已清，紧接着的 setup=10 才拦得住。
    """
    state: dict = {}
    # 制造计数回落：5 → 1
    cycle_gate(state, "SOL-USD_UM", td_signal=_sig(5), params=P)
    cycle_gate(state, "SOL-USD_UM", td_signal=_sig(1), params=P)
    assert state["SOL-USD_UM"]["reset"] is True
    # 新周期数到 9 → 放行建仓
    assert cycle_gate(state, "SOL-USD_UM", td_signal=_sig(9), params=P) is None
    cycle_mark_bought(state, "SOL-USD_UM")
    # 建仓后 reset 已清 → 累加期 10 / 11 必须被拦
    assert state["SOL-USD_UM"]["reset"] is False
    for su in (10, 11):
        gate = cycle_gate(state, "SOL-USD_UM", td_signal=_sig(su), params=P)
        assert gate and "同周期已建仓" in gate, f"setup={su} 应被拦（reset 未消费）"
