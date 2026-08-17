"""Broker registry tests (docs/quant-system.md §19, 2026-08-17).

Step 0: 执行通道大类分层——CHANNEL_BROKER 唯一映射 + fail-closed。
"""

import pytest

from nanobot_quant.brokers.registry import (
    CHANNEL_BROKER,
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


def test_channel_map():
    assert CHANNEL_BROKER == {"dex": "okx_dex", "cex": "gate"}
    # 与数据源注册表通道 key 一致（结构性同源约束）
    from nanobot_quant.data_sources import CHANNEL_DATA_SOURCE
    assert set(CHANNEL_BROKER) == set(CHANNEL_DATA_SOURCE)


def test_spec_for_channel():
    assert spec_for_channel("cex").name == "gate"
    assert spec_for_channel("dex").name == "okx_dex"


def test_broker_for_channel_gate():
    b = broker_for_channel("cex", tokens_json=[], slippage="0.01")
    assert b.__class__.__name__ == "CexBroker"
    assert b._uid == "1"


def test_broker_for_channel_okx_dex():
    b = broker_for_channel(
        "dex", tokens_json=[], slippage="0.01", sol_buffer_pct=0.0
    )
    assert b.__class__.__name__ == "OnchainOSBroker"


def test_broker_for_channel_gate_ignores_dex_only_kw():
    # gate spec 不消费 sol_buffer_pct（**kw 吸收），不应报错
    b = broker_for_channel(
        "cex", tokens_json=[], slippage="0.01", sol_buffer_pct=0.05
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


def test_registry_matches_channel_binding():
    # 通道映射的每个 broker 都存在且 family 与通道一致
    for channel, name in CHANNEL_BROKER.items():
        spec = REGISTRY[name]
        assert spec.family == channel
