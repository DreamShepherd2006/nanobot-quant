"""OKX CEX P3-1：shared_pool 批次账号模型（§33.52）。

本批为**纯内部改造，零行为变化**：`okx_cex` 仍不在 `EXECUTION_CHANNELS`，
`gate` / `okx_dex` 两条通道的路径一行未动。只把「账号标识 / 余额 / 取价」
按 `BrokerSpec.slot_model` 分派出来，为 P3-2 开放通道铺路。

覆盖：
- `shared_pool_account_ids`：场景池首项 / 已配置子账号兜底 / 无账号 fail-closed
- `ensure_batches`：shared_pool 通道生成「同一账号 × N 个逻辑 slot」
- 策略层：`slot_model` 缺省回退 `per_account`（gate 零变化）、shared_pool 跳过
  Gate 子账号余额快照、余额键按 `okx_symbol` 基础币查找、取价优先同所
- 契约守卫：`okx_cex` 尚未进可选通道（P3-2 删此用例）
"""

from __future__ import annotations

import pytest

from nanobot_quant import batches as batches_mod
from nanobot_quant.batches import BatchManager, shared_pool_account_ids
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _spec(name: str, family: str = "cex", slot_model: str = "shared_pool"):
    return type("Spec", (), {"name": name, "family": family, "slot_model": slot_model})()


def _strategy(params: dict):
    st = TdSequentialStrategy.__new__(TdSequentialStrategy)
    st.parameters = params
    st._cex_brokers = {}
    st._slot_balances = {}
    st._name_to_uid = {}
    st._balances_error = None
    st._price_cache = {}
    st._PRICE_CACHE_TTL = 15
    st.symbols = []
    return st


# ── shared_pool_account_ids ───────────────────────────────────────────────


def test_shared_pool_ids_prefer_scene_pool(monkeypatch):
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: [{"name": "okx_main", "configured": True}],
    )
    ids, err = shared_pool_account_ids(3, ["okx_main"])
    assert err == ""
    assert ids == ["okx_main"] * 3


def test_shared_pool_ids_fall_back_to_configured_subaccount(monkeypatch):
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: [
            {"name": "未配置", "configured": False},
            {"name": "DreamShepherdbot1", "configured": True},
        ],
    )
    ids, err = shared_pool_account_ids(2)
    assert (ids, err) == (["DreamShepherdbot1"] * 2, "")


def test_shared_pool_ids_fail_closed_without_account(monkeypatch):
    monkeypatch.setattr("nanobot_quant.okx_cex_credentials.list_sub_accounts", lambda: [])
    ids, err = shared_pool_account_ids(2)
    assert ids == []
    assert "子账号" in err


def test_shared_pool_ids_fail_closed_when_credentials_unreadable(monkeypatch):
    def boom():
        raise RuntimeError("disk io")

    monkeypatch.setattr("nanobot_quant.okx_cex_credentials.list_sub_accounts", boom)
    ids, err = shared_pool_account_ids(2)
    assert ids == []
    assert "读取子账号配置失败" in err


# ── ensure_batches（shared_pool 分支） ────────────────────────────────────


def test_ensure_batches_shared_pool_reuses_single_account(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "nanobot_quant.brokers.registry.spec_for_channel", lambda name: _spec(name)
    )
    monkeypatch.setattr(batches_mod, "_load_or_migrate", lambda *a, **k: None)
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: [{"name": "DreamShepherdbot1", "configured": True}],
    )
    monkeypatch.setattr(
        batches_mod, "batches_path", lambda *a, **k: tmp_path / "b.json"
    )

    bm, msg = batches_mod.ensure_batches(3, "SOL", channel="okx_cex")

    assert isinstance(bm, BatchManager)
    assert [s["account_id"] for s in bm.slots] == ["DreamShepherdbot1"] * 3
    assert "3 slots" in msg or "SOL" in msg


def test_ensure_batches_shared_pool_fail_closed_without_account(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "nanobot_quant.brokers.registry.spec_for_channel", lambda name: _spec(name)
    )
    monkeypatch.setattr(batches_mod, "_load_or_migrate", lambda *a, **k: None)
    monkeypatch.setattr("nanobot_quant.okx_cex_credentials.list_sub_accounts", lambda: [])
    monkeypatch.setattr(
        batches_mod, "batches_path", lambda *a, **k: tmp_path / "b.json"
    )

    bm, msg = batches_mod.ensure_batches(2, "SOL", channel="okx_cex")

    assert bm is None
    assert "批次初始化失败" in msg


# ── 策略层分派 ────────────────────────────────────────────────────────────


def test_slot_model_defaults_to_per_account():
    """缺省必须回退 per_account —— gate 通道零行为变化。"""
    st = _strategy({})
    assert st._slot_model == "per_account"
    assert st._shared_pool_channel is False


def test_slot_model_reads_parameters():
    st = _strategy({"slot_model": "shared_pool"})
    assert st._shared_pool_channel is True


def test_prefetch_balances_skips_gate_snapshot_for_shared_pool(monkeypatch):
    """shared_pool 不做 Gate 子账号快照（余额归 broker 单账号查询）。"""
    st = _strategy({"slot_model": "shared_pool"})
    st._slot_balances = {"stale": {"SOL": {}}}
    st._name_to_uid = {"stale": "1"}

    called = {"gate": 0}

    def boom(*a, **k):
        called["gate"] += 1
        raise AssertionError("shared_pool 不应调用 Gate 子账号快照")

    monkeypatch.setattr(
        "nanobot_quant.gate_credentials.fetch_all_balances", boom, raising=False
    )

    st._prefetch_slot_balances()

    assert called["gate"] == 0
    assert st._slot_balances == {}
    assert st._name_to_uid == {}
    assert st._balances_error is None


def test_slot_balances_ignores_snapshot_for_shared_pool(monkeypatch):
    """use_snapshot=True 在 shared_pool 下被绕过（走 broker 逐查）。"""
    st = _strategy({"slot_model": "shared_pool"})
    slot = {"slot": 1, "account_id": "DreamShepherdbot1"}
    st._slot_balances = {"uid-1": {"SOL": {"available": 999.0}}}
    st._name_to_uid = {"DreamShepherdbot1": "uid-1"}

    class _FakeBroker:
        def _balances(self):
            return {"SOL": {"available": 1.5, "locked": 0.0}}

    monkeypatch.setattr(st, "_cex_slot_broker", lambda s: _FakeBroker())

    assert st._cex_slot_balances(slot, use_snapshot=True) == {
        "SOL": {"available": 1.5, "locked": 0.0}
    }


def test_slot_balances_still_uses_snapshot_for_per_account():
    """per_account（gate）保持原行为：快照命中即返回，不查 broker。"""
    st = _strategy({"slot_model": "per_account"})
    slot = {"slot": 1, "account_id": "gate_bot1"}
    st._slot_balances = {"uid-1": {"USDT": {"available": 4.0}}}
    st._name_to_uid = {"gate_bot1": "uid-1"}

    assert st._cex_slot_balances(slot, use_snapshot=True) == {
        "USDT": {"available": 4.0}
    }


def test_token_balance_uses_okx_base_ccy_for_shared_pool(monkeypatch):
    """余额键 = OKX 账户币种（XCRCL），不是 tokens.json 的 symbol（CRCLX）。"""
    st = _strategy(
        {
            "slot_model": "shared_pool",
            "tokens_json": [{"symbol": "CRCLX", "okx_symbol": "XCRCL-USDT"}],
        }
    )
    slot = {"slot": 1, "account_id": "DreamShepherdbot1"}
    monkeypatch.setattr(
        st,
        "_cex_slot_balances",
        lambda s, use_snapshot=False: {
            "XCRCL": {"available": 0.02, "locked": 0.0},
        },
    )

    assert st._cex_slot_token_balance(slot, "CRCLX") == pytest.approx(0.02)


def test_token_balance_falls_back_to_symbol_for_shared_pool(monkeypatch):
    st = _strategy({"slot_model": "shared_pool", "tokens_json": []})
    slot = {"slot": 1, "account_id": "DreamShepherdbot1"}
    monkeypatch.setattr(
        st,
        "_cex_slot_balances",
        lambda s, use_snapshot=False: {"SOL": {"available": 0.5, "locked": 0.0}},
    )

    assert st._cex_slot_token_balance(slot, "SOL") == pytest.approx(0.5)


def test_price_prefers_okx_source_for_shared_pool(monkeypatch):
    import nanobot_quant.data_sources as ds_mod

    seen: list[str] = []

    class _Src:
        def __init__(self, name, px):
            self.name, self.px = name, px

        def get_price(self, ccy):
            seen.append(self.name)
            return self.px

    def fake_get(name):
        return _Src(name, 7.0 if name == "okx_cex" else 0.0)

    monkeypatch.setattr(ds_mod, "get_data_source", fake_get)

    st = _strategy({"slot_model": "shared_pool"})
    assert st._cex_price_of("SOL") == pytest.approx(7.0)
    assert seen[0] == "okx_cex"


def test_price_prefers_gate_source_for_per_account(monkeypatch):
    import nanobot_quant.data_sources as ds_mod

    seen: list[str] = []

    class _Src:
        def __init__(self, name, px):
            self.name, self.px = name, px

        def get_price(self, ccy):
            seen.append(self.name)
            return self.px

    monkeypatch.setattr(
        ds_mod, "get_data_source", lambda name: _Src(name, 11.0 if name == "gate_cex" else 0.0)
    )

    st = _strategy({})
    assert st._cex_price_of("LINK") == pytest.approx(11.0)
    assert seen[0] == "gate_cex"


# ── 契约守卫：P3-1 不开放通道 ─────────────────────────────────────────────


def test_okx_cex_executable_after_p3_3():
    """P3-1 期曾锁定「通道不可选」；P3-3（2026-10-28 用户拍板）已开放。

    ``slot_model=shared_pool`` 不变：批次仍是逻辑槽位、同账号内共享余额池。
    """
    from nanobot_quant.brokers.registry import spec_for_channel
    from nanobot_quant.exec_params import EXECUTION_CHANNELS

    assert "okx_cex" in EXECUTION_CHANNELS
    assert spec_for_channel("okx_cex").slot_model == "shared_pool"


# ── 日志降频（2026-10-30 用户反馈：每次调用都打，实盘每轮必刷屏）──────────


def test_pool_exclusion_logged_once_until_pool_changes(monkeypatch, capsys):
    """「期权账号已从现货池排除」只在**池成员变化**时打一次。

    用户实测：1m 循环 × 4 标的每轮调一次、页面 10s 轮询又调一次 ⇒ 每分钟
    几十条纯噪音。可见性保留（首轮必打、改凭证后重打），但不刷屏。
    """
    monkeypatch.setattr(batches_mod, "_SPOT_POOL_LOGGED", None, raising=False)

    def _accounts(names):
        return [{"name": n, "configured": True} for n in names]

    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: _accounts(["DreamShepherdbot1", "DreamShepherdbot2"]),
    )
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.option_account_name",
        lambda: "DreamShepherdbot1",
    )

    for _ in range(5):
        shared_pool_account_ids(3)
    first = capsys.readouterr().err
    assert first.count("已从现货池排除") == 1

    # 池成员变化（新增一个现货账号）→ 应重打一次
    monkeypatch.setattr(
        "nanobot_quant.okx_cex_credentials.list_sub_accounts",
        lambda: _accounts(["DreamShepherdbot1", "DreamShepherdbot2", "DreamShepherdbot3"]),
    )
    shared_pool_account_ids(3)
    assert capsys.readouterr().err.count("已从现货池排除") == 1
