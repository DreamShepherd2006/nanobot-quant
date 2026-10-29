"""OKX CEX 现货通道 P3-2a：slot 轮转分配 + 余额单轮缓存（§33.52）。

覆盖：
- `shared_pool_account_ids`：多账号轮转（A2：3 账号 × 每账号 N slot）、
  场景池优先、已配置账号兜底、单账号退化为旧行为、无账号 fail-closed
- 策略层：shared_pool 下同账号多 slot 只查一次余额（单轮缓存）、
  查询失败缓存空 dict（fail-closed 不重试）、per_account 通道零影响
- `_activate_scene` 每轮清空缓存

对应 PR：OKX CEX P3-2a（通道仍未开放，零用户可见行为变化）。
"""

from __future__ import annotations

import pytest

from nanobot_quant.batches import shared_pool_account_ids
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


# ── helpers ──────────────────────────────────────────────────────────────


def _strategy(params: dict):
    st = TdSequentialStrategy.__new__(TdSequentialStrategy)
    st.parameters = params
    st._cex_brokers = {}
    st._slot_balances = {}
    st._name_to_uid = {}
    st._balances_error = None
    st._price_cache = {}
    st._PRICE_CACHE_TTL = 15
    st._sp_balances = {}
    st.symbols = []
    return st


class _FakeBroker:
    """只统计 `_balances()` 调用次数（不触网）。"""

    def __init__(self, balances=None, error=None):
        self.calls = 0
        self._balances_data = balances or {}
        self._error = error

    def _balances(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._balances_data


# ── 轮转分配 ─────────────────────────────────────────────────────────────


def test_ids_rotate_across_scene_pool(monkeypatch):
    """3 账号 × 4 逻辑 slot → 轮转 a,b,c,a（A2：单账号内共享余额池）。"""
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: [
            {"name": n, "configured": True} for n in ("okx_a", "okx_b", "okx_c")
        ],
    )
    ids, err = shared_pool_account_ids(4, ["okx_a", "okx_b", "okx_c"])
    assert err == ""
    assert ids == ["okx_a", "okx_b", "okx_c", "okx_a"]


def test_ids_single_account_degrades_to_old_behaviour(monkeypatch):
    """账号池只有 1 个 → 全部 slot 同一账号（与 P3-1 行为一致）。"""
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: [{"name": "okx_main", "configured": True}],
    )
    ids, err = shared_pool_account_ids(3, ["okx_main"])
    assert (ids, err) == (["okx_main"] * 3, "")


def test_ids_use_all_configured_accounts(monkeypatch):
    """无场景池时用**全部**已配置账号（不再只取第一个）轮转。"""
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: [
            {"name": "bot1", "configured": True},
            {"name": "未配置", "configured": False},
            {"name": "bot2", "configured": True},
        ],
    )
    ids, err = shared_pool_account_ids(5)
    assert err == ""
    assert ids == ["bot1", "bot2", "bot1", "bot2", "bot1"]


def test_ids_scene_pool_wins_when_belongs_to_channel(monkeypatch):
    """场景池成员属于本通道已配置账号时，优先用场景池（保留用户选择）。"""
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: [
            {"name": "from_cred", "configured": True},
            {"name": "scene_a", "configured": True},
            {"name": "scene_b", "configured": True},
        ],
    )
    ids, err = shared_pool_account_ids(2, ["scene_a", "scene_b"])
    assert (ids, err) == (["scene_a", "scene_b"], "")


def test_ids_ignores_scene_pool_from_other_channel(monkeypatch):
    """2026-10-29 实测缺口：池里存的是别的通道账号名（gate_botN），

    切到 okx_cex 后会写进台账（交易所不存在该账号）。现规则 = 与本通道
    已配置账号取交集，全不匹配则回退通道账号列表。
    """
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: [
            {"name": "DreamShepherdbot1", "configured": True},
            {"name": "DreamShepherdbot2", "configured": True},
        ],
    )
    ids, err = shared_pool_account_ids(2, ["gate_bot1", "gate_bot2"])
    assert err == ""
    assert ids == ["DreamShepherdbot1", "DreamShepherdbot2"]


def test_ids_fail_closed_when_channel_accounts_unreadable(monkeypatch):
    """通道账号读不出来时不能沿用别的通道的账号名（fail-closed）。"""

    def boom():
        raise RuntimeError("cred file missing")

    monkeypatch.setattr("nanobot_quant.okx_cex_credentials.list_sub_accounts", boom)
    ids, err = shared_pool_account_ids(2, ["gate_bot1"])
    assert ids == []
    assert "无法校验场景池账号" in err


def test_ids_fail_closed_without_any_account(monkeypatch):
    monkeypatch.setattr("nanobot_quant.okx_cex_credentials.list_sub_accounts", lambda: [])
    ids, err = shared_pool_account_ids(3)
    assert ids == []
    assert "子账号" in err


def test_ids_strip_blank_preferred_entries(monkeypatch):
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: [{"name": "okx_a", "configured": True}],
    )
    ids, err = shared_pool_account_ids(2, ["", "  ", "okx_a"])
    assert (ids, err) == (["okx_a", "okx_a"], "")


def test_dummy_data_source_exposes_datetime():
    """StrategyExecutor 每轮调 strategy.get_datetime()（实测崩溃点）。"""
    from datetime import datetime

    from nanobot_quant.brokers._dummy_data_source import DummyDataSource

    ds = DummyDataSource()
    now = ds.get_datetime(adjust_for_delay=True)
    assert isinstance(now, datetime)
    assert ds.get_chains() == {}


# ── 余额单轮缓存 ─────────────────────────────────────────────────────────


def test_shared_pool_balances_queried_once_per_account():
    """同账号 2 个 slot、连续 3 次取余额 → broker 只被调用 1 次。"""
    st = _strategy({"channel_family": "cex", "slot_model": "shared_pool"})
    fake = _FakeBroker({"USDT": {"available": 100.0, "locked": 0.0}})
    st._cex_slot_broker = lambda slot: fake

    slot1 = {"slot": 1, "account_id": "okx_a"}
    slot2 = {"slot": 2, "account_id": "okx_a"}

    b1 = st._cex_slot_balances(slot1, use_snapshot=True)
    b2 = st._cex_slot_balances(slot2, use_snapshot=True)
    b3 = st._cex_slot_balances(slot1, use_snapshot=True)

    assert fake.calls == 1
    assert b1 is b2 is b3
    assert b1["USDT"]["available"] == 100.0


def test_shared_pool_balances_cached_per_distinct_account():
    """不同账号各查一次（a 两次、b 一次 → 共 2 次）。"""
    st = _strategy({"channel_family": "cex", "slot_model": "shared_pool"})
    calls = {"acct": []}

    class _Broker:
        def __init__(self, acct):
            self.acct = acct

        def _balances(self):
            calls["acct"].append(self.acct)
            return {"USDT": {"available": 1.0 if self.acct == "okx_a" else 2.0}}

    st._cex_slot_broker = lambda slot: _Broker(str(slot["account_id"]))

    a = {"slot": 1, "account_id": "okx_a"}
    b = {"slot": 2, "account_id": "okx_b"}
    st._cex_slot_balances(a, use_snapshot=True)
    st._cex_slot_balances(a, use_snapshot=True)
    st._cex_slot_balances(b, use_snapshot=True)

    assert calls["acct"] == ["okx_a", "okx_b"]


def test_shared_pool_balance_error_cached_as_empty_no_retry():
    """查询失败缓存 {}（本轮不重试）→ 调用方 fail-closed 资金 0。"""
    st = _strategy({"channel_family": "cex", "slot_model": "shared_pool"})
    fake = _FakeBroker(error=RuntimeError("network down"))
    st._cex_slot_broker = lambda slot: fake
    st.logger = type("L", (), {"warning": staticmethod(lambda *a, **k: None)})()

    slot = {"slot": 1, "account_id": "okx_a"}
    assert st._cex_slot_balances(slot, use_snapshot=True) == {}
    assert st._cex_slot_balances(slot, use_snapshot=True) == {}
    assert fake.calls == 1  # 失败结果也被缓存，不重试


def test_shared_pool_sell_path_bypasses_cache():
    """SELL/估值路径（use_snapshot=False）仍逐查——安全关键路径不赌缓存。"""
    st = _strategy({"channel_family": "cex", "slot_model": "shared_pool"})
    fake = _FakeBroker({"USDT": {"available": 5.0}})
    st._cex_slot_broker = lambda slot: fake
    slot = {"slot": 1, "account_id": "okx_a"}

    st._cex_slot_balances(slot, use_snapshot=True)   # 1 次（进缓存）
    st._cex_slot_balances(slot, use_snapshot=False)  # 逐查 +1
    st._cex_slot_balances(slot, use_snapshot=False)  # 逐查 +1
    assert fake.calls == 3


def test_gate_per_account_channel_unaffected():
    """per_account（gate）不走 shared_pool 缓存：每次各查（旧行为）。"""
    st = _strategy({"channel_family": "cex", "slot_model": "per_account"})
    fake = _FakeBroker({"USDT": {"available": 3.0}})
    st._cex_slot_broker = lambda slot: fake
    st._balances_prefetched = False

    slot = {"slot": 1, "account_id": "gate_bot1"}
    st._cex_slot_balances(slot, use_snapshot=True)
    st._cex_slot_balances(slot, use_snapshot=True)
    assert fake.calls == 2
    assert st._sp_balances == {}


def test_activate_scene_clears_shared_pool_cache():
    """每轮场景激活清空缓存（跨轮不复用旧余额）。"""
    st = _strategy({"channel_family": "cex", "slot_model": "shared_pool"})
    st._sp_balances = {"okx_a": {"USDT": {"available": 1.0}}}
    st._round_buy_used = {}
    st._last_setup = {}
    st._wave_tried = {}
    st._td_params = {"entry_setup": 9}

    # 只验证清理动作（_activate_scene 其余分支需要完整运行时，这里直接调用
    # 其开头两行等价逻辑不安全，故用最小 rt 走真函数并捕获后续异常）。
    rt = {"params": {}, "enabled": True}
    try:
        st._activate_scene("high", rt)
    except Exception:  # noqa: BLE001
        pass
    assert st._sp_balances == {}


# ── 启动对账先于 initialize()（2026-10-29 实测崩溃点） ──────────────────


def test_cex_slot_broker_before_initialize_uses_lazy_cache(monkeypatch):
    """对账在 ``initialize()`` 之前调用 ``_cex_slot_broker``。

    ``_cex_brokers`` 原先只在 ``initialize()`` 里建立，对账阶段调用直接抛
    ``AttributeError: 'TdSequentialStrategy' object has no attribute
    '_cex_brokers'``（shared_pool 对账全部失败、且台账账号名随之错乱）。
    """
    import nanobot_quant.brokers.okx_cex_broker as okxb
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    monkeypatch.setattr(okxb.OkxCexBroker, "__init__", lambda self, **kw: None)
    st = TdSequentialStrategy.__new__(TdSequentialStrategy)
    st.parameters = {"slot_model": "shared_pool", "tokens_json": []}

    broker = st._cex_slot_broker({"slot": 1, "account_id": "acc1"})
    assert broker is not None
    assert "1:acc1" in st.__dict__["_cex_brokers"]
    # 同 slot 再取 → 命中缓存（同一对象），不重复构造
    assert st._cex_slot_broker({"slot": 1, "account_id": "acc1"}) is broker


# ── 账号分工：期权用第一个子账号，现货用剩下的（2026-10-29 用户确认） ──


def test_ids_exclude_option_account_from_spot_pool(monkeypatch):
    """期权账号（含保证金）不得混进现货池。"""
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.option_account_name", lambda: "DreamShepherdbot1"
    )
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: [
            {"name": n, "configured": True}
            for n in ("DreamShepherdbot1", "DreamShepherdbot2", "DreamShepherdbot3")
        ],
    )
    ids, err = shared_pool_account_ids(3, [])
    assert err == ""
    assert ids == ["DreamShepherdbot2", "DreamShepherdbot3", "DreamShepherdbot2"]


def test_ids_fail_closed_when_only_option_account_configured(monkeypatch):
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.option_account_name", lambda: "DreamShepherdbot1"
    )
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: [{"name": "DreamShepherdbot1", "configured": True}],
    )
    ids, err = shared_pool_account_ids(3, [])
    assert ids == []
    assert "非期权" in err
