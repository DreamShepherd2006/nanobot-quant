"""td_live 启动对账（天然持仓导入台账）测试 — 2026-08-10 拍板设计。

场景覆盖：
- 链上天然持仓 → 导入 available slot（min_hold 每账户扣减）
- 纯保留量（SOL gas）→ 不导入、不动
- cost_price 优先于对账价兜底
- 已 open 的 slot 跳过（TD 自己开的仓不重复导入）
- 对账后还原活跃账户（wallet switch 是全局状态）
"""

from __future__ import annotations

from nanobot_quant.batches import BatchManager
from nanobot_quant.td_live import _TdLiveRunner

SOLANA_ADDR = "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1"


def _make_bm(symbol="CRCLX", accounts=("acc-1", "acc-2")):
    return BatchManager(symbol=symbol, account_ids=list(accounts))


def _make_runner(balances, monkeypatch, home="acc-1"):
    """mock wallet 三件套：switch 记录当前账户，balance 返回该账户资产。"""
    state = {"current": home}

    def fake_switch(aid):
        state["current"] = aid
        return {"ok": True, "data": {}}

    def fake_balance(**kw):
        assets = balances.get(state["current"], [])
        return {"status": "ok",
                "data": {"details": [{"tokenAssets": assets}]}}

    def fake_status():
        return {"status": "ok", "data": {"currentAccountId": home}}

    monkeypatch.setattr("nanobot_quant.tools.tools_wallet.wallet_switch",
                        fake_switch)
    monkeypatch.setattr("nanobot_quant.tools.tools_wallet.wallet_balance",
                        fake_balance)
    monkeypatch.setattr("nanobot_quant.tools.tools_wallet.wallet_status",
                        fake_status)
    monkeypatch.setattr("nanobot_quant.onchainos_cli.get_token_price",
                        lambda symbol, tokens_json=None, chain="solana": 66.8)
    return _TdLiveRunner(), state


def _tokens(min_hold=0.0, cost_price=None, address=SOLANA_ADDR):
    return [{"symbol": "CRCLX", "address": address, "chain": "solana",
             "min_hold": min_hold, "cost_price": cost_price,
             "confirmed": True}]


def test_reconcile_imports_natural_position(monkeypatch):
    bm = _make_bm()
    balances = {"acc-1": [{"symbol": "CRCLX", "balance": "0.0524761",
                           "tokenAddress": SOLANA_ADDR}]}
    runner, state = _make_runner(balances, monkeypatch)
    runner._reconcile_import(bm, "CRCLX", _tokens())
    open_lots = bm.open_slots()
    assert len(open_lots) == 1
    assert open_lots[0]["slot"] == 1
    assert abs(open_lots[0]["lot"]["qty"] - 0.0524761) < 1e-9
    assert abs(open_lots[0]["lot"]["entry_price"] - 66.8) < 1e-6  # 对账价兜底
    assert state["current"] == "acc-1"  # 还原活跃账户


def test_reconcile_skips_pure_min_hold(monkeypatch):
    """SOL 场景：账户余额 = min_hold → 纯保留量不导入。"""
    bm = _make_bm()
    balances = {"acc-1": [{"symbol": "SOL", "balance": "0.01",
                           "tokenAddress": ""}]}
    runner, _ = _make_runner(balances, monkeypatch)
    runner._reconcile_import(bm, "SOL", [{"symbol": "SOL", "address": "",
                                          "chain": "solana",
                                          "min_hold": 0.01,
                                          "cost_price": None,
                                          "confirmed": True}])
    assert bm.open_slots() == []


def test_reconcile_imports_excess_above_min_hold(monkeypatch):
    """账户余额超出 min_hold → 导入超出部分（SOL 有 TD 仓时的 SELL 场景）。"""
    bm = _make_bm()
    balances = {"acc-1": [{"symbol": "SOL", "balance": "0.042",
                           "tokenAddress": ""}]}
    runner, _ = _make_runner(balances, monkeypatch)
    runner._reconcile_import(bm, "SOL", [{"symbol": "SOL", "address": "",
                                          "chain": "solana",
                                          "min_hold": 0.01,
                                          "cost_price": None,
                                          "confirmed": True}])
    open_lots = bm.open_slots()
    assert len(open_lots) == 1
    assert abs(open_lots[0]["lot"]["qty"] - 0.032) < 1e-9  # 0.042 - 0.01


def test_reconcile_uses_cost_price_when_set(monkeypatch):
    bm = _make_bm()
    balances = {"acc-1": [{"symbol": "CRCLX", "balance": "0.05",
                           "tokenAddress": SOLANA_ADDR}]}
    runner, _ = _make_runner(balances, monkeypatch)
    runner._reconcile_import(bm, "CRCLX", _tokens(cost_price=66.5))
    open_lots = bm.open_slots()
    assert abs(open_lots[0]["lot"]["entry_price"] - 66.5) < 1e-6  # 用户成本价优先


def test_reconcile_skips_open_slots(monkeypatch):
    """已 open 的 slot 跳过（TD 自己的仓）；天然持仓只进 available slot。"""
    bm = _make_bm()
    bm.open_lot(qty=1.0, entry_price=60.0, entry_time="t0", slot=1)
    balances = {"acc-2": [{"symbol": "CRCLX", "balance": "0.05",
                           "tokenAddress": SOLANA_ADDR}]}
    runner, _ = _make_runner(balances, monkeypatch)
    runner._reconcile_import(bm, "CRCLX", _tokens())
    open_lots = bm.open_slots()
    assert len(open_lots) == 2  # slot1(TD) + slot2(天然)
    assert open_lots[1]["slot"] == 2


def test_reconcile_no_natural_position(monkeypatch):
    bm = _make_bm()
    runner, _ = _make_runner({}, monkeypatch)
    runner._reconcile_import(bm, "CRCLX", _tokens())
    assert bm.open_slots() == []


def test_reconcile_uses_balance_token_price(monkeypatch):
    """余额自带 tokenPrice（wallet balance 返回）优先于额外 CLI 取价。

    真实场景：RENDER/CRCLX 不在 OKX DEX 索引内，market price/index 返回
    空导致 get_token_price 失败；但余额明细自带 tokenPrice 可用。
    """
    bm = _make_bm()
    balances = {"acc-1": [{"symbol": "RENDER", "balance": "3.06",
                           "tokenAddress": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof",
                           "tokenPrice": "1.2955476623805305"}]}
    runner, _ = _make_runner(balances, monkeypatch)
    # get_token_price 不应被调用（余额价已够用）
    monkeypatch.setattr(
        "nanobot_quant.onchainos_cli.get_token_price",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应调用 CLI 取价")),
    )
    runner._reconcile_import(
        bm, "RENDER",
        [{"symbol": "RENDER",
          "address": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof",
          "chain": "solana", "min_hold": 0.0, "cost_price": None,
          "confirmed": True}],
    )
    open_lots = bm.open_slots()
    assert len(open_lots) == 1
    assert abs(open_lots[0]["lot"]["entry_price"] - 1.2955476623805305) < 1e-9


def test_reconcile_matches_native_sol_by_symbol(monkeypatch):
    """原生 SOL：钱包余额 tokenAddress 为空，登记地址为 wSOL。

    tokens.json 中 SOL 地址自动补 wSOL（So111...），链上原生 SOL
    tokenAddress="" —— 地址匹配必然失败，必须回退 symbol 匹配。
    """
    bm = _make_bm()
    balances = {"acc-1": [{"symbol": "SOL", "balance": "0.042",
                           "tokenAddress": "", "tokenPrice": "76.84"}]}
    runner, _ = _make_runner(balances, monkeypatch)
    runner._reconcile_import(
        bm, "SOL",
        [{"symbol": "SOL",
          "address": "So11111111111111111111111111111111111111112",
          "chain": "solana", "min_hold": 0.01, "cost_price": None,
          "confirmed": True}],
    )
    open_lots = bm.open_slots()
    assert len(open_lots) == 1
    assert abs(open_lots[0]["lot"]["qty"] - 0.032) < 1e-9  # 0.042 - 0.01
    assert abs(open_lots[0]["lot"]["entry_price"] - 76.84) < 1e-9  # 余额价
