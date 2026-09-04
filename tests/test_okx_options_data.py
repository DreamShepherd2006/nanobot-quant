"""okx_options_data 单元测试（mock okx_sdk.public/market 层，不触网）。

期权链数据来自官方 python-okx SDK；本测试验证解析/分组/配对/过滤/口径，
HTTP 与 SDK 请求由 fake 层替代。
"""

import pytest

from nanobot_quant import okx_options_data as od

# 到期时间基于真实时钟派生（过去/未来判定与生产一致）
import time

_NOW = int(time.time() * 1000)
_D1 = _NOW + 86_400_000
_D2 = _NOW + 2 * 86_400_000
_D3 = _NOW + 3 * 86_400_000
_PAST = _NOW - 86_400_000


def _inst(strike: int, typ: str, exp: int, family="BTC-USD_UM"):
    return {
        "instId": f"{family}-{exp}-{strike}-{typ}",
        "instFamily": family, "state": "live", "optType": typ,
        "ctType": "linear", "stk": str(strike), "expTime": str(exp),
        "ctVal": "1", "ctMult": "0.01", "uly": "BTC-USD",
    }


def _inst_set(family="BTC-USD_UM"):
    insts = []
    for exp in (_D1, _D2, _D3):
        for st in (60000, 65000, 70000, 75000, 80000, 85000):
            insts.append(_inst(st, "C", exp, family))
            insts.append(_inst(st, "P", exp, family))
    insts.append(_inst(65000, "C", _PAST, family))   # 已到期
    insts.append(_inst(65000, "P", _PAST, family))
    return insts


def _tickers_dict():
    out = {}
    for exp in (_D1, _D2, _D3):
        for typ in ("C", "P"):
            iid = f"BTC-USD_UM-{exp}-80000-{typ}"
            out[iid] = {"instId": iid,
                        "bidPx": "1000" if typ == "C" else "364.5",
                        "askPx": "1001" if typ == "C" else "372.6"}
    return out


def _summary_dict():
    out = {}
    for exp in (_D1, _D2, _D3):
        for st in (60000, 65000, 70000, 75000, 80000, 85000):
            for typ in ("C", "P"):
                iid = f"BTC-USD_UM-{exp}-{st}-{typ}"
                d = "-0.9" if typ == "C" else "-0.1"
                out[iid] = {"instId": iid, "markVol": "0.5", "delta": d}
    return out


class _FakePublic:
    def get_instruments(self, instType="OPTION", instFamily="BTC-USD_UM", **kw):
        return {"code": "0", "data": _inst_set(instFamily), "msg": ""}

    def get_opt_summary(self, instFamily="BTC-USD_UM", **kw):
        return {"code": "0", "data": list(_summary_dict().values()), "msg": ""}


class _FakeMarket:
    def get_ticker(self, instId="", **kw):
        return {"code": "0", "data": [{"instId": instId, "last": "81000"}], "msg": ""}

    def get_tickers(self, instType="OPTION", **kw):
        return {"code": "0", "data": list(_tickers_dict().values()), "msg": ""}

    def get_index_tickers(self, instId="", **kw):
        return {"code": "0", "data": [{"instId": instId, "idxPx": "4473.25"}], "msg": ""}

    def get_history_candles(self, instId="", **kw):
        # 收盘 100 → 110 → 100 → 110 → 121（返回按 ts 降序）
        rows = []
        ts = _NOW
        for c in (121, 110, 100, 110, 100):
            rows.append([str(ts), "", "", "", str(c), "", "", ""])
            ts -= 86_400_000
        return {"code": "0", "data": rows, "msg": ""}


@pytest.fixture
def fake_sdk(monkeypatch):
    monkeypatch.setattr(od.okx_sdk, "public", lambda: _FakePublic())
    monkeypatch.setattr(od.okx_sdk, "market", lambda: _FakeMarket())
    yield


@pytest.fixture(autouse=True)
def _clear_cache():
    od._cache.clear()
    yield
    od._cache.clear()


# ── tests ───────────────────────────────────────────────────────

def test_list_expiries_filters_past(fake_sdk):
    exps = od.list_expiries("BTC-USD_UM")
    assert [e["exp_ms"] for e in exps] == [_D1, _D2, _D3]
    assert exps[0]["days"] == 1


def test_list_expiries_rejects_unknown_family(fake_sdk):
    with pytest.raises(od.OkxSdkError, match="未知标的"):
        od.list_expiries("XRP-USD")


def test_fetch_chain_default_near_three_groups(fake_sdk):
    chain = od.fetch_chain("BTC-USD_UM")
    assert chain["spot"] == 81000.0
    assert chain["lot_coin"] == 0.01
    assert [g["exp_ms"] for g in chain["groups"]] == [_D1, _D2, _D3]
    # ±20%（81000 → [64800, 97200]）：60000 出界；65000~85000 在内
    assert [r["strike"] for r in chain["groups"][0]["rows"]] == [
        65000.0, 70000.0, 75000.0, 80000.0, 85000.0]


def test_fetch_chain_selected_expiry_and_full_range(fake_sdk):
    chain = od.fetch_chain("BTC-USD_UM", expiries=[_D2], spot_pct_range=None)
    assert [g["exp_ms"] for g in chain["groups"]] == [_D2]
    assert [r["strike"] for r in chain["groups"][0]["rows"]] == [
        60000.0, 65000.0, 70000.0, 75000.0, 80000.0, 85000.0]


def test_put_premium_apr_and_iv(fake_sdk):
    """U 本位 80000-P ask=372.6（USD/1 名义币），lot=0.01 BTC，spot=81000。

    prem_usd = 372.6 × 0.01 = 3.726（USD/张）；prem_pct = 372.6/81000×100 = 0.46%；apr = ×365/1 天
    """
    chain = od.fetch_chain("BTC-USD_UM", expiries=[_D1], spot_pct_range=None)
    row = next(r for r in chain["groups"][0]["rows"] if r["strike"] == 80000)
    p = row["P"]
    assert p["ask"] == 372.6
    assert p["prem_usd"] == pytest.approx(3.726, rel=1e-6)
    assert p["prem_pct"] == pytest.approx(0.46)
    # 剩余天数含小时偏差 → 年化用宽松容差
    assert p["apr_pct"] == pytest.approx(0.46 * 365.0, rel=1e-3)
    assert p["iv"] == pytest.approx(50.0)
    assert row["C"]["bid"] == 1000


def test_hv_from_closes(fake_sdk):
    hv = od._spot_hv("BTC-USD_UM", 30)
    assert hv["days"] == 4
    assert hv["hv_pct"] is not None and hv["hv_pct"] > 0


def test_xau_index_reference_and_no_hv(fake_sdk):
    """XAU 无现货盘 → 参考价走指数（idxPx），现货 HV 不可用（None）。"""
    chain = od.fetch_chain("XAU-USD_UM", expiries=[_D1], spot_pct_range=None)
    assert chain["spot"] == 4473.25
    assert chain["spot_inst"] == "XAU-USD (index)"
    assert chain["hv"]["hv_pct"] is None
    # 盘口/权利金换算与 BTC 家族共用同一公式（prem_usd = ask × lot，见 test_put_premium_apr_and_iv）；
    # XAU 无现货盘仅影响参考价来源与 HV，不影响 prem 口径
    assert chain["groups"][0]["rows"]

