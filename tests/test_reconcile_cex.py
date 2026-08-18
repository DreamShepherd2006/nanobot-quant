"""td_live CEX 启动对账（子账号天然持仓导入台账）测试 — Step 2，2026-08-18 拍板。

设计要点（docs/quant-system.md 第二十章）：
- P1-A 范围：只导入 td_symbols 标的池的币（per-symbol 对账天然满足）
- P2-A 阈值：价值 < Gate min_quote（$3，交易对规则动态拉取）不导入
  ——CEX 卖出受 min_quote 硬限，导入死 lot 会卡 slot
- P3-A entry_price：tokens.json cost_price 优先 → gate ticker 兜底
- 数据源：主 key /wallet/sub_account_balances 一次拉全部子账号，
  按 slot 的 account_id（gate_botN）→ UID 匹配；无状态、无需还原
- 幂等：available slot 才导入、open 跳过、导入后持久化
"""

from __future__ import annotations

from nanobot_quant.batches import BatchManager
from nanobot_quant.td_live import _TdLiveRunner


def _make_bm(symbol="CRCLX", names=("gate_bot1", "gate_bot2")):
    return BatchManager(symbol=symbol, account_ids=list(names))


def _creds(bot2_uid="59175258"):
    return {
        "main": {"api_key": "k_main", "api_secret": "s_main", "uid": "15119093"},
        "sub_accounts": {
            "gate_bot1": {"uid": "59175220", "api_key": "k1", "api_secret": "s1"},
            "gate_bot2": {"uid": bot2_uid, "api_key": "k2", "api_secret": "s2"},
        },
    }


def _make_runner(creds, balances, monkeypatch, price=74.14, min_quote="3"):
    """mock gate 四件套：凭证 / 子账号余额 / 交易对规则 / gate ticker。"""
    monkeypatch.setattr("nanobot_quant.gate_credentials.load_gate_credentials",
                        lambda: creds)
    monkeypatch.setattr("nanobot_quant.gate_credentials.load_slot_map",
                        lambda creds=None: {"1": "gate_bot1", "2": "gate_bot2"})
    monkeypatch.setattr("nanobot_quant.gate_sdk.sub_account_balances",
                        lambda api_key, api_secret: balances)
    monkeypatch.setattr("nanobot_quant.gate_sdk.get_currency_pair",
                        lambda api_key, api_secret, pair: {
                            "min_quote_amount": min_quote,
                            "trade_status": "tradable",
                        })

    class _FakeDs:
        def get_price(self, symbol):
            return price

    monkeypatch.setattr(
        "nanobot_quant.data_sources.base.get_data_source",
        lambda name: _FakeDs(),
    )
    return _TdLiveRunner()


def _tokens(cost_price=None, gate_symbol=None):
    return [{"symbol": "CRCLX", "chain": "solana",
             "cost_price": cost_price, "gate_symbol": gate_symbol,
             "confirmed": True}]


def _bal(bot2_crclx="0.112069"):
    return [
        {"uid": "59175220", "available": {"RENDER": "0.0065"}, "locked": {}},
        {"uid": "59175258", "available": {"CRCLX": bot2_crclx}, "locked": {}},
    ]


def test_cex_reconcile_imports_natural_position(monkeypatch):
    """gate_bot2 天然持仓 CRCLX 0.112069 → 导入 slot 2，entry_price=gate ticker。

    0.112069 × 74.14 ≈ $8.31 ≥ min_quote $3 → 导入（对账价兜底）。
    """
    bm = _make_bm()
    runner = _make_runner(_creds(), _bal(), monkeypatch)
    runner._reconcile_import_cex(bm, "CRCLX", _tokens())
    open_lots = bm.open_slots()
    assert len(open_lots) == 1
    assert open_lots[0]["slot"] == 2
    assert abs(open_lots[0]["lot"]["qty"] - 0.112069) < 1e-9
    assert abs(open_lots[0]["lot"]["entry_price"] - 74.14) < 1e-6  # gate ticker 兜底


def test_cex_reconcile_skips_dust_below_min_quote(monkeypatch):
    """持仓价值 < min_quote $3 → 不导入、slot 保持 available（P2-A）。

    真实样本：gate_bot1 RENDER 0.0065（≈$0.01）远低于 $3。
    """
    bm = _make_bm()
    runner = _make_runner(_creds(), _bal(), monkeypatch)
    runner._reconcile_import_cex(bm, "RENDER",
                                 [{"symbol": "RENDER", "chain": "solana",
                                   "confirmed": True}])
    assert bm.open_slots() == []


def test_cex_reconcile_uses_cost_price_when_set(monkeypatch):
    """tokens.json cost_price 优先于 gate ticker（P3-A）。"""
    bm = _make_bm()
    runner = _make_runner(_creds(), _bal(), monkeypatch)
    runner._reconcile_import_cex(bm, "CRCLX", _tokens(cost_price=70.0))
    open_lots = bm.open_slots()
    assert len(open_lots) == 1
    assert abs(open_lots[0]["lot"]["entry_price"] - 70.0) < 1e-6


def test_cex_reconcile_skips_open_slots(monkeypatch):
    """已 open 的 slot 跳过（TD 自己的仓）；天然持仓只进 available slot。"""
    bm = _make_bm()
    bm.open_lot(qty=1.0, entry_price=60.0, entry_time="t0", slot=1)
    # 天然持仓在 gate_bot2（slot 2）→ 导入 slot 2
    runner = _make_runner(_creds(), _bal(), monkeypatch)
    runner._reconcile_import_cex(bm, "CRCLX", _tokens())
    open_lots = bm.open_slots()
    assert len(open_lots) == 2  # slot1(TD) + slot2(天然)
    assert open_lots[1]["slot"] == 2


def test_cex_reconcile_no_credentials(monkeypatch):
    """无 gate 凭证 / 主 key 缺失 → 安全跳过（fail-closed 不误导入）。"""
    bm = _make_bm()
    runner = _make_runner(None, [], monkeypatch)
    runner._reconcile_import_cex(bm, "CRCLX", _tokens())
    assert bm.open_slots() == []

    runner = _make_runner({"main": {}, "sub_accounts": {}}, [], monkeypatch)
    runner._reconcile_import_cex(bm, "CRCLX", _tokens())
    assert bm.open_slots() == []


def test_cex_reconcile_sub_accounts_without_uid(monkeypatch, capsys):
    """gate.json 有 sub_accounts 但缺 UID → 明确诊断 + 安全跳过（不静默）。

    HF Space 曾为 flat 形态（仅主账号），对账曾静默报「无天然持仓」——
    本测试确保缺 UID 映射时有显式诊断，便于定位配置问题。
    """
    bm = _make_bm()
    creds = {
        "main": {"api_key": "k_main", "api_secret": "s_main"},
        "sub_accounts": {
            "gate_bot1": {"api_key": "k1", "api_secret": "s1"},  # 无 uid
            "gate_bot2": {"api_key": "k2", "api_secret": "s2"},
        },
    }
    runner = _make_runner(creds, _bal(), monkeypatch)
    runner._reconcile_import_cex(bm, "CRCLX", _tokens())
    assert bm.open_slots() == []
    err = capsys.readouterr().err
    assert "无 UID 映射" in err


def test_cex_reconcile_empty_sub_balances(monkeypatch, capsys):
    """主 key 无子账号权限/余额接口返回空 → 明确诊断 + 安全跳过。"""
    bm = _make_bm()
    runner = _make_runner(_creds(), [], monkeypatch)
    runner._reconcile_import_cex(bm, "CRCLX", _tokens())
    assert bm.open_slots() == []
    err = capsys.readouterr().err
    assert "子账号余额为空" in err


def test_cex_reconcile_imports_multiple_slots(monkeypatch):
    """多子账号各有持仓 → 各自导入对应 slot。"""
    bm = _make_bm()
    balances = [
        {"uid": "59175220",
         "available": {"CRCLX": "0.02"}, "locked": {}},        # $1.48 < $3 dust
        {"uid": "59175258",
         "available": {"CRCLX": "0.112069"}, "locked": {}},    # $8.31 导入
    ]
    runner = _make_runner(_creds(), balances, monkeypatch)
    runner._reconcile_import_cex(bm, "CRCLX", _tokens())
    open_lots = bm.open_slots()
    assert len(open_lots) == 1
    assert open_lots[0]["slot"] == 2  # bot1 的 $1.48 dust 不占 slot


def test_cex_reconcile_matches_by_gate_symbol(monkeypatch):
    """tokens.json gate_symbol 优先匹配余额币种（Gate 币种大写）。"""
    bm = _make_bm()
    # symbol 小写 CRCLx，余额币种大写 CRCLX —— 靠 gate_symbol 或 upper 命中
    runner = _make_runner(_creds(), _bal(), monkeypatch)
    runner._reconcile_import_cex(
        bm, "CRCLx",
        [{"symbol": "CRCLx", "chain": "solana", "confirmed": True}],
    )
    open_lots = bm.open_slots()
    assert len(open_lots) == 1
    assert open_lots[0]["slot"] == 2


def test_cex_reconcile_dynamic_min_quote(monkeypatch):
    """min_quote 从交易对规则动态拉取（非硬编码 $3），低于阈值即跳过。"""
    bm = _make_bm()
    # 0.112069 × 74.14 ≈ $8.31 ≥ $10? 否 → 跳过
    runner = _make_runner(_creds(), _bal(), monkeypatch, min_quote="10")
    runner._reconcile_import_cex(bm, "CRCLX", _tokens())
    assert bm.open_slots() == []
