"""Broker registry tests (docs/quant-system.md §19, 2026-08-17).

Step 0: 执行通道大类分层——通道值=broker spec 实例名 + fail-closed（方案 C，2026-08-17）。
"""

import pytest

from nanobot_quant.brokers.registry import (
    REGISTRY,
    broker_for_channel,
    get_broker,
    list_brokers,
    spec_for_channel,
)


@pytest.fixture(autouse=True)
def _gate_creds(monkeypatch):
    """CexBroker 构造需要 gate 凭证——单测注入假 main key。"""
    def fake_load():
        return {"main": {"api_key": "k-test", "api_secret": "s-test", "uid": "1"}}
    monkeypatch.setattr(
        "nanobot_quant.gate_credentials.load_gate_credentials", fake_load
    )


def test_registry_contains_executables():
    names = list_brokers()
    assert "okx_dex" in names
    assert "gate" in names

    g = get_broker("gate")
    assert g.family == "cex"
    assert g.quote == "USDT"
    assert g.exchange == "gate"
    assert g.data_source == "gate_cex"

    o = get_broker("okx_dex")
    assert o.family == "dex"
    assert o.quote == "USDC"
    assert o.exchange == "okx"
    assert o.data_source == "onchainos"


def test_channel_map_aligns_with_enum():
    """方案 C：通道=broker 实例名，EXECUTION_CHANNELS 与注册表 spec 一一对应。"""
    from nanobot_quant.exec_params import EXECUTION_CHANNELS
    assert set(EXECUTION_CHANNELS) == set(REGISTRY)
    assert "okx_dex" in EXECUTION_CHANNELS
    assert "gate" in EXECUTION_CHANNELS


def test_spec_for_channel():
    assert spec_for_channel("gate").name == "gate"
    assert spec_for_channel("okx_dex").name == "okx_dex"


def test_spec_for_channel_legacy_values_normalized():
    """旧大类值 cex/dex 迁移期兼容（normalize_execution_channel）。"""
    assert spec_for_channel("cex").name == "gate"
    assert spec_for_channel("dex").name == "okx_dex"


def test_spec_family_semantics():
    """通道家族（family）语义：gate→cex、okx_dex→dex（2026-08-17 D 修复契约）。

    策略/对账层用 family 分叉（_is_cex / _reconcile_all），不可用实例名——
    gate≠cex 曾导致 CEX 对账跳过判断失效、DEX 对账在 cex 通道误跑。
    """
    assert spec_for_channel("gate").family == "cex"
    assert spec_for_channel("okx_dex").family == "dex"
    # 旧大类值归一化后 family 语义不变
    assert spec_for_channel("cex").family == "cex"
    assert spec_for_channel("dex").family == "dex"


def test_broker_for_channel_gate():
    b = broker_for_channel("gate", tokens_json=[], slippage="0.01")
    assert b.__class__.__name__ == "CexBroker"
    assert b._uid == "1"


def test_broker_for_channel_okx_dex():
    b = broker_for_channel(
        "okx_dex", tokens_json=[], slippage="0.01", sol_buffer_pct=0.0
    )
    assert b.__class__.__name__ == "OnchainOSBroker"


def test_broker_for_channel_gate_ignores_dex_only_kw():
    # gate spec 不消费 sol_buffer_pct（**kw 吸收），不应报错
    b = broker_for_channel(
        "gate", tokens_json=[], slippage="0.01", sol_buffer_pct=0.05
    )
    assert b.__class__.__name__ == "CexBroker"


def test_unknown_channel_fail_closed():
    with pytest.raises(KeyError):
        spec_for_channel("unknown")
    with pytest.raises(KeyError):
        broker_for_channel("unknown")


def test_unknown_broker_fail_closed():
    with pytest.raises(KeyError):
        get_broker("nope")


def test_registry_matches_execution_channels():
    # 执行通道（实例名）与注册表一一对应，且 family 与分组语义一致
    from nanobot_quant.exec_params import EXECUTION_CHANNELS
    assert set(EXECUTION_CHANNELS) == set(REGISTRY)
    assert REGISTRY["gate"].family == "cex"
    assert REGISTRY["okx_dex"].family == "dex"
