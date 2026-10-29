"""instId 计价币自适应（2026-10-29）。

OKX 现货同一 base 常并存 ``XXX-USDT`` 与 ``XXX-USDC``（用户实测 SOL/ETH 两组
都在）。因此：① 解析 instId 时按给定 quote → USDC 兜底 → X 前缀兜底；
② broker 的现金校验必须用**该标的实际计价币**，不能硬编码 USDT。
"""

import pytest

import nanobot_quant.okx_cex_data as data
from nanobot_quant.brokers import okx_cex_broker as broker_mod
from nanobot_quant.brokers.okx_cex_broker import OkxCexBroker


@pytest.fixture()
def catalog(monkeypatch):
    def _set(ids):
        frozen = frozenset(ids)
        monkeypatch.setattr(data, "spot_inst_ids", lambda session=None: frozen)
        monkeypatch.setattr(broker_mod, "spot_inst_ids", lambda session=None: frozen)
        return frozen

    return _set


def test_resolve_prefers_requested_quote(catalog):
    catalog({"SOL-USDT", "SOL-USDC"})
    assert data.resolve_inst_id("SOL") == "SOL-USDT"


def test_resolve_falls_back_to_usdc(catalog):
    catalog({"BTC-USDC", "ETH-USDT", "ETH-USDC"})   # 该 base 没有 -USDT 时用 -USDC
    assert data.resolve_inst_id("BTC") == "BTC-USDC"
    assert data.resolve_inst_id("ETH") == "ETH-USDT"


def _bare_broker(quote="USDT"):
    b = OkxCexBroker.__new__(OkxCexBroker)
    b._quote = quote
    b._tokens_json = []
    return b


def test_broker_quote_of_follows_resolved_pair(catalog):
    catalog({"BTC-USDC"})
    b = _bare_broker()
    assert b.inst_id_of("BTC") == "BTC-USDC"
    assert b.quote_of("BTC") == "USDC", "计价币必须跟实际对走，不能硬编码 USDT"


def test_broker_quote_of_usdt_pair(catalog):
    catalog({"SOL-USDT", "SOL-USDC"})
    b = _bare_broker()
    assert b.quote_of("SOL") == "USDT"


def test_strict_resolve_returns_empty_when_absent(catalog):
    catalog({"SOL-USDT"})
    assert data.resolve_inst_id("XSOL", strict=True) == ""
    assert data.resolve_inst_id("SOL", strict=True) == "SOL-USDT"


def test_broker_falls_back_to_raw_symbol_on_bad_mapping(catalog, monkeypatch):
    """tokens.json 把 SOL 错登记成 XSOL：仍应解析到 SOL-USDT（不被打死）。"""
    catalog({"SOL-USDT"})
    b = _bare_broker()
    monkeypatch.setattr(b, "_base_ticker", lambda s: "XSOL")
    assert b.inst_id_of("SOL") == "SOL-USDT"


# ── 计价币由 WebUI 参数决定（quote_ccy，2026-10-29）────────────────


def test_quote_ccy_param_meta_and_validation():
    from nanobot_quant import exec_params as ep

    meta = ep.PARAM_META["quote_ccy"]
    assert meta["type"] == "enum" and meta["enum"] == ["USDT", "USDC"]
    assert meta["channels"] == "okx_cex", "只在 OKX CEX 通道下显示（Gate 固定 USDT）"
    assert ep.DEFAULT_EXEC_PARAMS["quote_ccy"] == "USDT"
    assert ep.validate_exec_param("quote_ccy", "USDC") is None
    assert ep.validate_exec_param("quote_ccy", "USDX") is not None


def test_broker_default_quote_reads_webui_param(monkeypatch):
    from nanobot_quant import exec_params as ep

    monkeypatch.setattr(ep, "load_exec_params", lambda: {"quote_ccy": "USDC"})
    assert broker_mod._default_quote_ccy() == "USDC"

    monkeypatch.setattr(ep, "load_exec_params", lambda: {"quote_ccy": "usdc"})
    assert broker_mod._default_quote_ccy() == "USDC"      # 大小写归一

    monkeypatch.setattr(ep, "load_exec_params", lambda: {"quote_ccy": "BOGUS"})
    assert broker_mod._default_quote_ccy() == "USDT"      # 非法回退


def test_broker_explicit_quote_wins(monkeypatch):
    monkeypatch.setattr(broker_mod, "account_creds",
                        lambda name: {"api_key": "k", "secret_key": "s", "passphrase": "p"})
    monkeypatch.setattr(broker_mod, "_default_quote_ccy", lambda: "USDC")
    b = OkxCexBroker(quote="USDT")
    assert b._quote == "USDT", "显式传参优先于 WebUI 参数（测试/回测可覆盖）"
