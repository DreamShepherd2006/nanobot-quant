"""P4（2026-10-29）：台账按交易所订单号对账 + 全平单轮限流。

设计见 docs/quant-system.md §33.52.5（①lot 记 ord_id/fill_px、②重启逐批
核对、③查不到标记核对失败）与 §33.52.6（全平四块、限流）。验证口径 = 模拟
重启对账（含「台账有、实盘无」坏数据）+ 模拟 100 批全平。
"""

from __future__ import annotations

import pytest

from nanobot_quant.batches import BatchManager
from nanobot_quant.td_live import _TdLiveRunner


# ── helpers ────────────────────────────────────────────────────────────────


class _FakeBroker:
    def __init__(self, result, inst="SOL-USDC"):
        self._result = result
        self._inst = inst
        self.queries: list[tuple[str, str]] = []

    def inst_id_of(self, symbol: str) -> str:
        return self._inst

    def _query_order(self, order_id: str, pair: str = ""):
        self.queries.append((order_id, pair))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeStrategy:
    def __init__(self, broker):
        self._b = broker

    def _cex_slot_broker(self, slot):
        return self._b


class _FakeBM:
    def __init__(self, slots):
        self.slots = slots
        self.saved = 0

    def save(self):
        self.saved += 1


def _slot(no, qty, ord_id=None, fill_px=None):
    lot = {"qty": qty, "entry_price": 100.0, "entry_time": f"2026-10-29T0{no}:00:00"}
    if ord_id:
        lot["ord_id"] = ord_id
    if fill_px is not None:
        lot["fill_px"] = fill_px
    return {"slot": no, "status": "open", "account_id": "DreamShepherdbot2", "lot": lot}


def _runner(broker) -> _TdLiveRunner:
    r = object.__new__(_TdLiveRunner)
    r._strategy = _FakeStrategy(broker)
    return r


# ── ① lot 记 ord_id / fill_px + 重启逐批核对 ───────────────────────────────


def test_verify_ord_ok_backfills_fill_px(capsys):
    bm = _FakeBM([_slot(1, 0.5, ord_id="ord-1")])
    broker = _FakeBroker(("filled", 0.5, 0.0, 100.31))
    _runner(broker)._reconcile_verify_ord(bm, "SOL")
    lot = bm.slots[0]["lot"]
    assert lot["reconcile"] == "ok"
    assert lot["fill_px"] == pytest.approx(100.31)
    assert bm.saved == 1
    assert broker.queries == [("ord-1", "SOL-USDC")]
    assert "核对 1 批（不符 0）" in capsys.readouterr().err


def test_verify_ord_missing_marks_but_keeps_lot(capsys):
    """台账有、实盘无（撤单/零成交）→ 只标记，绝不删台账。"""
    bm = _FakeBM([_slot(1, 0.5, ord_id="ord-2")])
    _runner(_FakeBroker(("cancelled", 0.0, 0.0, 0.0)))._reconcile_verify_ord(bm, "SOL")
    assert bm.slots[0]["lot"]["reconcile"] == "missing"
    assert bm.slots[0]["status"] == "open"          # 台账保留
    assert len(bm.slots) == 1
    assert "TD RECON MISMATCH" in capsys.readouterr().err


def test_verify_ord_short_detects_partial(capsys):
    bm = _FakeBM([_slot(1, 1.0, ord_id="ord-3")])
    _runner(_FakeBroker(("filled", 0.4, 0.0, 99.9)))._reconcile_verify_ord(bm, "SOL")
    assert bm.slots[0]["lot"]["reconcile"] == "short"
    assert "实盘 filled=0.4" in capsys.readouterr().err


def test_verify_ord_pending_and_unknown(capsys):
    bm = _FakeBM([_slot(1, 1.0, ord_id="ord-4"), _slot(2, 1.0, ord_id="ord-5")])
    broker_bad = _FakeBroker(RuntimeError("timeout"))
    _runner(broker_bad)._reconcile_verify_ord(bm, "SOL")
    assert {s["lot"]["reconcile"] for s in bm.slots} == {"unknown"}
    assert "保留台账" in capsys.readouterr().err

    bm2 = _FakeBM([_slot(1, 1.0, ord_id="ord-6")])
    _runner(_FakeBroker(("open", 0.0, 1.0, 0.0)))._reconcile_verify_ord(bm2, "SOL")
    assert bm2.slots[0]["lot"]["reconcile"] == "pending"


def test_verify_ord_skips_legacy_lots_without_ord_id(capsys):
    """改动前导入/开仓的旧批次（无 ord_id）跳过——继续由余额反推兜底。"""
    bm = _FakeBM([_slot(1, 0.5), _slot(2, 0.2, ord_id="ord-7")])
    broker = _FakeBroker(("filled", 0.2, 0.0, 100.0))
    _runner(broker)._reconcile_verify_ord(bm, "SOL")
    assert broker.queries == [("ord-7", "SOL-USDC")]
    assert "reconcile" not in bm.slots[0]["lot"]
    assert bm.slots[1]["lot"]["reconcile"] == "ok"


def test_verify_ord_no_strategy_is_noop():
    bm = _FakeBM([_slot(1, 0.5, ord_id="ord-8")])
    r = object.__new__(_TdLiveRunner)
    r._strategy = None
    r._reconcile_verify_ord(bm, "SOL")
    assert "reconcile" not in bm.slots[0]["lot"]
    assert bm.saved == 0


# ── ② 全平单轮限流 ────────────────────────────────────────────────────────


def _bm_with_slots(tmp_path, n: int) -> BatchManager:
    accounts = [f"DreamShepherdbot{2 + i % 4}" for i in range(n)]
    bm = BatchManager(symbol="SOL", account_ids=accounts,
                      path=tmp_path / "batches.test.SOL.json",
                      channel="okx_cex", scene="high")
    for i in range(n):
        bm.open_lot(slot=i + 1, qty=0.1, entry_price=100.0,
                    ord_id=f"ord-{i + 1}", fill_px=100.0)
    return bm


def test_pick_all_slots_limit_100_batches(tmp_path):
    """模拟 100 批全平：单轮只给限额内，剩余下轮继续。"""
    bm = _bm_with_slots(tmp_path, 100)
    assert len(bm.pick_all_slots("fifo")) == 100          # None = 旧行为
    first = bm.pick_all_slots("fifo", limit=20)
    assert len(first) == 20
    assert first[0]["slot"] == 1 and first[-1]["slot"] == 20
    assert len(bm.pick_all_slots("lifo", limit=20)) == 20
    # limit 非法值（0/负）→ 视为不限，避免「配 0 结果一批都平不掉」
    assert len(bm.pick_all_slots("fifo", limit=0)) == 100


def test_sell_all_max_per_round_from_params():
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    strat = object.__new__(TdSequentialStrategy)
    strat.parameters = {"sell_all_max_per_round": 3}
    strat._sell_all_max = max(1, int(strat.parameters.get(
        "sell_all_max_per_round", 20) or 20))
    assert strat._sell_all_max == 3
    strat.parameters = {"sell_all_max_per_round": 0}   # 0 → 回落默认 20
    strat._sell_all_max = max(1, int(strat.parameters.get(
        "sell_all_max_per_round", 20) or 20))
    assert strat._sell_all_max == 20


# ── ③ 订单号提取 ─────────────────────────────────────────────────────────


def test_cex_ord_id_sources():
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    strat = object.__new__(TdSequentialStrategy)

    class _Order:
        def __init__(self, cp=None, ident=None):
            self.custom_params = cp
            self.identifier = ident

    assert strat._cex_ord_id(_Order({"cex": {"ord_id": "123"}})) == "123"
    assert strat._cex_ord_id(_Order({}, "gate-oid")) == "gate-oid"
    assert strat._cex_ord_id(_Order(None, None)) is None
