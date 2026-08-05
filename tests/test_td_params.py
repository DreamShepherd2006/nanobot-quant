"""Tests for TD parameter defaults, validation, persistence and the
WebUI handlers (/config/td-params)."""

import asyncio
import json

import pandas as pd
import pytest
from nanobot_quant.strategies.td_sequential import calculate
from nanobot_quant.td_params import (
    DEFAULT_TD_PARAMS,
    PARAM_META,
    load_td_params,
    save_td_params,
    validate_value,
    validate_weights,
)
from nanobot_quant.td_params_handlers import register_td_params_routes

from nanobot_quant import td_params as td_params_mod

# ── Helpers ─────────────────────────────────────────────────────────────

class _FakeRequest:
    def __init__(self, body=None):
        self._body = body

    async def json(self):
        return self._body


class _FakeApp:
    def __init__(self):
        self.routes = []

    def get(self, path):
        def deco(fn):
            self.routes.append(("GET", path, fn))
            return fn
        return deco

    def post(self, path):
        def deco(fn):
            self.routes.append(("POST", path, fn))
            return fn
        return deco


@pytest.fixture(autouse=True)
def _isolated_params(tmp_path, monkeypatch):
    """Point td_params_path (both import sites) at a temp file."""
    target = tmp_path / "td_params.json"

    def fake_path():
        return target

    monkeypatch.setattr(td_params_mod, "td_params_path", fake_path)
    monkeypatch.setattr(
        "nanobot_quant.td_params_handlers.td_params_path", fake_path
    )
    yield target


def _call(handler, body=None):
    return asyncio.run(handler(_FakeRequest(body)))


def _falling_df(n_drop: int = 9) -> pd.DataFrame:
    """Construct a DataFrame with a clean buy setup:
    rising start (flip baseline) followed by ``n_drop`` consecutive falling
    closes.  With compare_length=4 the final buy_setup_count == n_drop
    (bar i=5 → count 1 … i=4+n_drop → count n_drop)."""
    rises = [100, 101, 102, 103, 104]          # indices 0–4 (flip baseline)
    drops = list(range(100, 100 - n_drop, -1))  # 100, 99, 98, …
    closes = rises + drops
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [1_000_000] * len(closes),
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )
    return df


# ── Defaults / loading ──────────────────────────────────────────────────

class TestDefaults:
    def test_defaults_match_pre_param_hardcoded(self):
        assert DEFAULT_TD_PARAMS["setup_period"] == 9
        assert DEFAULT_TD_PARAMS["countdown_period"] == 13
        assert DEFAULT_TD_PARAMS["compare_length"] == 4
        assert DEFAULT_TD_PARAMS["countdown_strict"] is True
        assert DEFAULT_TD_PARAMS["recycle_threshold"] == 18
        assert DEFAULT_TD_PARAMS["weight_setup"] == 0.40
        assert DEFAULT_TD_PARAMS["weight_countdown"] == 0.30
        assert DEFAULT_TD_PARAMS["weight_tdst"] == 0.15
        assert DEFAULT_TD_PARAMS["weight_volume"] == 0.10
        assert DEFAULT_TD_PARAMS["weight_bb"] == 0.05
        assert DEFAULT_TD_PARAMS["score_threshold"] == 0.0
        assert DEFAULT_TD_PARAMS["entry_setup"] == 9
        assert DEFAULT_TD_PARAMS["exit_setup"] == 9
        assert DEFAULT_TD_PARAMS["exit_countdown"] == 13
        assert DEFAULT_TD_PARAMS["tdst_filter"] is False
        # default weights are a valid combination
        assert validate_weights(DEFAULT_TD_PARAMS) is None

    def test_meta_covers_all_params(self):
        assert set(PARAM_META) == set(DEFAULT_TD_PARAMS)
        for k, meta in PARAM_META.items():
            assert meta.get("group") in ("td", "weights", "strategy")
            assert meta.get("label")

    def test_load_without_file_returns_defaults(self):
        assert load_td_params() == DEFAULT_TD_PARAMS

    def test_load_corrupt_file_returns_defaults(self, _isolated_params):
        _isolated_params.write_text("{not json", encoding="utf-8")
        assert load_td_params() == DEFAULT_TD_PARAMS

    def test_load_merges_partial_file(self, _isolated_params):
        _isolated_params.write_text(
            json.dumps({"setup_period": 10}), encoding="utf-8"
        )
        params = load_td_params()
        assert params["setup_period"] == 10
        assert params["countdown_period"] == 13  # untouched default

    def test_load_ignores_invalid_values(self, _isolated_params):
        _isolated_params.write_text(
            json.dumps({"setup_period": 99, "countdown_strict": "yes"}),
            encoding="utf-8",
        )
        params = load_td_params()
        assert params["setup_period"] == 9  # invalid → default
        assert params["countdown_strict"] is True


# ── Validation ──────────────────────────────────────────────────────────

class TestValidation:
    @pytest.mark.parametrize("bad", [4, 5.5, "9", True, None])
    def test_setup_period_invalid(self, bad):
        assert validate_value("setup_period", bad) is not None

    @pytest.mark.parametrize("good", [5, 9, 14])
    def test_setup_period_valid(self, good):
        assert validate_value("setup_period", good) is None

    @pytest.mark.parametrize("bad", [8, 17, 0])
    def test_countdown_period_invalid(self, bad):
        assert validate_value("countdown_period", bad) is not None

    @pytest.mark.parametrize("good", [9, 13, 16])
    def test_countdown_period_valid(self, good):
        assert validate_value("countdown_period", good) is None

    @pytest.mark.parametrize("bad", [2, 6])
    def test_compare_length_invalid(self, bad):
        assert validate_value("compare_length", bad) is not None

    @pytest.mark.parametrize("good", [3, 4, 5])
    def test_compare_length_valid(self, good):
        assert validate_value("compare_length", good) is None

    def test_bool_fields(self):
        assert validate_value("countdown_strict", True) is None
        assert validate_value("countdown_strict", False) is None
        assert validate_value("countdown_strict", 1) is not None
        assert validate_value("tdst_filter", "on") is not None

    def test_weight_bounds(self):
        assert validate_value("weight_setup", -0.1) is not None
        assert validate_value("weight_setup", 1.5) is not None
        assert validate_value("weight_setup", 0.4) is None

    def test_score_threshold_bounds(self):
        assert validate_value("score_threshold", -1) is not None
        assert validate_value("score_threshold", 101) is not None
        assert validate_value("score_threshold", 60) is None

    def test_weights_sum_must_equal_one(self):
        params = dict(DEFAULT_TD_PARAMS)
        params["weight_setup"] = 0.45  # now sums to 1.05
        assert validate_weights(params) is not None
        params["weight_bb"] = 0.0  # back to 1.00
        assert validate_weights(params) is None


# ── Persistence ─────────────────────────────────────────────────────────

class TestPersistence:
    def test_save_load_roundtrip(self, _isolated_params):
        custom = dict(DEFAULT_TD_PARAMS)
        custom.update(
            setup_period=10,
            countdown_period=11,
            weight_setup=0.50,
            weight_countdown=0.25,
            weight_tdst=0.15,
            weight_volume=0.05,
            weight_bb=0.05,
            score_threshold=20.0,
        )
        result = save_td_params(custom)
        assert result["ok"] is True
        assert _isolated_params.is_file()
        loaded = load_td_params()
        for k in DEFAULT_TD_PARAMS:
            assert loaded[k] == custom[k], k

    def test_save_rejects_invalid(self, _isolated_params):
        bad = dict(DEFAULT_TD_PARAMS)
        bad["setup_period"] = 99
        result = save_td_params(bad)
        assert result["ok"] is False
        assert "Setup" in result["error"]
        assert not _isolated_params.is_file()

    def test_save_rejects_bad_weights(self, _isolated_params):
        bad = dict(DEFAULT_TD_PARAMS)
        bad["weight_volume"] = 0.20  # sums to 1.10
        result = save_td_params(bad)
        assert result["ok"] is False
        assert "权重合计" in result["error"]

    def test_reset_removes_file(self, _isolated_params):
        custom = dict(DEFAULT_TD_PARAMS)
        custom["setup_period"] = 12
        assert save_td_params(custom)["ok"] is True
        result = save_td_params({"reset": True})
        assert result["ok"] is True
        assert not _isolated_params.is_file()
        assert load_td_params() == DEFAULT_TD_PARAMS


# ── Engine behaviour (zero change by default) ───────────────────────────

class TestStrategyParams:
    """Static assertions on the lumibot strategy parameter surface."""

    def test_strategy_parameters_include_td_defaults(self):
        from nanobot_quant.strategies.td_sequential_strategy import (
            TdSequentialStrategy,
        )

        params = TdSequentialStrategy.parameters
        assert params["setup_period"] == 9
        assert params["countdown_period"] == 13
        assert params["compare_length"] == 4
        assert params["score_threshold"] == 0.0
        assert params["entry_setup"] == 9
        assert params["exit_setup"] == 9
        assert params["exit_countdown"] == 13
        assert params["tdst_filter"] is False
        assert params["weight_setup"] == 0.40
        # strategy defaults are overridable per backtest run
        overridden = dict(params, setup_period=10, score_threshold=25.0)
        assert overridden["setup_period"] == 10
        assert overridden["score_threshold"] == 25.0


class TestEngineBehaviour:
    def test_default_params_identical_to_explicit_defaults(self):
        df = _falling_df()
        a = calculate(df)
        b = calculate(df, params=dict(DEFAULT_TD_PARAMS))
        assert a == b
        assert a["setup_buy"] == 9
        assert a["recommendation"] != "HOLD"  # setup 9 complete → signal

    def test_setup_period_parameter_effective(self):
        r9 = calculate(_falling_df(9), params=dict(DEFAULT_TD_PARAMS))
        assert r9["setup_buy"] == 9
        assert r9["recommendation"].startswith("BUY")  # b_9 checked vs setup_period=9

        p10 = dict(DEFAULT_TD_PARAMS)
        p10["setup_period"] = 10
        # only 9 falling bars → setup 10 not complete yet
        r10 = calculate(_falling_df(9), params=p10)
        assert r10["setup_buy"] == 9
        assert r10["recommendation"] == "HOLD"
        # 10 falling bars → setup 10 completes
        r10b = calculate(_falling_df(10), params=p10)
        assert r10b["setup_buy"] == 10
        assert r10b["recommendation"].startswith("BUY")

        p14 = dict(DEFAULT_TD_PARAMS)
        p14["setup_period"] = 14
        r14 = calculate(_falling_df(13), params=p14)
        assert r14["setup_buy"] == 13
        assert r14["recommendation"] == "HOLD"

    def test_weights_effective_on_score(self):
        df = _falling_df()
        base = calculate(df, params=dict(DEFAULT_TD_PARAMS))
        # bump setup weight (keep sum=1 by cutting bb) → score must rise
        p = dict(DEFAULT_TD_PARAMS)
        p["weight_setup"] = 0.70
        p["weight_bb"] = 0.0  # keep total 1.0
        boosted = calculate(df, params=p)
        assert boosted["score"] > base["score"]

    def test_compare_length_effective(self):
        # same series evaluated with compare_length=3 also completes
        df = _falling_df()
        r3 = calculate(df, params={
            **DEFAULT_TD_PARAMS, "compare_length": 3,
        })
        assert r3["setup_buy"] == 9


# ── WebUI handlers ──────────────────────────────────────────────────────

class TestHandlers:
    def test_two_endpoints(self):
        app = _FakeApp()
        register_td_params_routes(app, None)
        paths = sorted((m, p) for m, p, _ in app.routes)
        assert paths == sorted(
            [("GET", "/config/td-params"), ("POST", "/config/td-params")]
        )

    def test_page_renders(self, _isolated_params):
        from nanobot_quant.td_params_handlers import td_params_page

        resp = _call(td_params_page)
        assert resp.status_code == 200
        html = resp.body.decode()
        assert "TD 策略参数" in html
        assert "Setup 周期" in html
        assert "权重合计" in html
        assert 'value="9"' in html  # default setup period rendered
        assert "当前策略：TD Sequential（原版）" in html  # strategy banner

    def test_save_via_handler(self, _isolated_params):
        from nanobot_quant.td_params_handlers import td_params_save

        custom = dict(DEFAULT_TD_PARAMS)
        custom["setup_period"] = 11
        resp = _call(td_params_save, custom)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert load_td_params()["setup_period"] == 11

    def test_save_invalid_via_handler(self, _isolated_params):
        from nanobot_quant.td_params_handlers import td_params_save

        bad = dict(DEFAULT_TD_PARAMS)
        bad["setup_period"] = 4
        resp = _call(td_params_save, bad)
        assert resp.status_code == 400
        data = json.loads(resp.body.decode())
        assert data["ok"] is False

    def test_save_bad_body(self, _isolated_params):
        from nanobot_quant.td_params_handlers import td_params_save

        resp = _call(td_params_save, None)
        assert resp.status_code == 400

    def test_reset_via_handler(self, _isolated_params):
        from nanobot_quant.td_params_handlers import td_params_page, td_params_save

        custom = dict(DEFAULT_TD_PARAMS)
        custom["setup_period"] = 12
        assert _call(td_params_save, custom).status_code == 200
        resp = _call(td_params_save, {"reset": True})
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert not _isolated_params.is_file()
        assert "默认参数" in _call(td_params_page).body.decode()


# ── Per-strategy parameter sets (strategy registry) ─────────────────────

class TestPerStrategyParams:
    """Parameters are stored per strategy; switching strategy shows the
    selected strategy's own set."""

    def test_params_isolated_per_strategy(self, _isolated_params):
        res = save_td_params({"setup_period": 8}, strategy="td_sequential")
        assert res["ok"] is True
        assert load_td_params(strategy="td_sequential")["setup_period"] == 8
        # the other variant is untouched
        assert load_td_params(strategy="td_sequential_cycle")["setup_period"] == 9
        assert _isolated_params.is_file()

    def test_legacy_flat_layout_migrates_to_default_strategy(self, _isolated_params):
        """Old single-layer file belongs to the production default strategy;
        a subsequent save migrates the file to the per-strategy layout."""
        _isolated_params.write_text(
            json.dumps({"setup_period": 8}), encoding="utf-8"
        )
        assert load_td_params(strategy="td_sequential")["setup_period"] == 8
        assert load_td_params(strategy="td_sequential_cycle")["setup_period"] == 9
        save_td_params({"countdown_period": 12}, strategy="td_sequential_cycle")
        raw = json.loads(_isolated_params.read_text(encoding="utf-8"))
        assert raw["td_sequential"]["setup_period"] == 8
        assert raw["td_sequential_cycle"]["countdown_period"] == 12

    def test_reset_only_current_strategy(self, _isolated_params):
        save_td_params({"setup_period": 8}, strategy="td_sequential")
        save_td_params({"setup_period": 7}, strategy="td_sequential_cycle")
        res = save_td_params({"reset": True}, strategy="td_sequential")
        assert res["ok"] is True
        assert load_td_params(strategy="td_sequential")["setup_period"] == 9
        assert load_td_params(strategy="td_sequential_cycle")["setup_period"] == 7

    def test_cycle_defaults_no_countdown(self):
        """cycle 参数默认无 countdown 权重（归一化四项合计 1.0）."""
        d = load_td_params(strategy="td_sequential_cycle")
        assert d["weight_countdown"] == 0.0
        assert abs(sum(d[k] for k in ("weight_setup", "weight_tdst",
                                      "weight_volume", "weight_bb")) - 1.0) <= 1e-6
        # original variant unaffected
        base = load_td_params(strategy="td_sequential")
        assert base["weight_countdown"] == 0.30

    def test_param_applies_filters_countdown(self):
        from nanobot_quant.td_params import PARAM_META
        from nanobot_quant.td_params_handlers import _param_applies

        assert _param_applies(PARAM_META["setup_period"], "td_sequential_cycle")
        assert not _param_applies(PARAM_META["countdown_period"], "td_sequential_cycle")
        assert _param_applies(PARAM_META["countdown_period"], "td_sequential")

    def test_group_html_omits_countdown_for_cycle(self):
        from nanobot_quant.strategies.registry import get_strategy
        from nanobot_quant.td_params_handlers import _group_html

        base = get_strategy("td_sequential").params_defaults
        cycle = get_strategy("td_sequential_cycle").params_defaults
        assert "Countdown 权重" not in _group_html("weights", cycle, "td_sequential_cycle", cycle)
        assert "Setup 权重" in _group_html("weights", cycle, "td_sequential_cycle", cycle)
        assert "Countdown 权重" in _group_html("weights", base, "td_sequential", base)


# ── TDST break signals are display/reference only (2026-08-05) ──────────


def _mk_df(closes: list) -> pd.DataFrame:
    """One bar per close: High=close+1, Low=close-1, daily index."""
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [1_000_000] * len(closes),
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )


def test_tdst_break_is_display_only():
    """TDST 突破（setup 未完成）：表格列保留 BUY (Resistance Break)，
    但 calculate() 摘要降级 HOLD——执行链不触发（与回测 setup 规则一致）。"""
    from nanobot_quant.strategies.td_sequential import _DeMarkEngine, calculate

    # setup_buy 1-9 完成后重置，最后一根 110 突破 TDST 压力 101
    closes = [100, 101, 102, 103, 104] + list(range(100, 91, -1)) + [110]
    df = _mk_df(closes)
    engine = _DeMarkEngine(df, None)
    engine.run_all(news_count=0)
    last = engine.df.iloc[-1]
    assert last["sell_setup_count"] != 9          # setup 未完成
    assert last["buy_setup_count"] == 0
    assert last["recommendation"] == "BUY (Resistance Break)"  # 表格仍展示
    assert calculate(df)["recommendation"] == "HOLD"            # 执行链降级


def test_setup_signal_not_overridden_by_tdst_break():
    """同一 bar setup 完成 + TDST 突破：rec 保留 setup 信号方向（不被突破覆盖）。"""
    from nanobot_quant.strategies.td_sequential import _DeMarkEngine, calculate

    # sell setup 1-9 完成（连续上涨），最后一根 113 同时突破 TDST 压力 101
    closes = (
        [100, 101, 102, 103, 104]
        + list(range(100, 91, -1))
        + [105, 106, 107, 108, 109, 110, 111, 112, 113]
    )
    df = _mk_df(closes)
    engine = _DeMarkEngine(df, None)
    engine.run_all(news_count=0)
    last = engine.df.iloc[-1]
    assert last["sell_setup_count"] == 9
    assert last["recommendation"] == "SELL (Setup Complete)"
    assert calculate(df)["recommendation"] == "SELL (Setup Complete)"
