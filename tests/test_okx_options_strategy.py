"""卖 put / 卖 call 自动循环决策核心单测（纯函数，不触网）。"""

from __future__ import annotations

import pytest

from nanobot_quant import okx_options_select as osl
from nanobot_quant import okx_options_strategy as st

FAMILY = "SOL-USD_UM"

# 注入用的期权链（与 okx_options_data.fetch_chain 同形）
CHAIN = {
    "family": FAMILY, "spot": 100.0, "lot_coin": 0.1,
    "groups": [{
        "days": 5, "date": "2026-09-20", "exp_ms": 1789948800000,
        "rows": [
            {"strike": 88.0, "P": {"inst_id": "SOL-USD_UM-260920-88-P",
                                    "bid": 0.30, "ask": 0.36, "iv": 80.0, "delta": -0.12}},
            {"strike": 92.0, "P": {"inst_id": "SOL-USD_UM-260920-92-P",
                                    "bid": 0.60, "ask": 0.70, "iv": 75.0, "delta": -0.22}},
        ],
    }],
}

PARAMS = {"entry_setup": 9, "entry_countdown": 13,
          "max_contracts_per_family": 1, "max_contracts_total": 3}

# 卖 call（covered）注入链：C 侧候选；strike 104 距现价 4% < 5% 应被距离过滤
CALL_CHAIN = {
    "family": FAMILY, "spot": 100.0, "lot_coin": 0.1,
    "groups": [{
        "days": 5, "date": "2026-09-20", "exp_ms": 1789948800000,
        "rows": [
            {"strike": 104.0, "C": {"inst_id": "SOL-USD_UM-260920-104-C",
                                      "bid": 1.20, "ask": 1.35, "iv": 80.0, "delta": 0.30}},
            {"strike": 108.0, "C": {"inst_id": "SOL-USD_UM-260920-108-C",
                                      "bid": 0.50, "ask": 0.60, "iv": 80.0, "delta": 0.20}},
            {"strike": 112.0, "C": {"inst_id": "SOL-USD_UM-260920-112-C",
                                      "bid": 0.20, "ask": 0.28, "iv": 80.0, "delta": 0.10}},
        ],
    }],
}
COVERED = {"base": "SOL", "spot_avail": 0.2, "sellable_sz": 2,
           "spot_cov_pct": 200.0, "cost_hint": 104.0}
CALL_PARAMS = {"max_calls_per_family": 1, "max_calls_total": 2}


def _call(**kw):
    args = {"params": CALL_PARAMS, "covered": dict(COVERED),
            "chain": CALL_CHAIN, "base_px": 100.0}
    args.update(kw)
    return st.evaluate_call_entry(FAMILY, **args)


def _entry(**kw):
    args = {"td_signal": {"setup_buy": 9, "cd_buy": 0}, "params": PARAMS,
            "chain": CHAIN, "base_px": 100.0}
    args.update(kw)
    return st.evaluate_entry(FAMILY, **args)


# ── TD 信号判定 ──────────────────────────────────────
class TestTdEntryReason:
    def test_setup_channel(self):
        assert st.td_entry_reason({"setup_buy": 9}, entry_setup=9,
                                  entry_countdown=13) == "buy9(setup_buy=9)"

    def test_countdown_channel(self):
        r = st.td_entry_reason({"setup_buy": 3, "cd_buy": 13}, entry_setup=9,
                               entry_countdown=13)
        assert r == "cd13(cd_buy=13)"

    def test_setup_wins_when_both(self):
        r = st.td_entry_reason({"setup_buy": 10, "cd_buy": 13}, entry_setup=9,
                               entry_countdown=13)
        assert r.startswith("buy9")

    def test_below_thresholds_and_junk(self):
        assert st.td_entry_reason({"setup_buy": 8, "cd_buy": 12}, entry_setup=9,
                                  entry_countdown=13) is None
        assert st.td_entry_reason({}, entry_setup=9, entry_countdown=13) is None
        assert st.td_entry_reason(None, entry_setup=9, entry_countdown=13) is None


# ── 入场决策 ─────────────────────────────────────────
class TestEvaluateEntry:
    def test_happy_path_uses_selector_top1(self):
        d, note = _entry()
        assert d is not None
        assert d.inst_id == "SOL-USD_UM-260920-92-P"   # 净收益率更高排在前
        assert d.sz == 1 and d.bid == 0.60
        assert d.entry_reason == "buy9(setup_buy=9)"
        assert "信号：buy9" in note

    def test_no_signal_returns_none_with_reason(self):
        d, note = _entry(td_signal={"setup_buy": 5, "cd_buy": 2})
        assert d is None and "无 TD 衰竭信号" in note

    def test_family_contract_cap_blocks(self):
        d, note = _entry(open_contracts=1)
        assert d is None and "张数上限" in note

    def test_total_contract_cap_blocks(self):
        d, note = _entry(total_contracts=3)
        assert d is None and "全局在仓" in note

    def test_iv_gate_blocks_low_percentile(self):
        d, note = _entry(params={**PARAMS, "iv_min_percentile": 70},
                         iv_percentile=42.0)
        assert d is None and "IV 闸门" in note

    def test_iv_gate_fail_open_when_no_sample(self):
        d, note = _entry(params={**PARAMS, "iv_min_percentile": 70},
                         iv_percentile=None)
        assert d is not None and "样本不足，放行" in note

    def test_iv_gate_passes_when_high(self):
        d, note = _entry(params={**PARAMS, "iv_min_percentile": 70},
                         iv_percentile=88.0)
        assert d is not None and "IV 闸门" in note

    def test_no_candidate_when_chain_empty(self):
        d, note = _entry(chain={"spot": 100.0, "lot_coin": 0.1, "groups": []})
        assert d is None and "无合格候选" in note

    def test_iv_gate_disabled_by_default(self):
        d, _ = _entry(iv_percentile=5.0)   # 未配 iv_min_percentile
        assert d is not None


# ── 出场决策 ─────────────────────────────────────────
def _pos(inst, pos, avg, mark, side="short"):
    return {"inst_id": inst, "side": side, "pos": pos, "avg_px": avg, "mark_px": mark}


class TestEvaluateExits:
    def test_take_profit_when_premium_halved(self):
        rows = [_pos("SOL-USD_UM-260918-94-P", 1.0, 0.40, 0.19)]
        out = st.evaluate_exits(rows, tp_pct=50)
        assert len(out) == 1 and out[0].drop_pct == 52.5 and out[0].reason == "take_profit"

    def test_not_yet_when_drop_small(self):
        assert st.evaluate_exits([_pos("SOL-USD_UM-260918-94-P", 1.0, 0.40, 0.30)],
                                 tp_pct=50) == []

    def test_disabled_when_zero(self):
        assert st.evaluate_exits([_pos("SOL-USD_UM-260918-94-P", 1.0, 0.40, 0.01)],
                                 tp_pct=0) == []

    def test_long_positions_ignored(self):
        rows = [_pos("SOL-USD_UM-260918-94-C", 1.0, 0.40, 0.10, side="long")]
        assert st.evaluate_exits(rows, tp_pct=50) == []

    def test_junk_rows_skipped(self):
        rows = [_pos("", 1.0, 0.4, 0.1), _pos("SOL-USD_UM-260918-94-P", 0, 0.4, 0.1),
                _pos("SOL-USD_UM-260918-94-P", 1.0, None, 0.1)]
        assert st.evaluate_exits(rows, tp_pct=50) == []

    def test_premium_rises_no_exit(self):
        rows = [_pos("SOL-USD_UM-260918-94-P", 1.0, 0.25, 0.39)]  # 与当前 94-P 实况同
        assert st.evaluate_exits(rows, tp_pct=50) == []


class TestContractsByFamily:
    def test_counts_short_only(self):
        rows = [_pos("SOL-USD_UM-260918-94-P", 1.0, 0.3, 0.3),
                _pos("SOL-USD_UM-260918-96-P", 2.0, 0.3, 0.3),
                _pos("BTC-USD_UM-260918-56000-C", 1.0, 40, 40, side="long")]
        assert st.contracts_by_family(rows) == {"SOL": 3}

    def test_empty(self):
        assert st.contracts_by_family([]) == {}
        assert st.contracts_by_family(None) == {}


# ── 方向隔离（§24 C42：卖 put 线与卖 call 线互不越界） ──────────────
class TestRightIsolation:
    def test_put_line_does_not_close_short_call(self):
        rows = [_pos("SOL-USD_UM-260918-94-P", 1.0, 0.40, 0.19),      # 已达 put 止盈线
                _pos("SOL-USD_UM-260918-110-C", 1.0, 0.40, 0.10)]     # 卖出的 call（side=short）
        out = st.evaluate_exits(rows, tp_pct=50)
        assert [x.inst_id for x in out] == ["SOL-USD_UM-260918-94-P"]

    def test_call_line_manages_call_only(self):
        rows = [_pos("SOL-USD_UM-260918-94-P", 1.0, 0.40, 0.19),
                _pos("SOL-USD_UM-260918-110-C", 1.0, 0.40, 0.19)]
        out = st.evaluate_exits(rows, tp_pct=50, opt_type="C")
        assert [x.inst_id for x in out] == ["SOL-USD_UM-260918-110-C"]

    def test_opt_type_field_wins_over_inst_id(self):
        row = {**_pos("SOL-USD_UM-260918-110-C", 1.0, 0.40, 0.19), "opt_type": "C"}
        assert st.evaluate_exits([row], tp_pct=50, opt_type="P") == []
        assert len(st.evaluate_exits([row], tp_pct=50, opt_type="C")) == 1

    def test_contracts_by_family_separates_right(self):
        rows = [_pos("SOL-USD_UM-260918-94-P", 1.0, 0.3, 0.3),
                _pos("SOL-USD_UM-260918-96-P", 2.0, 0.3, 0.3),
                _pos("SOL-USD_UM-260918-110-C", 5.0, 0.3, 0.3)]      # call 不吃 put 额度
        assert st.contracts_by_family(rows) == {"SOL": 3}
        assert st.contracts_by_family(rows, opt_type="C") == {"SOL": 5}

    def test_unparsable_inst_not_counted(self):
        # 方向判不出的行：不计额度、不参与止盈（fail-closed 不动仓）
        rows = [_pos("NOPE", 2.0, 0.3, 0.3)]
        assert st.contracts_by_family(rows) == {}
        assert st.evaluate_exits([_pos("NOPE", 2.0, 0.40, 0.10)], tp_pct=50) == []


# ── 卖 call（covered）入场决策（§33.40）──────────────────────
class TestEvaluateCallEntry:
    def test_happy_path(self):
        d, note = _call()
        assert d is not None
        assert d.inst_id == "SOL-USD_UM-260920-108-C"       # 104-C 被距离过滤（距现价 4%）
        assert d.opt_type == "C" and d.sz == 1
        assert d.cost_basis == 104.0                        # 默认取同家族接货价
        assert d.collateral_usd == pytest.approx(10.0)       # 现货市值 100 × 0.1
        assert "covered" in d.entry_reason and "成本锚 C=104" in note
        assert d.to_event()["opt_type"] == "C"

    def test_put_decision_defaults_to_p(self):
        d, _ = _entry()
        assert d.opt_type == "P" and d.to_event()["opt_type"] == "P"

    def test_family_cap_blocks(self):
        d, note = _call(open_calls=1)
        assert d is None and "张数上限" in note

    def test_total_cap_blocks(self):
        d, note = _call(open_calls=0, total_calls=2)
        assert d is None and "张数上限：全局" in note

    def test_covered_capacity_subtracts_inflight(self):
        """现货覆盖 2 张、已在仓 2 张 call ⇒ 无余量（不重复使用覆盖）。"""
        d, note = _call(open_calls=2, total_calls=2, params={})
        assert d is None and "covered 容量不足" in note

    def test_covered_missing_fails_closed(self):
        d, note = _call(covered=None)
        assert d is None and "covered 上下文不可用" in note

    def test_cost_anchor_missing_is_fail_closed(self):
        cv = {**COVERED, "cost_hint": None}
        d, note = _call(covered=cv)
        assert d is None and "无成本锚" in note and "fail-closed" in note

    def test_allow_no_cost_basis_passes_with_warning(self):
        cv = {**COVERED, "cost_hint": None}
        d, note = _call(covered=cv, params={**CALL_PARAMS, "allow_no_cost_basis": True})
        assert d is not None and d.cost_basis is None
        assert "⚠️ 无成本锚" in note

    def test_explicit_cost_basis_overrides_hint(self):
        d, note = _call(cost_basis=120.0)
        assert d is None and "无合格候选" in note            # K+bid ≥ 120 的档不存在
        d2, _ = _call(cost_basis=108.5)                       # 108-C: 108+0.5 = 108.5 ≥ 108.5 ✓
        assert d2 is not None and d2.cost_basis == 108.5

    def test_cost_basis_filter_excludes_cheap_calls(self):
        """保本门硬过滤：K+bid < C 的档不进候选（选档与执行门同判据）。"""
        res = osl.select_calls(FAMILY, base_px=100.0, chain=CALL_CHAIN, cost_basis=112.2)
        ids = [c["inst_id"] for c in res["candidates"]]
        assert ids == ["SOL-USD_UM-260920-112-C"]            # 112+0.2 = 112.2 ≥ C
        # 104-C 先被距离过滤（距现价 4%）→ 保本门只过滤掉 108-C 一档
        assert res["filtered"]["cost_basis"] == 1
        assert res["filtered"]["distance"] == 1

    def test_no_candidates_when_all_too_close(self):
        chain = {"family": FAMILY, "spot": 100.0, "lot_coin": 0.1,
                 "groups": [{"days": 5, "date": "d", "exp_ms": 1,
                             "rows": [{"strike": 101.0,
                                       "C": {"inst_id": "SOL-USD_UM-260920-101-C",
                                             "bid": 0.9, "ask": 1.0, "iv": 80.0, "delta": 0.4}}]}]}
        d, note = _call(chain=chain)
        assert d is None and "无合格候选" in note
