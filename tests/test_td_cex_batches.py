"""TD 分批 CEX 通道（Step 1，2026-08-17，docs/quant-system.md §19）。

覆盖（批次状态机复用，只换「钱在哪、怎么下单」）：
- channel_family=cex 判定与 min_hold=0（交易所无 gas）
- _buy_on_slot_cex：pv_slot 风控 / 资金不足跳过 / position_limit BLOCK /
  下单成功 / error 不建仓 / pending 不 open_lot（fail-safe）
- _sell_lot_cex：filled 平仓 / pending 台账保持 open / error 重试 /
  无持仓释放幽灵批次 / 缩量卖出
- pending 确认循环跳过 CEX（Step 2 实现）
- td_live._prepare_batches：cex 用 slot_map（gate_botN）、DEX 台账自动
  .bak 快照迁移
"""

from __future__ import annotations

import logging
import json

import pandas as pd
import pytest

from nanobot_quant.batches import BatchManager
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _bars_with(closes: list[float]):
    from lumibot.entities import Bars

    # 小写列 = lumibot v4.5.78 Bars 契约（Bars.__init__ 访问 df["close"] 派生
    # return 列）；CexDataSource 修复后输出小写列，测试 mock 须同契约
    # （2026-08-17 A 修复）。
    df = pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes],
         "low": [c - 1 for c in closes], "close": closes,
         "volume": [1_000_000] * len(closes)},
        index=pd.date_range("2025-01-01", periods=len(closes), freq="D"),
    )
    return Bars(df, "ONCHAIN", None)


def _oscillate() -> list[float]:
    return [100.0 + (i % 2) * 2 for i in range(41)]


def _buy_closes() -> list[float]:
    return _oscillate() + [100.0 - i for i in range(1, 14)]


def _mock_order(identifier="cex-order-1", quantity=1.0, filled=True, error=None):
    return type("Order", (), {
        "identifier": identifier,
        "quantity": quantity,
        "error": error,
        "custom_params": None,
        "is_filled": lambda self=None: filled,
        "set_error": lambda self, e: setattr(self, "error", e),
    })()


class _Req:
    """PortfolioEngine.build_*_order 返回的 OrderRequest 最小 stub。"""

    def __init__(self, asset=None, quantity=1.0, action="buy"):
        self.asset = asset
        self.quantity = quantity
        self.action = action


def _make_cex_strategy(bm: BatchManager, bars, **params) -> TdSequentialStrategy:
    """构造 CEX 通道策略（mock 子账号 broker / 余额 / 下单）。"""
    params.setdefault("min_history", 50)
    params.setdefault("channel_family", "cex")
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, **params)
    s.logger = logging.getLogger("td-cex-test")
    s.portfolio_value = 100_000.0
    s.cash = 100_000.0
    s._bars = bars
    s.get_position = lambda symbol: None
    s.get_historical_prices = lambda symbol, length, timestep: s._bars

    captured: dict = {"submitted": []}

    def _create_order(asset, quantity, action):
        captured["order"] = (asset, quantity, action)
        return _mock_order(quantity=quantity)

    s.create_order = _create_order
    s.batch_manager = bm
    s.initialize()
    # CEX 子账号 mock（单测不触网络/不触 load_slot_map）
    s._cex_brokers = {}
    s._cex_submit = lambda slot, req: captured["submitted"].append(
        _mock_order(quantity=req.quantity)
    ) or _mock_order(quantity=req.quantity)
    s._cex_slot_balances = lambda slot, use_snapshot=False: {"USDT": {"available": 1e9, "locked": 0}}
    s._captured = captured
    return s


def _make_bm(tmp_path, n: int = 3, account_ids=None) -> BatchManager:
    ids = account_ids or [f"gate_bot{i}" for i in range(1, n + 1)]
    return BatchManager(
        symbol="CRCLX",
        account_ids=ids,
        path=tmp_path / "batches.json",
    )


# ── 通道判定 / min_hold ─────────────────────────────────────────────

def test_is_cex_detection(tmp_path):
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60), channel_family="cex")
    assert s._is_cex() is True
    s2 = _make_cex_strategy(bm, _bars_with([100.0] * 60), channel_family="dex")
    assert s2._is_cex() is False


def test_symbol_min_hold_zero_in_cex(tmp_path):
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(
        bm, _bars_with([100.0] * 60),
        tokens_json=[{"symbol": "CRCLX", "min_hold": 0.01}],
    )
    assert s._symbol_min_hold() == 0.0  # 交易所无 gas 保留


# ── BUY（_buy_on_slot → CEX 分支）───────────────────────────────────

def test_cex_buy_success_opens_slot(tmp_path):
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(bm, _bars_with(_buy_closes()))
    slot = bm.available_slots()[0]
    result = s._buy_on_slot(slot, price=70.0, reason="setup_buy")
    assert result is not None
    order, qty = result
    assert qty > 0
    assert len(s._captured["submitted"]) == 1


def test_cex_buy_full_loop_opens_lot(tmp_path):
    """完整循环（on_trading_iteration）：filled → 调用方 open_lot。"""
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(bm, _bars_with(_buy_closes()))
    s.on_trading_iteration()
    assert bm.open_slots() != []
    assert bm.open_slots()[0]["lot"]["qty"] > 0


def test_cex_buy_insufficient_funds_skips(tmp_path):
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(bm, _bars_with(_buy_closes()))
    s._cex_slot_balances = lambda slot, use_snapshot=False: {"USDT": {"available": 1.0, "locked": 0}}
    slot = bm.available_slots()[0]
    result = s._buy_on_slot(slot, price=70.0, reason="setup_buy")
    assert result is None  # 资金不足 → 跳过，不建仓
    assert len(s._captured["submitted"]) == 0


def test_cex_buy_position_limit_block(tmp_path):
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(
        bm, _bars_with(_buy_closes()),
        max_position_pct=0.01,  # 1% 上限 → 大仓位 BLOCK
    )
    s._cex_slot_portfolio_value = lambda slot, use_snapshot=False: 100.0  # 小资产账户
    slot = bm.available_slots()[0]
    result = s._buy_on_slot(slot, price=70.0, reason="setup_buy")
    assert result is None
    assert len(s._captured["submitted"]) == 0


def test_cex_buy_fixed_amount_skips_position_limit(tmp_path):
    """CEX fixed_amount 跳过 position_limit（拍板 A）：固定 100U > 25%×pv_slot(11.45)
    仍买入；资金检查保留（子账号余额充足 → 成功）。"""
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(
        bm, _bars_with(_buy_closes()),
        quantity_mode="fixed_amount", td_fixed_amount=100.0,
        max_position_pct=0.25,
    )
    s._cex_slot_portfolio_value = lambda slot, use_snapshot=False: 11.45  # 小账号：100U 远超 25% 上限
    s.on_trading_iteration()
    assert len(s._captured["submitted"]) == 1
    assert len(bm.open_slots()) == 1


def test_cex_buy_fixed_amount_insufficient_funds(tmp_path):
    """CEX fixed_amount 资金检查保留：USDT 余额 < 固定金额 → SKIP 不建仓。"""
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(
        bm, _bars_with(_buy_closes()),
        quantity_mode="fixed_amount", td_fixed_amount=10.0,
    )
    s._cex_slot_balances = lambda slot, use_snapshot=False: {"USDT": {"available": 5.0, "locked": 0}}
    s.on_trading_iteration()
    assert s._captured["submitted"] == []
    assert bm.open_slots() == []


def test_cex_buy_order_error_no_open(tmp_path):
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(bm, _bars_with(_buy_closes()))
    s._cex_submit = lambda slot, req: _mock_order(error="[51000] bad")
    slot = bm.available_slots()[0]
    result = s._buy_on_slot(slot, price=70.0, reason="setup_buy")
    assert result is None  # 下单失败不建仓（防幽灵 lot）
    assert bm.open_slots() == []


def test_cex_buy_pending_not_open_by_caller(tmp_path):
    """BUY pending（5s 未 filled）→ 调用方不 open_lot（fail-safe，拍板点 4）。"""
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(bm, _bars_with(_buy_closes()))
    s._cex_submit = lambda slot, req: _mock_order(filled=False)  # pending
    s.on_trading_iteration()
    assert bm.open_slots() == []  # 未确认 → 不建仓
    assert len(s._pending_buys) == 1
    info = next(iter(s._pending_buys.values()))
    assert info["cex"] is True
    assert info["order_id"] == "cex-order-1"  # CEX：order_id 来自 identifier


def test_cex_long_records_actual_price(tmp_path, monkeypatch):
    """CEX LONG 事件携带实际成交均价（Gate avg_deal_price 回填）——交易记录「成交价」列。"""
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(bm, _bars_with(_buy_closes()))
    events = []
    monkeypatch.setattr(
        "nanobot_quant.td_live_state.append_event", lambda e: events.append(e)
    )
    s.parameters["live_mode"] = True

    def _submit(slot, req):
        o = _mock_order(quantity=req.quantity)
        o.custom_params = {"cex": {"pair": "CRCLX_USDT", "avg_price": 1.3029}}
        return o

    s._cex_submit = _submit
    s.on_trading_iteration()
    long_events = [e for e in events if e["event"] == "LONG"]
    assert long_events
    assert long_events[0]["actual_price"] == 1.3029
    assert long_events[0]["price"] > 0  # 策略价仍在


def test_cex_avg_price_helper():
    """_cex_avg_price：无 cex 字段 / avg=0 / 空值 → None；有效值 → float。"""
    bm = BatchManager(symbol="CRCLX", account_ids=["gate_bot1"], path="/tmp/x.json")
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60))

    class _O:
        def __init__(self, cp):
            self.custom_params = cp

    assert s._cex_avg_price(_O(None)) is None            # custom_params=None
    assert s._cex_avg_price(_O({})) is None               # 空
    assert s._cex_avg_price(_O({"cex": {}})) is None     # 无 avg_price
    assert s._cex_avg_price(_O({"cex": {"avg_price": 0}})) is None     # 0
    assert s._cex_avg_price(_O({"cex": {"avg_price": ""}})) is None   # 空串
    assert s._cex_avg_price(_O({"cex": {"avg_price": "1.3029"}})) == 1.3029  # 字符串转 float
    assert s._cex_avg_price(_O({"cex": {"avg_price": 74.9}})) == 74.9
    # DEX order（onchain_pending 无 cex）→ None
    assert s._cex_avg_price(_O({"onchain_pending": {"tx_hash": "x"}})) is None


# ── SELL（_sell_lot → CEX 分支）─────────────────────────────────────

def test_cex_sell_filled_closes_lot(tmp_path):
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=0.05, entry_price=70.0, entry_time="t1")
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60))
    s._cex_slot_token_balance = lambda slot, symbol: 0.05
    s._sell_lot(
        bm.open_slots()[0], price=72.0,
        signal={"recommendation": "SELL"}, exit_reason="setup_sell",
    )
    assert bm.get_lot(1) is None  # filled → 平仓
    assert 1 not in s._pending_sells


def test_cex_exit_records_actual_price(tmp_path, monkeypatch):
    """CEX EXIT 事件携带实际成交均价（Gate avg_deal_price 回填）——交易记录「成交价」列。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=0.05, entry_price=70.0, entry_time="t1")
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60))
    events = []
    monkeypatch.setattr(
        "nanobot_quant.td_live_state.append_event", lambda e: events.append(e)
    )
    s.parameters["live_mode"] = True
    s._cex_slot_token_balance = lambda slot, symbol: 0.05

    def _submit(slot, req):
        o = _mock_order(quantity=req.quantity)
        o.custom_params = {"cex": {"pair": "CRCLX_USDT", "avg_price": 70.5}}
        return o

    s._cex_submit = _submit
    s._sell_lot(
        bm.open_slots()[0], price=72.0,
        signal={"recommendation": "SELL"}, exit_reason="setup_sell",
    )
    exit_events = [e for e in events if e["event"] == "EXIT"]
    assert exit_events
    assert exit_events[0]["actual_price"] == 70.5


def test_cex_sell_pending_keeps_lot(tmp_path):
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=0.05, entry_price=70.0, entry_time="t1")
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60))
    s._cex_slot_token_balance = lambda slot, symbol: 0.05
    s._cex_submit = lambda slot, req: _mock_order(filled=False)  # pending
    s._sell_lot(
        bm.open_slots()[0], price=72.0,
        signal={"recommendation": "SELL"}, exit_reason="setup_sell",
    )
    assert bm.get_lot(1) is not None  # 台账保持 open（防账实脱节）
    assert 1 in s._pending_sells
    assert s._pending_sells[1]["cex"] is True


def test_cex_sell_error_keeps_lot(tmp_path):
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=0.05, entry_price=70.0, entry_time="t1")
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60))
    s._cex_slot_token_balance = lambda slot, symbol: 0.05
    s._cex_submit = lambda slot, req: _mock_order(error="[52001] Insufficient")
    s._sell_lot(
        bm.open_slots()[0], price=72.0,
        signal={"recommendation": "SELL"}, exit_reason="setup_sell",
    )
    assert bm.get_lot(1) is not None  # 失败 → 台账保留可重试
    assert 1 not in s._pending_sells


def test_cex_sell_empty_releases_lot(tmp_path):
    """子账号无持仓 → 幽灵批次释放台账（与 DEX 对称）。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=0.05, entry_price=70.0, entry_time="t1")
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60))
    s._cex_slot_token_balance = lambda slot, symbol: 0.0
    s._sell_lot(
        bm.open_slots()[0], price=72.0,
        signal={"recommendation": "SELL"}, exit_reason="setup_sell",
    )
    assert bm.get_lot(1) is None
    assert len(s._captured["submitted"]) == 0  # 无持仓不卖


def test_cex_sell_shrink(tmp_path):
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=0.05, entry_price=70.0, entry_time="t1")
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60))
    s._cex_slot_token_balance = lambda slot, symbol: 0.02  # 余额 < 台账
    s._sell_lot(
        bm.open_slots()[0], price=72.0,
        signal={"recommendation": "SELL"}, exit_reason="setup_sell",
    )
    assert bm.get_lot(1) is None  # 缩量卖出后平仓
    assert s._captured["submitted"][0].quantity == 0.02  # 缩量后下单数量


# ── _cex_slot_token_balance 键映射（2026-08-26 修复）──────────────────
# 回归：gate_symbol 是完整 pair（CRCLXUSDT/CRCLX_USDT）时，持仓检查
# 仍命中 Gate 余额的基础币键（"CRCLX"），不得误判无持仓释放真实台账。


def test_cex_token_balance_gate_symbol_pair(tmp_path):
    """gate_symbol 为无分隔 pair（CRCLXUSDT）→ 命中基础币键。"""
    bm = _make_bm(tmp_path, n=2)
    s = _make_cex_strategy(
        bm, _bars_with([100.0] * 60),
        tokens_json=[{"symbol": "CRCLX", "gate_symbol": "CRCLXUSDT"}],
    )
    s._cex_slot_balances = lambda slot, use_snapshot=False: {
        "USDT": {"available": 3.0, "locked": 0},
        "CRCLX": {"available": 0.045, "locked": 0},
    }
    assert s._cex_slot_token_balance(bm.slots[0], "CRCLX") == 0.045


def test_cex_token_balance_gate_symbol_underscore(tmp_path):
    """gate_symbol 为下划线 pair（CRCLX_USDT）→ 同样命中基础币键。"""
    bm = _make_bm(tmp_path, n=2)
    s = _make_cex_strategy(
        bm, _bars_with([100.0] * 60),
        tokens_json=[{"symbol": "CRCLX", "gate_symbol": "CRCLX_USDT"}],
    )
    s._cex_slot_balances = lambda slot, use_snapshot=False: {
        "USDT": {"available": 3.0, "locked": 0},
        "CRCLX": {"available": 0.045, "locked": 0},
    }
    assert s._cex_slot_token_balance(bm.slots[0], "CRCLX") == 0.045


def test_cex_token_balance_fallback_symbol(tmp_path):
    """无 gate_symbol → 回退 symbol 作余额键（现有行为回归）。"""
    bm = _make_bm(tmp_path, n=2)
    s = _make_cex_strategy(
        bm, _bars_with([100.0] * 60),
        tokens_json=[{"symbol": "SOL"}],
    )
    s._cex_slot_balances = lambda slot, use_snapshot=False: {
        "USDT": {"available": 3.0, "locked": 0},
        "SOL": {"available": 0.5, "locked": 0},
    }
    assert s._cex_slot_token_balance(bm.slots[0], "SOL") == 0.5


def test_cex_token_balance_not_found_zero(tmp_path):
    """余额无该币 → 返回 0（fail-closed，不误报持仓）。"""
    bm = _make_bm(tmp_path, n=2)
    s = _make_cex_strategy(
        bm, _bars_with([100.0] * 60),
        tokens_json=[{"symbol": "CRCLX", "gate_symbol": "CRCLXUSDT"}],
    )
    s._cex_slot_balances = lambda slot, use_snapshot=False: {"USDT": {"available": 3.0, "locked": 0}}
    assert s._cex_slot_token_balance(bm.slots[0], "CRCLX") == 0.0


# ── pending 确认循环跳过 CEX（Step 2 实现）──────────────────────────

def test_pending_confirmation_skips_cex(tmp_path):
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60))
    s._pending_sells[1] = {"slot": 1, "cex": True, "order_id": "x"}
    s._pending_buys[1] = {"slot": 1, "cex": True, "order_id": "y"}
    s._check_pending_confirmations()  # 不查询、不抛错（Step 2 补确认）
    assert s._pending_sells[1]["cex"] is True
    assert s._pending_buys[1]["cex"] is True


# ── td_live._prepare_batches 通道化 ─────────────────────────────────

def test_prepare_batches_cex_uses_slot_map(tmp_path, monkeypatch):
    from nanobot_quant import td_live as td_live_mod

    loader = td_live_mod._TdLiveRunner()
    monkeypatch.setattr(
        "nanobot_quant.gate_credentials.load_slot_map",
        lambda: {"1": "gate_bot1", "2": "gate_bot2"},
    )
    monkeypatch.setattr(
        "nanobot_quant.batches.batches_path",
        lambda s=None, c=None, scene=None: tmp_path / (f"batches.{c}.{scene}.{s}.json" if scene else f"batches.{c}.{s}.json" if c else f"batches.{s}.json"),
    )
    bm = loader._prepare_batches(2, "CRCLX", channel="cex")
    assert bm is not None
    assert [s["account_id"] for s in bm.slots] == ["gate_bot1", "gate_bot2"]


def test_prepare_batches_cex_fallback_slot_map(tmp_path, monkeypatch):
    """slot_map 缺失条目 → 按 gate_botN 兜底。"""
    from nanobot_quant import td_live as td_live_mod

    loader = td_live_mod._TdLiveRunner()
    monkeypatch.setattr(
        "nanobot_quant.gate_credentials.load_slot_map", lambda: {}
    )
    monkeypatch.setattr(
        "nanobot_quant.batches.batches_path",
        lambda s=None, c=None, scene=None: tmp_path / (f"batches.{c}.{scene}.{s}.json" if scene else f"batches.{c}.{s}.json" if c else f"batches.{s}.json"),
    )
    bm = loader._prepare_batches(3, "CRCLX", channel="cex")
    assert [s["account_id"] for s in bm.slots] == [
        "gate_bot1", "gate_bot2", "gate_bot3",
    ]


def test_prepare_batches_cex_keeps_dex_ledger(tmp_path, monkeypatch):
    """DEX 台账（无通道旧格式）→ cex 通道：归 okx_dex 保留，gate 台账独立新建。"""
    from nanobot_quant import td_live as td_live_mod

    loader = td_live_mod._TdLiveRunner()
    # 旧格式 DEX 台账（无通道前缀）
    dex_bm = BatchManager(
        symbol="CRCLX",
        account_ids=["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],
        path=tmp_path / "batches.CRCLX.json",
    )
    dex_bm.open_lot(qty=0.05, entry_price=70.0, entry_time="t1")
    dex_bm.save()
    monkeypatch.setattr(
        "nanobot_quant.gate_credentials.load_slot_map",
        lambda: {"1": "gate_bot1", "2": "gate_bot2"},
    )
    monkeypatch.setattr(
        "nanobot_quant.batches.batches_path",
        lambda s=None, c=None, scene=None: tmp_path / (f"batches.{c}.{scene}.{s}.json" if scene else f"batches.{c}.{s}.json" if c else f"batches.{s}.json"),
    )
    bm = loader._prepare_batches(2, "CRCLX", channel="cex")
    assert [s["account_id"] for s in bm.slots] == ["gate_bot1", "gate_bot2"]
    assert bm.open_slots() == []  # 新台账不含 DEX 历史仓位
    # DEX 台账原地保留（迁移到 okx_dex 命名空间，不被 cex 复用）
    migrated = tmp_path / "batches.okx_dex.CRCLX.json"
    assert migrated.exists()
    data = json.loads(migrated.read_text())
    assert data["slots"][0]["account_id"].startswith("aaaaaaaa")
    # gate 台账文件独立
    assert (tmp_path / "batches.gate.CRCLX.json").exists()


# ── S3a 场景池子变更检测（2026-08-20） ─────────────────────────────

def test_prepare_batches_scene_pool_change_rebuilds(tmp_path, monkeypatch):
    """场景池子变更（台账 slot 账号 ≠ 场景 sub_accounts）→ 无 open 快照重建。

    high 从 gate_bot1-5 收窄到 gate_bot1-2：台账 5 slots → 2 slots，
    旧台账快照 .scene.bak.* 保留可追溯。
    """
    from nanobot_quant import td_live as td_live_mod

    loader = td_live_mod._TdLiveRunner()
    bm0 = BatchManager(
        symbol="CRCLX",
        account_ids=[f"gate_bot{i}" for i in range(1, 6)],
        path=tmp_path / "batches.gate.CRCLX.json",
    )
    bm0.save()
    monkeypatch.setattr(
        "nanobot_quant.batches.batches_path",
        lambda s=None, c=None, scene=None: tmp_path / (f"batches.{c}.{scene}.{s}.json" if scene else f"batches.{c}.{s}.json" if c else f"batches.{s}.json"),
    )
    bm = loader._prepare_batches(
        2, "CRCLX", channel="cex",
        account_ids=["gate_bot1", "gate_bot2"],
    )
    assert [s["account_id"] for s in bm.slots] == ["gate_bot1", "gate_bot2"]
    assert len(list(tmp_path.glob("batches.gate.CRCLX.json.scene.bak.*"))) == 1


def test_prepare_batches_scene_pool_change_open_lot_fail_closed(tmp_path, monkeypatch):
    """场景池子变更但台账有 open lot → fail-closed 拒绝重建（不丢台账）。"""
    from nanobot_quant import td_live as td_live_mod

    loader = td_live_mod._TdLiveRunner()
    bm0 = BatchManager(
        symbol="CRCLX",
        account_ids=[f"gate_bot{i}" for i in range(1, 4)],
        path=tmp_path / "batches.gate.CRCLX.json",
    )
    bm0.open_lot(qty=0.05, entry_price=70.0, entry_time="t1")
    bm0.save()
    monkeypatch.setattr(
        "nanobot_quant.batches.batches_path",
        lambda s=None, c=None, scene=None: tmp_path / (f"batches.{c}.{scene}.{s}.json" if scene else f"batches.{c}.{s}.json" if c else f"batches.{s}.json"),
    )
    bm = loader._prepare_batches(
        2, "CRCLX", channel="cex",
        account_ids=["gate_bot1", "gate_bot2"],
    )
    # 保留原台账（含 open lot），不重建
    assert [s["account_id"] for s in bm.slots] == [
        "gate_bot1", "gate_bot2", "gate_bot3",
    ]
    assert bm.open_slots() != []
    assert list(tmp_path.glob("batches.gate.CRCLX.json.scene.bak.*")) == []


def test_prepare_batches_scene_pool_unchanged_keeps(tmp_path, monkeypatch):
    """场景池子与台账一致 → 直接复用，不重建（幂等）。"""
    from nanobot_quant import td_live as td_live_mod

    loader = td_live_mod._TdLiveRunner()
    bm0 = BatchManager(
        symbol="CRCLX",
        account_ids=["gate_bot1", "gate_bot2"],
        path=tmp_path / "batches.gate.CRCLX.json",
    )
    bm0.save()
    monkeypatch.setattr(
        "nanobot_quant.batches.batches_path",
        lambda s=None, c=None, scene=None: tmp_path / (f"batches.{c}.{scene}.{s}.json" if scene else f"batches.{c}.{s}.json" if c else f"batches.{s}.json"),
    )
    bm = loader._prepare_batches(
        2, "CRCLX", channel="cex",
        account_ids=["gate_bot1", "gate_bot2"],
    )
    assert [s["account_id"] for s in bm.slots] == ["gate_bot1", "gate_bot2"]
    assert list(tmp_path.glob("batches.gate.CRCLX.json.scene.bak.*")) == []


def test_prepare_batches_dex_keeps_cex_ledger(tmp_path, monkeypatch):
    """CEX 台账 → 切回 dex：gate 文件保留，dex 台账独立新建/复用。"""
    from nanobot_quant import td_live as td_live_mod

    loader = td_live_mod._TdLiveRunner()
    cex_bm = BatchManager(
        symbol="CRCLX",
        account_ids=["gate_bot1", "gate_bot2"],
        path=tmp_path / "batches.gate.CRCLX.json",
    )
    cex_bm.open_lot(qty=0.05, entry_price=70.0, entry_time="t1")
    cex_bm.save()
    monkeypatch.setattr(
        "nanobot_quant.batches.batches_path",
        lambda s=None, c=None, scene=None: tmp_path / (f"batches.{c}.{scene}.{s}.json" if scene else f"batches.{c}.{s}.json" if c else f"batches.{s}.json"),
    )
    monkeypatch.setattr(
        "nanobot_quant.tools.tools_wallet.wallet_accounts",
        lambda: {"status": "ok", "data": {"accounts": [
            {"account_id": "uuid-1"}, {"account_id": "uuid-2"},
        ]}},
    )
    bm = loader._prepare_batches(2, "CRCLX", channel="dex")
    assert [s["account_id"] for s in bm.slots] == ["uuid-1", "uuid-2"]
    # gate 台账保留（未被 dex 通道快照/删除）
    assert (tmp_path / "batches.gate.CRCLX.json").exists()
    data = json.loads((tmp_path / "batches.gate.CRCLX.json").read_text())
    assert data["slots"][0]["account_id"] == "gate_bot1"

# ── S3a 场景周期 → K 线粒度精确映射（2026-08-20） ─────────────────

def test_scene_timestep_exact_granularity():
    """场景 sleeptime 必须映射到精确 K 线粒度，不能笼统成 minute。

    S3a 多场景下 mid=5m 传 "minute" 会被数据源 _BAR_MAP 丢失成 1m——
    窗口粒度与场景周期不匹配（回归：SPYX mid=5m 曾拉 1m K 线）。
    """
    from nanobot_quant.strategies.td_sequential_strategy import (
        TdSequentialStrategy,
    )

    m = TdSequentialStrategy._TIMESTEP_BY_SLEEPTIME
    assert m["1m"] == "minute"
    assert m["5m"] == "5min"
    assert m["15m"] == "15min"
    assert m["30m"] == "30min"
    assert m["1H"] == "hour"
    assert m["4H"] == "4hour"
    assert m["1D"] == "day"
    assert m["1W"] == "week"

    # 数据源侧契约：5min/15min 必须能映射到真实 bar 粒度
    from nanobot_quant.data.cex_data_source import _BAR_MAP
    assert _BAR_MAP["minute"] == "1m"
    assert _BAR_MAP["5min"] == "5m"
    assert _BAR_MAP["15min"] == "15m"
    assert _BAR_MAP["hour"] == "1H"
    assert _BAR_MAP["day"] == "1D"


# ── S3 场景池 slot→子账号解析（2026-08-23 回归）──────────────────

class _FakeCexBroker:
    """记录 sub_account 的 fake，替代 CexBroker（不触网络/不触 gate.json）。"""

    def __init__(self, tokens_json=None, slippage="0.01", sub_account=None):
        self.sub_account = sub_account


def test_cex_slot_broker_prefers_slot_account_id(tmp_path, monkeypatch):
    """S3 场景化：slot.account_id（场景 sub_accounts 池）优先于全局 slot_map。

    回归（2026-08-23）：mid 场景 RENDER BUY 资金检查 slot1 曾按全局
    slot_map 解析成 gate_bot1（余额 0.0001 < 4）→「无可用资金 slot」，
    而场景池 gate_bot3 实际有 4.862 USDT——页面资金表（主 key 批量）
    与策略检查（子账号 key）数据源不同导致用户看到「有资金却买不了」。
    """
    import nanobot_quant.brokers.cex_broker as cex_broker_mod

    monkeypatch.setattr(cex_broker_mod, "CexBroker", _FakeCexBroker)
    # 全局 slot_map 默认 1..N（DEX 时代默认，无 slot_map 字段时如此）
    monkeypatch.setattr(
        "nanobot_quant.gate_credentials.load_slot_map",
        lambda creds=None: {"1": "gate_bot1", "2": "gate_bot2"},
    )
    bm = _make_bm(tmp_path, n=2, account_ids=["gate_bot3", "gate_bot4"])
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60), channel_family="cex")
    s._cex_brokers = {}

    # mid 场景 slot1 台账 account_id=gate_bot3（场景池）——必须优先
    broker = s._cex_slot_broker({"slot": 1, "account_id": "gate_bot3"})
    assert broker.sub_account == "gate_bot3"

    # 无 account_id 时回退全局 slot_map（兼容旧台账/确认路径）
    broker_fb = s._cex_slot_broker({"slot": 1, "account_id": ""})
    assert broker_fb.sub_account == "gate_bot1"

    # 缓存按 (slot_no, account) 区分——同 slot_no 不同子账号不复用错 broker
    broker_a = s._cex_slot_broker({"slot": 2, "account_id": "gate_bot4"})
    broker_b = s._cex_slot_broker({"slot": 2, "account_id": "gate_bot2"})
    assert broker_a.sub_account == "gate_bot4"
    assert broker_b.sub_account == "gate_bot2"
    assert broker_a is not broker_b


def test_cex_confirm_broker_uses_pending_account_id(tmp_path, monkeypatch):
    """pending 确认：用 pending 记录的 account_id 重建 broker（场景池）。

    2026-08-23：确认路径此前传空 account_id → 全局 slot_map → 查错
    子账号（如 gate_bot4 的 pending 用 gate_bot2 的 broker 查单）。
    """
    import nanobot_quant.brokers.cex_broker as cex_broker_mod

    monkeypatch.setattr(cex_broker_mod, "CexBroker", _FakeCexBroker)
    monkeypatch.setattr(
        "nanobot_quant.gate_credentials.load_slot_map",
        lambda creds=None: {"1": "gate_bot1", "2": "gate_bot2"},
    )
    bm = _make_bm(tmp_path, n=2, account_ids=["gate_bot3", "gate_bot4"])
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60), channel_family="cex")
    s._cex_brokers = {}

    broker = s._cex_confirm_broker(1, {"account_id": "gate_bot3"})
    assert broker.sub_account == "gate_bot3"

    # 旧记录无 account_id → 回退全局 slot_map（不崩）
    broker_legacy = s._cex_confirm_broker(1, {})
    assert broker_legacy.sub_account == "gate_bot1"

# ── 2026-08-26：Gate amount_precision 截断 → 台账记实际成交 filled ──

def test_cex_buy_full_loop_uses_filled_qty(tmp_path):
    """完整循环：CEX 下单 filled < 计划量（Gate 市价单 amount_precision
    截断，实测 ETH 0.00126091→0.0012）→ open_lot 用实际成交 filled，
    不用计划量——否则台账虚高 → 卖出缩量 → min_quote 卡 slot。"""
    bm = _make_bm(tmp_path)
    s = _make_cex_strategy(bm, _bars_with(_buy_closes()))

    def _submit_with_filled(slot, req):
        order = _mock_order(quantity=req.quantity)
        order.custom_params = {"cex": {"filled": 0.0012, "avg_price": 2461.6}}
        return order

    s._cex_submit = _submit_with_filled
    s.on_trading_iteration()
    lots = bm.open_slots()
    assert lots, "应建仓"
    assert lots[0]["lot"]["qty"] == pytest.approx(0.0012)


def test_cex_confirm_buy_uses_filled_not_planned(tmp_path):
    """pending 确认：CEX BUY 用实际成交 filled 建仓（info.qty 是计划量，
    Gate 截断后 filled < 计划量）——用计划量台账虚高（2026-08-26 修复）。"""
    bm = _make_bm(tmp_path, n=2, account_ids=["gate_bot1", "gate_bot2"])
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60), channel_family="cex")
    s._batch_managers = {}
    s._pending_buys[1] = {
        "slot": 1, "symbol": "ETH", "order_id": "x", "chain": "",
        "qty": 0.00126091, "price": 2458.54, "reason": "t", "cex": True,
        "account_id": "gate_bot1",
    }

    class _FakeQ:
        def _query_order(self, order_id, pair):
            return ("filled", 0.0012, 0.0, 2461.6)

    s._cex_confirm_broker = lambda slot_id, info: _FakeQ()
    s._confirm_cex_buy(1, s._pending_buys[1])
    lots = bm.open_slots()
    assert lots and lots[0]["lot"]["qty"] == pytest.approx(0.0012)

def test_cex_sell_releases_below_min_quote(tmp_path):
    """B 方案（2026-08-26 用户拍板）：卖出价值 < Gate min_quote → 释放台账
    不卖（服务端拒单 → EXIT_FAIL → 台账 open 卡 slot，2026-08-25 ETH
    $2.96 实证）——EXIT_RELEASE 记录、_cex_submit 不调用。"""
    bm = _make_bm(tmp_path, n=2, account_ids=["gate_bot1", "gate_bot2"])
    bm.open_lot(slot=1, qty=0.0012, entry_price=2461.6)   # $2.95 < $3
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60), channel_family="cex")
    submitted = []
    s._cex_submit = lambda slot, req: submitted.append(req) or _mock_order()
    s._cex_slot_token_balance = lambda slot, symbol: 0.0012
    s._cex_min_quote = lambda symbol: 3.0
    s._sell_lot_cex(bm.slots[0], 2461.0, {"recommendation": "SELL"}, "test")
    assert submitted == []          # 未发单
    assert bm.open_slots() == []    # 台账已释放（仓位留在子账号）


def test_cex_sell_above_min_quote_still_submits(tmp_path):
    """B 方案不误伤：卖出价值 ≥ min_quote → 正常下单（预检放行）。"""
    bm = _make_bm(tmp_path, n=2, account_ids=["gate_bot1", "gate_bot2"])
    bm.open_lot(slot=1, qty=0.0014, entry_price=2461.6)   # $3.45 ≥ $3
    s = _make_cex_strategy(bm, _bars_with([100.0] * 60), channel_family="cex")
    submitted = []
    s._cex_submit = lambda slot, req: submitted.append(req) or _mock_order()
    s._cex_slot_token_balance = lambda slot, symbol: 0.0014
    s._cex_min_quote = lambda symbol: 3.0
    s._sell_lot_cex(bm.slots[0], 2463.0, {"recommendation": "SELL"}, "test")
    assert len(submitted) == 1      # 正常下单（预检放行，B 方案不误伤）
