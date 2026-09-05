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
        "listTime": str(exp - 36 * 3600_000),
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


# mark 价生命周期假数据：305 根 5m（ts = _NOW 往前 305×5m ≈ 25.4h），分两页返回
_MARK_ROWS = [
    [str(_NOW - i * 300_000), "0.2", "0.3", "0.1", str(round(0.05 + i * 0.001, 4)), "0"]
    for i in range(305)
]


class _FakeMarket:
    def get_ticker(self, instId="", **kw):
        return {"code": "0", "data": [{"instId": instId, "last": "81000"}], "msg": ""}

    def get_tickers(self, instType="OPTION", **kw):
        return {"code": "0", "data": list(_tickers_dict().values()), "msg": ""}

    def get_index_tickers(self, instId="", **kw):
        return {"code": "0", "data": [{"instId": instId, "idxPx": "4473.25"}], "msg": ""}

    def _candles(self, rows, after=""):
        if after:
            a = int(after)
            rows = [r for r in rows if int(r[0]) < a]
        return rows[:300]

    def get_mark_price_candles(self, instId="", after="", before="", bar="", limit=""):
        return {"code": "0", "data": self._candles(_MARK_ROWS, after), "msg": ""}

    def get_history_mark_price_candles(self, instId="", after="", before="", bar="", limit=""):
        # 生命周期短合约：近期段已翻到 listTime、无更早历史 → 返回空（正常结束）
        return {"code": "0", "data": [], "msg": ""}

    def get_history_index_candles(self, instId="", after="", before="", bar="", limit=""):
        return {"code": "0", "data": [], "msg": ""}

    def get_history_candles(self, instId="", after="", before="", bar="", limit=""):
        # 收盘 100 → 110 → 100 → 110 → 121（返回按 ts 降序）
        rows = []
        ts = _NOW
        for c in (121, 110, 100, 110, 100):
            rows.append([str(ts), "", "", "", str(c), "", "", ""])
            ts -= 86_400_000
        return {"code": "0", "data": self._candles(rows, after), "msg": ""}


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


# ── 生命周期（mark 价 + 标的参考价对齐）───────────────────────

def test_fetch_lifecycle_pages_join_and_order(fake_sdk):
    """305 根 5m 分两页（300+5）合并完整、升序；现货日线 join 对齐（仅 _NOW 根）；
    ref 缺失容忍；prem 口径 = mark × lot。"""
    inst_id = f"BTC-USD_UM-{_D1}-80000-P"
    d = od.fetch_lifecycle(inst_id, "5m")
    assert d["inst_id"] == inst_id
    assert d["opt_type"] == "P"
    assert d["lot_coin"] == 0.01
    assert d["family"] == "BTC-USD_UM"
    assert d["ref_inst"] == "BTC-USDT"
    assert d["ref_kind"] == "spot"
    assert len(d["rows"]) == 305          # 两页合并、去重完整（无空洞/重复）
    times = [r["ts"] for r in d["rows"]]
    assert times == sorted(times)          # 升序（最新在最后）
    assert d["rows"][0]["mark_px"] is not None
    # 现货日线仅 ts=_NOW 一根与 5m 轴对齐（其余行 ref=None 容忍，不报错）
    assert d["rows"][-1]["ts"] == _NOW
    assert d["rows"][-1]["ref_px"] == 121.0
    assert d["rows"][-1]["mark_px"] == pytest.approx(0.05, abs=1e-6)


def test_fetch_lifecycle_rejects_bad_input(fake_sdk):
    inst_id = f"BTC-USD_UM-{_D1}-80000-P"
    with pytest.raises(od.OkxSdkError, match="粒度"):
        od.fetch_lifecycle(inst_id, "7m")
    with pytest.raises(od.OkxSdkError, match="未知标的家族"):
        od.fetch_lifecycle("XRP-USD_UM-260906-101-P", "5m")
    with pytest.raises(od.OkxSdkError, match="不存在"):
        od.fetch_lifecycle(f"BTC-USD_UM-{_D1}-99999-P", "5m")

