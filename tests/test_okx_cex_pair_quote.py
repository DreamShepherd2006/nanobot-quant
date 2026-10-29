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


# ── 下划线（Gate 口径）okx_symbol 必须自愈（2026-10-29 实盘 SOL 失效）──


def test_inst_id_for_normalizes_underscore(catalog):
    catalog({"SOL-USDT"})
    assert data.inst_id_for("SOL_USDT") == "SOL-USDT", "Gate 口径 SOL_USDT 应归一化"
    assert data.inst_id_for("SOL_usdt") == "SOL-USDT"


def test_inst_id_for_strips_quote_suffix(catalog):
    catalog({"SOL-USDT", "SOL-USDC"})
    assert data.inst_id_for("SOL-USDC", strict=True) == "SOL-USDC"
    catalog({"SOL-USDT"})
    # 计价币拼错/该档不存在：回退同 base 的其它计价对（韧性），但绝不凭空造对
    assert data.inst_id_for("SOL-XXX", strict=True) == "SOL-USDT"
    catalog({"SOL-USDT"})
    assert data.inst_id_for("NOTACOIN", strict=True) == "", "清单里没有该 base → 空（不猜）"


def test_data_source_path_uses_normalized_instid(catalog, monkeypatch):
    """数据源链路：okx_ticker 返回下划线格式时，底层 fetch 仍能解析出 instId。"""
    catalog({"SOL-USDT"})
    seen = {}

    def _fake_candles(inst_id, **kw):
        seen["inst"] = inst_id
        return {"code": "0", "data": []}

    monkeypatch.setattr(data, "spot_inst_ids", lambda session=None: frozenset({"SOL-USDT"}))
    assert data.inst_id_for("SOL_USDT") == "SOL-USDT"


def test_cash_diag_prints_even_when_empty(monkeypatch, capsys):
    """交易账户现金为 0 时必须留下痕迹（不能静默无输出）。"""
    b = _bare_broker()
    monkeypatch.setattr(OkxCexBroker, "_spot_names", lambda self: [])
    b._log_cash_once({"SOL": {"available": 0.0, "total": 1.5}})
    err = capsys.readouterr().err
    assert "现货池现金" in err and "无可交易现金" in err
    assert "资金账户" in err


def test_cash_diag_reports_available(monkeypatch, capsys):
    b = _bare_broker()
    monkeypatch.setattr(OkxCexBroker, "_spot_names", lambda self: [])
    b._log_cash_once({"USDC": {"available": 12.5, "total": 12.5}})
    err = capsys.readouterr().err
    assert "现货池现金 | {'USDC': 12.5}" in err


# ── 2026-10-29 实盘日志暴露的三处（对账 min_hold / 计价币 / 估值）──


def test_min_hold_safe_before_initialize():
    """对账发生在 initialize() 之前：_min_hold_* 不能 AttributeError。"""
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s.parameters = {}
    s._note_min_hold("SOL", {"slot": 1}, None)          # 不抛
    assert isinstance(s.__dict__.get("_min_hold_until", {}), dict)
    assert TdSequentialStrategy._min_hold_bars == 0      # 类属性兜底


def test_cex_quote_ccy_follows_broker():
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s.parameters = {"quote_ccy": "USDT"}
    s._cex_brokers = {("1", "DreamShepherdbot2"): type("B", (), {"_quote": "USDC"})()}
    assert s._cex_quote_ccy() == "USDC", "BUY 资金校验必须按实际计价币，不能硬编码 USDT"

    s._cex_brokers = {}
    assert s._cex_quote_ccy() == "USDT"                  # 回退执行参数


def test_spot_ids_stale_while_error(monkeypatch):
    """清单拉取失败时用旧清单顶着，并在退避期内不再重试。"""
    class _BoomMod:
        def __init__(self):
            self.n = 0

        def get(self, *a, **k):
            self.n += 1
            raise RuntimeError("network down")

    boom = _BoomMod()
    monkeypatch.setattr(data, "requests", boom)
    monkeypatch.setattr(data, "_spot_ids_cache", (0.0, frozenset({"SOL-USDT"})))
    monkeypatch.setattr(data, "_spot_ids_fail_until", 0.0)

    out = data.spot_inst_ids()
    assert out == frozenset({"SOL-USDT"}), "失败时应返回旧清单（stale-while-error）"
    assert boom.n == 1

    out2 = data.spot_inst_ids()
    assert out2 == frozenset({"SOL-USDT"})
    assert boom.n == 1, "退避期内不应再次发起请求"
