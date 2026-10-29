"""OKX CEX 现货通道 P3-2b：shared_pool 启动对账导入（§33.52）。

覆盖：
- 同账号多 available slot 只导入 1 批（共享余额池，不重复计数）
- 账号已有 open 批次 → 不再导入（币已被那批占用）
- min_position_value 阈值 / 精度死锁（floor 到 lotSz 后 < minSz）拦截
- cost_price 优先于 ticker 兜底
- 无持仓时不落台账
"""

from __future__ import annotations

import pytest

from nanobot_quant import td_live as td_live_mod


class _FakeBroker:
    def __init__(self, base="SOL", qty=1.0, px=100.0, lot=0.0001, min_sz=0.0001,
                 err=None):
        self.base = base
        self.qty = qty
        self.px = px
        self.lot = lot
        self.min_sz = min_sz
        self.err = err
        self.bal_calls = 0

    def inst_id_of(self, symbol):
        return f"{self.base}-USDT"

    def _balances(self):
        self.bal_calls += 1
        if self.err:
            raise self.err
        return {self.base: {"available": self.qty, "locked": 0.0, "eq_usd": 0.0}}

    def _price_of(self, symbol):
        return self.px

    def limits_of(self, symbol):
        return {"lot_sz": self.lot, "min_sz": self.min_sz, "tick_sz": 0.01}


class _FakeBM:
    def __init__(self, slots):
        self.slots = slots
        self.opened = []
        self.saved = 0

    def open_lot(self, qty, entry_price, entry_time, slot):
        self.opened.append({"qty": qty, "entry_price": entry_price, "slot": slot})

    def save(self):
        self.saved += 1


class _FakeStrategy:
    def __init__(self, brokers_by_account):
        self._brokers = brokers_by_account
        self.notes = []

    def _cex_slot_broker(self, slot):
        acct = str(slot.get("account_id") or "")
        b = self._brokers.get(acct)
        if b is None:
            raise RuntimeError(f"no broker for {acct}")
        return b

    def _note_min_hold(self, symbol, slot, t):
        self.notes.append((symbol, slot))


def _runner(strategy):
    r = td_live_mod._TdLiveRunner.__new__(td_live_mod._TdLiveRunner)
    r._strategy = strategy
    return r


def _slot(n, acct, status="available"):
    return {"slot": n, "account_id": acct, "status": status}


@pytest.fixture(autouse=True)
def _no_dust(monkeypatch):
    monkeypatch.setattr(td_live_mod, "_dust_threshold", lambda: 1.0)
    monkeypatch.setattr(
        "nanobot_quant.tokens_store.token_meta", lambda sym, tj: {}
    )


def test_imports_once_per_account_across_slots(capsys):
    """同账号 2 个 available slot → 只导入 1 批（不重复计数）。"""
    broker = _FakeBroker(qty=0.5, px=100.0)
    st = _FakeStrategy({"okx_a": broker})
    bm = _FakeBM([_slot(1, "okx_a"), _slot(2, "okx_a"), _slot(3, "okx_a")])

    _runner(st)._reconcile_import_shared_pool(bm, "SOL", None)

    assert len(bm.opened) == 1
    assert bm.opened[0]["slot"] == 1
    assert bm.opened[0]["qty"] == 0.5
    assert bm.opened[0]["entry_price"] == 100.0
    assert bm.saved == 1
    assert broker.bal_calls == 1  # 同账号只查一次余额
    assert st.notes == [("SOL", 1)]


def test_imports_per_distinct_account(capsys):
    """不同账号各自导入一批（shared_pool 语义：每账号一份余额池）。"""
    st = _FakeStrategy({
        "okx_a": _FakeBroker(qty=0.5, px=100.0),
        "okx_b": _FakeBroker(qty=0.3, px=100.0),
    })
    bm = _FakeBM([_slot(1, "okx_a"), _slot(2, "okx_b")])

    _runner(st)._reconcile_import_shared_pool(bm, "SOL", None)

    assert len(bm.opened) == 2
    assert [o["slot"] for o in bm.opened] == [1, 2]


def test_skips_account_with_open_lot(capsys):
    """账号已有 open 批次 → 不再导入（币已被那批占用）。"""
    broker = _FakeBroker(qty=0.5, px=100.0)
    st = _FakeStrategy({"okx_a": broker})
    bm = _FakeBM([_slot(1, "okx_a", "open"), _slot(2, "okx_a")])

    _runner(st)._reconcile_import_shared_pool(bm, "SOL", None)

    assert bm.opened == []
    assert broker.bal_calls == 0


def test_skips_below_min_position_value(capsys):
    """$0.5 < $1 阈值 → 不导入（dust 不占槽位）。"""
    st = _FakeStrategy({"okx_a": _FakeBroker(qty=0.005, px=100.0)})
    bm = _FakeBM([_slot(1, "okx_a")])

    _runner(st)._reconcile_import_shared_pool(bm, "SOL", None)

    assert bm.opened == []
    assert "dust" in capsys.readouterr().err


def test_skips_precision_deadlock(capsys):
    """floor 到 lotSz 后 < minSz → 不导入（防「有仓卖不掉」死 slot）。"""
    # 余额 0.00005 SOL，lotSz 0.0001 → floor 后 0 < minSz 0.0001
    st = _FakeStrategy({"okx_a": _FakeBroker(qty=0.00005, px=100000.0,
                                            lot=0.0001, min_sz=0.0001)})
    bm = _FakeBM([_slot(1, "okx_a")])

    _runner(st)._reconcile_import_shared_pool(bm, "SOL", None)

    assert bm.opened == []
    assert "精度死锁" in capsys.readouterr().err


def test_prefers_cost_price(monkeypatch):
    """tokens.json 有 cost_price 时用它作 entry_price（不取 ticker 价）。"""
    monkeypatch.setattr(
        "nanobot_quant.tokens_store.token_meta",
        lambda sym, tj: {"cost_price": 88.0},
    )
    st = _FakeStrategy({"okx_a": _FakeBroker(qty=0.5, px=100.0)})
    bm = _FakeBM([_slot(1, "okx_a")])

    _runner(st)._reconcile_import_shared_pool(bm, "SOL", [{"symbol": "SOL"}])

    assert bm.opened[0]["entry_price"] == 88.0


def test_no_position_logs_and_no_save(capsys):
    st = _FakeStrategy({"okx_a": _FakeBroker(qty=0.0)})
    bm = _FakeBM([_slot(1, "okx_a")])

    _runner(st)._reconcile_import_shared_pool(bm, "SOL", None)

    assert bm.opened == []
    assert bm.saved == 0
    assert "无天然持仓" in capsys.readouterr().err


def test_balance_error_reported_not_raised(capsys):
    """余额查询异常只报错跳过，不炸整个对账。"""
    st = _FakeStrategy({"okx_a": _FakeBroker(err=RuntimeError("boom"))})
    bm = _FakeBM([_slot(1, "okx_a")])

    _runner(st)._reconcile_import_shared_pool(bm, "SOL", None)

    assert bm.opened == []
    assert "余额查询失败" in capsys.readouterr().err
