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
    s._cex_slot_balances = lambda slot: {"USDT": {"available": 1e9, "locked": 0}}
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
    s._cex_slot_balances = lambda slot: {"USDT": {"available": 1.0, "locked": 0}}
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
    s._cex_slot_portfolio_value = lambda slot: 100.0  # 小资产账户
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
    s._cex_slot_portfolio_value = lambda slot: 11.45  # 小账号：100U 远超 25% 上限
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
    s._cex_slot_balances = lambda slot: {"USDT": {"available": 5.0, "locked": 0}}
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
        lambda s=None, c=None: tmp_path / (f"batches.{c}.{s}.json" if c else f"batches.{s}.json"),
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
        lambda s=None, c=None: tmp_path / (f"batches.{c}.{s}.json" if c else f"batches.{s}.json"),
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
        lambda s=None, c=None: tmp_path / (f"batches.{c}.{s}.json" if c else f"batches.{s}.json"),
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
        lambda s=None, c=None: tmp_path / (f"batches.{c}.{s}.json" if c else f"batches.{s}.json"),
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
