"""§33.43 Step 2 / 3 / 4 护栏单测（2026-09-28）。

- Step 2 成本锚 C 口径：``refresh_cost_bases``（(现金赔付+实际补买支出)÷面值×张数）
  + ``covered_context`` 的 ``cost_hint`` / ``cost_pending`` / ``cost_rows``
- Step 3 卖 put **现金担保前置门**：``evaluate_entry(cash_avail=...)``（含负数哨兵）
- Step 4a 到期档显式化：selector 默认 0–3 天 + **去掉静默放宽** + 旧默认一次性迁移
- Step 4b 卖 call **去重门**：``has_pending_inst``（含查询失败 fail-closed）

纯函数 / mock，不触网、不动真实持久卷。
"""

from __future__ import annotations

import json

import pytest

from nanobot_quant import okx_options_data as od
from nanobot_quant import okx_options_select as osl
from nanobot_quant import okx_options_strategy as st
from nanobot_quant import okx_options_trade as ot

FAMILY = "SOL-USD_UM"

# 注入链：strike 92 = 距现价 8%（≥5% 硬过滤）、delta −0.22（带内）→ 唯一候选
CHAIN = {
    "family": FAMILY, "spot": 100.0, "lot_coin": 0.1,
    "groups": [{
        "days": 2, "date": "2026-09-20", "exp_ms": 1789948800000,
        "rows": [
            {"strike": 92.0, "P": {"inst_id": "SOL-USD_UM-260920-92-P",
                                   "bid": 0.60, "ask": 0.70, "iv": 75.0,
                                   "delta": -0.22}},
        ],
    }],
}
PARAMS = {"entry_setup": 9, "entry_countdown": 13,
          "max_contracts_per_family": 1, "max_contracts_total": 3}

SETTLED = {"id": "L1", "kind": "open_put", "family": FAMILY,
           "inst_id": "SOL-USD_UM-260929-118-P", "strike": 118.0, "sz": 1,
           "status": ot.STATUS_SETTLED_ITM, "settle_px": 116.0,
           "settle_payout": 0.2}
COVER_FILL = {"id": "S1", "kind": "spot_cover", "status": "filled",
              "spot_inst": "SOL-USD", "sz": "0.1", "filled_sz": 0.1,
              "filled_px": 116.5, "fee_usd": 0.01}
# (0.2 + 0.1×116.5 + 0.01) ÷ (0.1×1) = 118.6
EXPECT_C = 118.6


def _entry(**kw):
    args = {"td_signal": {"setup_buy": 9, "cd_buy": 0}, "params": PARAMS,
            "chain": CHAIN, "base_px": 100.0}
    args.update(kw)
    return st.evaluate_entry(FAMILY, **args)


# ══════════════════ Step 3：卖 put 现金担保前置门 ══════════════════

class TestCashGate:
    def test_not_checked_when_absent(self):
        """不传 cash_avail = 不做现金校验（回测/纯信号路径）。"""
        dec, _ = _entry()
        assert dec is not None

    def test_pass_when_enough(self):
        dec, note = _entry(cash_avail=100.0)
        assert dec is not None
        assert dec.inst_id.endswith("92-P")
        assert "现金担保" in note

    def test_boundary_equal_passes(self):
        # 全损担保 = 92 × 0.1 × 1 = 9.2（等于即放行）
        dec, _ = _entry(cash_avail=9.2)
        assert dec is not None

    def test_blocked_when_short(self):
        dec, note = _entry(cash_avail=9.0)
        assert dec is None
        assert "现金担保不足" in note
        assert "9.20" in note            # 报明所需全损担保金额

    def test_blocked_on_query_failure_sentinel(self):
        dec, note = _entry(cash_avail=-1.0)
        assert dec is None
        assert "现金担保不可得" in note


# ══════════════════ Step 4a：到期档显式化 ══════════════════

class TestSelectorWindow:
    def test_default_window_is_0_3_days(self):
        s = osl.selector_params({})
        assert s["expiry_min_days"] == 0.0
        assert s["expiry_max_days"] == 3.0

    def test_explicit_window_respected(self):
        s = osl.selector_params({"expiry_min_days": 7, "expiry_max_days": 30})
        assert (s["expiry_min_days"], s["expiry_max_days"]) == (7.0, 30.0)

    def test_no_silent_widening_when_window_empty(self, monkeypatch):
        """窗口内无在售档 → fail-closed（旧行为会静默放宽为最近 3 档并照常建仓）。"""
        called = {"fetch": 0}
        monkeypatch.setattr(od, "list_expiries", lambda fam: [
            {"days": 1.0, "exp_ms": 1}, {"days": 2.0, "exp_ms": 2},
            {"days": 10.0, "exp_ms": 10}])

        def _fetch(*a, **k):
            called["fetch"] += 1
            return {"groups": []}

        monkeypatch.setattr(od, "fetch_chain", _fetch)
        res = osl.select_puts(FAMILY, base_px=100.0,
                              selector={"expiry_min_days": 3.0,
                                        "expiry_max_days": 7.0})
        assert res["candidates"] == []
        assert "fail-closed" in res["note"]
        assert "10天" in res["note"]      # 给出在售档，便于判断该调窗口
        assert res["expiry_mode"] == "window"
        assert called["fetch"] == 0        # 压根不去拉链（不再静默放宽）

    def test_window_hit_still_selects(self, monkeypatch):
        monkeypatch.setattr(od, "list_expiries", lambda fam: [
            {"days": 2.0, "exp_ms": 2}])
        monkeypatch.setattr(od, "fetch_chain",
                            lambda fam, expiries=None: CHAIN)
        res = osl.select_puts(FAMILY, base_px=100.0,
                              selector={"expiry_min_days": 0.0,
                                        "expiry_max_days": 3.0})
        assert res["candidates"], "窗口命中时照常选档"


class TestSelectorWindowMigration:
    @staticmethod
    def _write(tmp_path, data):
        p = tmp_path / "okx_options_params.json"
        p.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        return p

    def test_legacy_default_pair_migrated_and_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ot, "params_path",
                            lambda: tmp_path / "okx_options_params.json")
        p = self._write(tmp_path, {"selector": {"expiry_min_days": 3.0,
                                                "expiry_max_days": 7.0},
                                   "cover": {"auto": False}})
        d = ot.load_option_params()
        assert (d["selector"]["expiry_min_days"],
                d["selector"]["expiry_max_days"]) == (0.0, 3.0)
        saved = json.loads(p.read_text("utf-8"))
        assert (saved["selector"]["expiry_min_days"],
                saved["selector"]["expiry_max_days"]) == (0.0, 3.0)
        assert saved["selector_window_migrated"]
        assert saved["cover"] == {"auto": False}      # 其他字段原样保留
        # 幂等：再读仍是 0–3，且不会重复改写
        p.write_text(json.dumps({"selector": {"expiry_min_days": 0.0,
                                              "expiry_max_days": 3.0},
                                 "selector_window_migrated": saved[
                                     "selector_window_migrated"]},
                                ensure_ascii=False), "utf-8")
        assert ot.load_option_params()["selector"]["expiry_min_days"] == 0.0

    def test_custom_window_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ot, "params_path",
                            lambda: tmp_path / "okx_options_params.json")
        p = self._write(tmp_path, {"selector": {"expiry_min_days": 2.0,
                                                "expiry_max_days": 5.0}})
        ot.load_option_params()
        saved = json.loads(p.read_text("utf-8"))
        assert (saved["selector"]["expiry_min_days"],
                saved["selector"]["expiry_max_days"]) == (2.0, 5.0)
        assert "selector_window_migrated" not in saved

    def test_user_reset_after_migration_not_re_migrated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ot, "params_path",
                            lambda: tmp_path / "okx_options_params.json")
        p = self._write(tmp_path, {"selector": {"expiry_min_days": 3.0,
                                                "expiry_max_days": 7.0},
                                   "selector_window_migrated": "3-7 -> 0-3 (x)"})
        ot.load_option_params()
        saved = json.loads(p.read_text("utf-8"))
        assert saved["selector"]["expiry_max_days"] == 7.0


# ══════════════════ Step 4b：卖 call 去重门 ══════════════════

class TestPendingDedup:
    def test_true_on_match(self, monkeypatch):
        monkeypatch.setattr(ot, "pending_orders", lambda a, f: [
            {"inst_id": "SOL-USD_UM-260920-92-C"}])
        assert ot.has_pending_inst("A", FAMILY, "SOL-USD_UM-260920-92-C") is True

    def test_false_when_other_inst_only(self, monkeypatch):
        monkeypatch.setattr(ot, "pending_orders", lambda a, f: [
            {"inst_id": "SOL-USD_UM-260920-92-P"}])
        assert ot.has_pending_inst("A", FAMILY, "SOL-USD_UM-260920-92-C") is False

    def test_fail_closed_on_query_error(self, monkeypatch):
        def _boom(a, f):
            raise RuntimeError("api down")

        monkeypatch.setattr(ot, "pending_orders", _boom)
        assert ot.has_pending_inst("A", FAMILY, "SOL-USD_UM-260920-92-C") is True

    def test_blank_inst_is_false(self):
        assert ot.has_pending_inst("A", FAMILY, "") is False


# ══════════════════ Step 2：成本锚 C 口径 ══════════════════

class TestCostBasis:
    @staticmethod
    def _env(tmp_path, monkeypatch, rows):
        p = tmp_path / "ledger.json"
        p.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
        monkeypatch.setattr(ot, "ledger_path", lambda: p)
        return p

    def test_write_back_uses_actual_spend(self, tmp_path, monkeypatch):
        p = self._env(tmp_path, monkeypatch, [dict(SETTLED), dict(COVER_FILL)])
        assert ot.refresh_cost_bases(FAMILY) == 1
        row = json.loads(p.read_text("utf-8"))[0]
        assert row["cost_basis"] == pytest.approx(EXPECT_C, rel=1e-6)
        assert "赔付" in row["cost_basis_note"] and "补买" in row["cost_basis_note"]
        assert row["cost_basis_ts"]

    def test_idempotent(self, tmp_path, monkeypatch):
        self._env(tmp_path, monkeypatch, [dict(SETTLED), dict(COVER_FILL)])
        assert ot.refresh_cost_bases(FAMILY) == 1
        assert ot.refresh_cost_bases(FAMILY) == 0

    def test_partial_fill_not_written(self, tmp_path, monkeypatch):
        """到货不足额（<99%）不回写 —— 保持「待补买」，仍以 K 暂计。"""
        p = self._env(tmp_path, monkeypatch,
                      [dict(SETTLED),
                       {**COVER_FILL, "sz": "0.05", "filled_sz": 0.05}])
        assert ot.refresh_cost_bases(FAMILY) == 0
        assert "cost_basis" not in json.loads(p.read_text("utf-8"))[0]

    def test_no_fill_not_written(self, tmp_path, monkeypatch):
        p = self._env(tmp_path, monkeypatch, [dict(SETTLED)])
        assert ot.refresh_cost_bases(FAMILY) == 0
        assert "cost_basis" not in json.loads(p.read_text("utf-8"))[0]

    def test_ambiguous_rows_not_attributed(self, tmp_path, monkeypatch):
        """同 base 两行待核算 + 无 ref_id 的补买 → 不猜（宁可留待人工）。"""
        p = self._env(tmp_path, monkeypatch,
                      [dict(SETTLED), {**SETTLED, "id": "L2"}, dict(COVER_FILL)])
        assert ot.refresh_cost_bases(FAMILY) == 0
        assert all("cost_basis" not in r
                   for r in json.loads(p.read_text("utf-8")) if r["id"].startswith("L"))

    def test_ref_id_binding_wins(self, tmp_path, monkeypatch):
        p = self._env(tmp_path, monkeypatch,
                      [dict(SETTLED), {**SETTLED, "id": "L2"},
                       {**COVER_FILL, "ref_id": "L2"}])
        assert ot.refresh_cost_bases(FAMILY) == 1
        by_id = {r["id"]: r for r in json.loads(p.read_text("utf-8"))}
        assert "cost_basis" in by_id["L2"]
        assert "cost_basis" not in by_id["L1"]

    def test_payout_backfilled_when_missing(self, tmp_path, monkeypatch):
        """账单未给毛赔付时按 (K−结算价)×面值×张数 反推。"""
        p = self._env(tmp_path, monkeypatch,
                      [{**SETTLED, "settle_payout": 0}, dict(COVER_FILL)])
        assert ot.refresh_cost_bases(FAMILY) == 1
        row = json.loads(p.read_text("utf-8"))[0]
        assert row["cost_basis"] == pytest.approx(EXPECT_C, rel=1e-6)

    def test_other_family_untouched(self, tmp_path, monkeypatch):
        self._env(tmp_path, monkeypatch, [dict(SETTLED), dict(COVER_FILL)])
        assert ot.refresh_cost_bases("BTC-USD_UM") == 0

    def test_covered_context_exposes_cost_and_no_pending(self, tmp_path, monkeypatch):
        self._env(tmp_path, monkeypatch, [dict(SETTLED), dict(COVER_FILL)])
        monkeypatch.setattr(ot, "account_balance", lambda a: {"details": [
            {"ccy": "SOL", "avail_bal": 0.1}, {"ccy": "USDC", "avail_bal": 5.0}]})
        ctx = ot.covered_context("A", FAMILY)
        assert ctx["cost_hint"] == pytest.approx(EXPECT_C, rel=1e-6)
        assert ctx["cost_pending"] is False
        assert ctx["cost_rows"][0]["settled"] is True
        assert ctx["sellable_sz"] == 1          # 0.1 SOL / (0.1×0.99)

    def test_covered_context_pending_falls_back_to_strike(self, tmp_path, monkeypatch):
        self._env(tmp_path, monkeypatch, [dict(SETTLED)])      # 未补买
        monkeypatch.setattr(ot, "account_balance", lambda a: {"details": []})
        ctx = ot.covered_context("A", FAMILY)
        assert ctx["cost_hint"] == 118.0
        assert ctx["cost_pending"] is True
        assert ctx["cost_rows"][0]["settled"] is False

    def test_covered_context_write_failure_does_not_break_read(self, tmp_path,
                                                               monkeypatch):
        self._env(tmp_path, monkeypatch, [dict(SETTLED), dict(COVER_FILL)])

        def _boom(family=""):
            raise RuntimeError("disk")

        monkeypatch.setattr(ot, "refresh_cost_bases", _boom)
        monkeypatch.setattr(ot, "account_balance", lambda a: {"details": []})
        ctx = ot.covered_context("A", FAMILY)      # 回写失败不影响只读上下文
        assert ctx["cost_hint"] == 118.0
        assert ctx["cost_pending"] is True
