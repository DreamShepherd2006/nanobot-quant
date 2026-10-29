"""pending 确认的空转守卫（2026-10-29，okx_cex 通道实测）。

现场：CEX 通道每轮迭代开头都打一条 `[DIAG] ensure_onchainos_dir`——
`_check_pending_confirmations()` 无 pending 时仍无条件调
`self._home_account_id()`（走 onchainos CLI 的 wallet accounts），白跑一次
DEX 命令。改为「无 pending 立即返回 + home 惰性获取」。
"""

from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _bare_strategy():
    st = TdSequentialStrategy.__new__(TdSequentialStrategy)
    st._pending_sells = {}
    st._pending_buys = {}
    return st


def test_noop_when_no_pending():
    st = _bare_strategy()
    touched = []
    st._home_account_id = lambda: touched.append("home") or "acct-home"
    st._wallet_switch = lambda aid: touched.append(("switch", aid)) or True

    st._check_pending_confirmations()

    assert touched == [], "无 pending 时不得触碰 DEX（onchainos CLI / wallet switch）"


def test_missing_pending_attrs_do_not_raise():
    st = TdSequentialStrategy.__new__(TdSequentialStrategy)   # 属性未初始化
    st._check_pending_confirmations()                          # 不得 AttributeError
