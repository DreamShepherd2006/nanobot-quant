"""Tests for nanobot_quant.onchainos_cli pricing path.

Covers the official onchainos CLI pricing order: ``market price`` first,
aggregated ``market index`` fallback, and NO candle-close pricing
(``market kline`` is a data endpoint, not a pricing endpoint).
"""

from nanobot_quant import onchainos_cli


def test_get_price_prefers_market_price(monkeypatch):
    calls = []

    def fake_run(*args, **_kw):
        calls.append(args)
        assert args[:2] == ("market", "price")
        return {
            "ok": True,
            "data": [{"price": "66.87", "tokenContractAddress": "X", "chainIndex": "501"}],
        }

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    monkeypatch.setattr(
        onchainos_cli, "resolve_token_address", lambda symbol, tokens_json=None: "Xaddr"
    )
    assert onchainos_cli.get_price("CRCLX") == "66.87"
    assert calls == [("market", "price", "--address", "Xaddr", "--chain", "solana")]


def test_get_price_falls_back_to_index(monkeypatch):
    calls = []

    def fake_run(*args, **_kw):
        calls.append(args)
        if args[1] == "price":
            return {"ok": True, "data": []}  # no price data on this chain
        if args[1] == "index":
            return {"ok": True, "data": [{"price": "66.80"}]}
        return None

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    monkeypatch.setattr(
        onchainos_cli, "resolve_token_address", lambda symbol, tokens_json=None: "Xaddr"
    )
    assert onchainos_cli.get_price("CRCLX") == "66.80"
    assert calls[0][1] == "price" and calls[1][1] == "index"


def test_get_price_never_uses_kline(monkeypatch):
    calls = []

    def fake_run(*args, **_kw):
        calls.append(args)
        return None  # both price and index fail

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    monkeypatch.setattr(
        onchainos_cli, "resolve_token_address", lambda symbol, tokens_json=None: "Xaddr"
    )
    assert onchainos_cli.get_price("CRCLX") is None
    assert all(c[1] != "kline" for c in calls)


def test_get_market_price_parses_data_array(monkeypatch):
    monkeypatch.setattr(
        onchainos_cli,
        "_run",
        lambda *args, **kw: {"ok": True, "data": [{"price": "0.1234"}]},
    )
    assert onchainos_cli.get_market_price("Xaddr", chain="solana") == "0.1234"


def test_get_market_price_parses_flat_price(monkeypatch):
    monkeypatch.setattr(
        onchainos_cli,
        "_run",
        lambda *args, **kw: {"ok": True, "data": {"price": "9.9"}},
    )
    assert onchainos_cli.get_market_price("Xaddr", chain="solana") == "9.9"


def test_stablecoin_shortcut_skips_cli(monkeypatch):
    called = []

    def fake_run(*args, **_kw):
        called.append(args)
        return None

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    assert onchainos_cli.get_price("USDC") == "1"
    assert onchainos_cli.get_price("usdt") == "1"
    assert not called


# ── get_token_assets / get_wallet_balance 归一化 ───────────────────

def test_get_token_assets_details_shape():
    """v4.3.1 主形状：data.details[0].tokenAssets。"""
    from nanobot_quant.onchainos_cli import get_token_assets

    data = {
        "details": [
            {"accountId": "a1", "tokenAssets": [
                {"symbol": "RENDER", "balance": "6.06"},
                {"symbol": "CRCLX", "balance": "0.05"},
            ]},
        ]
    }
    out = get_token_assets(data)
    assert [t["symbol"] for t in out] == ["RENDER", "CRCLX"]


def test_get_token_assets_legacy_shapes():
    from nanobot_quant.onchainos_cli import get_token_assets

    assert get_token_assets({"assets": [{"symbol": "SOL"}]})[0]["symbol"] == "SOL"
    assert get_token_assets({"balances": [{"symbol": "SOL"}]})[0]["symbol"] == "SOL"
    assert get_token_assets({"assets": []}) == []
    assert get_token_assets("not a dict") == []
    assert get_token_assets(None) == []


def test_get_token_assets_filters_non_dict_entries():
    """元素为 str（如账户名/时间戳泄漏进列表）时必须过滤，否则 broker 的
    t.get() 会抛 AttributeError（P1 loop 验证实测崩溃点）。"""
    from nanobot_quant.onchainos_cli import get_token_assets

    data = {"details": [{"tokenAssets": [
        {"symbol": "SOL", "balance": "1"},
        "details",
        "totalValueUsd",
    ]}]}
    out = get_token_assets(data)
    assert [t["symbol"] for t in out] == ["SOL"]


def test_get_wallet_balance_normalises_dict_shape(monkeypatch):
    """get_wallet_balance 返回 dict（details 形状）时必须归一化为 list[dict]，
    否则 broker 迭代 dict keys 崩溃（'str' object has no attribute 'get'）。"""
    from nanobot_quant import onchainos_cli
    from nanobot_quant.onchainos_cli import get_wallet_balance

    def fake_run(*args, **_kw):
        # 真实 CLI 信封形状：{"ok":true,"data":{"details":[...]}}
        return {
            "ok": True,
            "data": {
                "details": [
                    {"accountId": "a1", "tokenAssets": [
                        {"symbol": "RENDER", "balance": "6.06", "usdValue": "7.93"},
                    ]},
                ],
            },
        }

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    out = get_wallet_balance()
    assert isinstance(out, list)
    assert all(isinstance(t, dict) for t in out)
    assert out[0]["symbol"] == "RENDER"


def test_get_wallet_balance_legacy_dict_shape(monkeypatch):
    """兼容旧形状：_run 直接返回内层 details（无信封）时同样归一化。"""
    from nanobot_quant import onchainos_cli
    from nanobot_quant.onchainos_cli import get_wallet_balance

    def fake_run(*args, **_kw):
        return {"details": [{"tokenAssets": [{"symbol": "SOL", "balance": "1"}]}]}

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    out = get_wallet_balance()
    assert [t["symbol"] for t in out] == ["SOL"]


def test_get_wallet_balance_cli_error(monkeypatch):
    """CLI 失败（错误信封）→ 返回 [] 而非抛异常（broker 安全降级）。"""
    from nanobot_quant import onchainos_cli
    from nanobot_quant.onchainos_cli import get_wallet_balance

    def fake_run(*args, **_kw):
        return {
            "_exit_code": 1,
            "_stderr": "session expired, please login again: onchainos wallet login",
        }

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    assert get_wallet_balance() == []


def test_broker_positions_survive_dict_shape(monkeypatch):
    """OnchainOSBroker._pull_positions 对 dict 形状余额不再崩溃（回归）。

    CLI v4.3.1 的 ``wallet balance`` 返回 ``data.details[0].tokenAssets``
    （dict 包裹）；get_wallet_balance 归一化为 list[dict] 后 broker 才能
    安全迭代 t.get()。mock 底层 _run 走真实归一化路径。
    """
    from nanobot_quant import onchainos_cli
    from nanobot_quant.brokers.onchainos_broker import OnchainOSBroker

    def fake_run(*args, **_kw):
        # 真实 CLI 信封：{"ok":true,"data":{"details":[...]}}
        return {
            "ok": True,
            "data": {
                "details": [
                    {
                        "accountId": "a1",
                        "tokenAssets": [
                            {"symbol": "RENDER", "balance": "6.06", "tokenPrice": "1.3", "usdValue": "7.93"},
                            {"symbol": "SOL", "balance": "0.0899", "tokenPrice": "160", "usdValue": "14.4"},
                        ],
                    },
                ],
            },
        }

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    broker = OnchainOSBroker(tokens_json=[], slippage="0.01", sol_buffer_pct=0.05)
    positions = broker._pull_positions(None)
    # SOL 不计入持仓；RENDER 为唯一持仓
    assert len(positions) == 1
    assert positions[0].asset.symbol == "RENDER"
    assert abs(positions[0].quantity - 6.06) < 1e-9
    # current_price 从 tokenPrice 字段（构造后赋值，v4.5.78 签名无该参数）
    assert abs(positions[0].current_price - 1.3) < 1e-9

def test_broker_balances_uses_usd_value_field(monkeypatch):
    """_get_balances_at_broker 必须用 CLI v4.3.1 的 usdValue 字段计算 total，
    否则 portfolio_value=0 → TD BLOCK（曾读 valueUsd 恒为 0）。"""
    from nanobot_quant import onchainos_cli
    from nanobot_quant.brokers.onchainos_broker import OnchainOSBroker

    def fake_run(*args, **_kw):
        return {
            "ok": True,
            "data": {
                "details": [{"tokenAssets": [
                    {"symbol": "RENDER", "balance": "6.06", "usdValue": "7.93"},
                    {"symbol": "SOL", "balance": "0.0899", "usdValue": "14.4"},
                ]}],
            },
        }

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    broker = OnchainOSBroker(tokens_json=[], slippage="0.01", sol_buffer_pct=0.05)
    cash, pos, total = broker._get_balances_at_broker(None, None)
    assert abs(cash - 14.4) < 1e-9      # SOL → cash
    assert abs(pos - 7.93) < 1e-9       # RENDER → positions
    assert abs(total - 22.33) < 1e-9


def test_broker_balances_legacy_valueusd_fallback(monkeypatch):
    """老形状 valueUsd 字段仍兼容（防御性回退）。"""
    from nanobot_quant import onchainos_cli
    from nanobot_quant.brokers.onchainos_broker import OnchainOSBroker

    def fake_run(*args, **_kw):
        return {
            "ok": True,
            "data": {
                "details": [{"tokenAssets": [
                    {"symbol": "SOL", "balance": "1", "valueUsd": "77.0"},
                ]}],
            },
        }

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    broker = OnchainOSBroker(tokens_json=[], slippage="0.01", sol_buffer_pct=0.05)
    cash, pos, total = broker._get_balances_at_broker(None, None)
    assert abs(total - 77.0) < 1e-9


def test_resolve_token_echoes_entry_chain(monkeypatch):
    """tokens.json entry chain wins over the caller default (SPCXB → bnb)."""
    from nanobot_quant import onchainos_cli

    monkeypatch.setattr(onchainos_cli, "_validate_token_entry",
                        lambda entry, chain="solana":
                        {"ok": True, "issue": None, "category": None})
    entry = {"symbol": "SPCXB", "chain": "bnb",
             "address": "0xbe000000000000000000000000000000000003e1",
             "confirmed": True}
    r = onchainos_cli.resolve_token("SPCXB", tokens_json=[entry], chain="solana")
    assert r["ok"] is True
    assert r["chain"] == "bnb"
    assert r["source"] == "tokens_json"


def test_resolve_token_builtin_chain_is_solana(monkeypatch):
    """Builtin SOL always resolves to solana regardless of caller chain."""
    from nanobot_quant import onchainos_cli

    r = onchainos_cli.resolve_token("SOL", chain="bnb")
    assert r["ok"] is True
    assert r["chain"] == "solana"


def test_resolve_token_l2_validation_uses_entry_chain(monkeypatch):
    """A bnb EVM entry passes validation under its own chain (not solana)."""
    from nanobot_quant import onchainos_cli

    entry = {"symbol": "SPCXB", "chain": "bnb",
             "address": "0xbe000000000000000000000000000000000003e1",
             "confirmed": False}
    r = onchainos_cli.resolve_token("SPCXB", tokens_json=[entry], chain="solana")
    assert r["ok"] is True          # no chain_mismatch under bnb
    assert r["confirmed"] is False
    assert r["chain"] == "bnb"


def test_token_chain_returns_entry_chain():
    from nanobot_quant.tokens_store import token_chain

    entries = [{"symbol": "SPCXB", "chain": "bnb"}]
    assert token_chain("SPCXB", entries) == "bnb"
    assert token_chain("spcxb", entries) == "bnb"  # case-insensitive
    assert token_chain("CRCLX", entries) == "solana"  # default
    assert token_chain("CRCLX", []) == "solana"

def test_broker_submit_uses_entry_chain(monkeypatch):
    """Broker swaps on the target's own chain (SPCXB → bnb), not the
    global okx.json chain."""
    from nanobot_quant import onchainos_cli
    from nanobot_quant.brokers.onchainos_broker import OnchainOSBroker

    captured = {}
    monkeypatch.setattr(
        "nanobot_quant.brokers.onchainos_broker.resolve_token_address",
        lambda symbol, tokens_json=None: "0xbe000000000000000000000000000000000003e1",
    )
    monkeypatch.setattr(
        "nanobot_quant.brokers.onchainos_broker.swap_execute",
        lambda from_addr, to_addr, from_amount, slippage, chain="solana",
               wallet=None: (
            captured.update(chain=chain) or {"ok": True, "tx_id": "0xtx"}
        ),
    )
    monkeypatch.setattr(
        "nanobot_quant.brokers.onchainos_broker.get_active_wallet_address",
        lambda chain: "0x3ec58f7cf1daf99584281c62fb634ba5a254e8c6",
    )
    monkeypatch.setattr(
        "nanobot_quant.brokers.onchainos_broker.get_token_price",
        lambda symbol, tokens_json=None, chain="solana": 137.08,
    )

    broker = OnchainOSBroker(
        tokens_json=[{"symbol": "SPCXB", "chain": "bnb",
                      "address": "0xbe000000000000000000000000000000000003e1"}],
        slippage="0.01", sol_buffer_pct=0.05,
    )
    from types import SimpleNamespace

    class _Order(SimpleNamespace):
        def set_error(self, *a, **k):
            pass

        def set_identifier(self, *a, **k):
            pass

        def set_filled(self, *a, **k):
            pass

    order = _Order(
        asset=SimpleNamespace(symbol="SPCXB"),
        side="sell",
        quantity=1.0,
        quote=SimpleNamespace(symbol="USDC"),
    )
    result = broker._submit_order(order)
    assert result is not None
    assert captured["chain"] == "bnb"
def test_round_readable_amount(monkeypatch):
    """swap 提交前 readable-amount 舍入到 8 位小数。

    2026-08-11 回归：qty = pv × max_position_pct / price 的浮点除法产生
    15+ 位小数（0.042222355341467045），CLI 按 token decimals（8）校验
    拒绝执行（00:44 CRCLX cd_sell=13 实证）。舍入后不超限。
    """
    calls = []
    monkeypatch.setattr(onchainos_cli, "_run", lambda *a, **k: (calls.append(a), None)[1])
    onchainos_cli.swap_execute(
        from_addr="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        to_addr="XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1",
        amount="0.042222355341467045",
    )
    args = calls[0]
    assert args[args.index("--readable-amount") + 1] == "0.04222236"

    # 整数/少位数不变
    calls.clear()
    onchainos_cli.swap_execute(
        from_addr="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        to_addr="XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1",
        amount="1.5",
    )
    assert calls[0][calls[0].index("--readable-amount") + 1] == "1.5"
    assert calls[0][calls[0].index("--readable-amount") + 1] == "1.5"


def test_swap_execute_decimals_retry(monkeypatch):
    """SPCX 6 decimals：首次 8 位被拒 → 解析 (6 decimals) → 按 6 位重试成功。

    2026-08-11 回归：SPCX setup_sell=9 平仓连续 EXIT_FAIL——CLI 拒绝
    --readable-amount "0.02053879"（8 位小数 > 6 decimals 上限）。
    """
    monkeypatch.setattr(onchainos_cli, "_DECIMALS_CACHE", {})
    calls = []

    def fake_run(*args, **_kw):
        calls.append(args)
        if len(calls) == 1:
            return {
                "_exit_code": 1,
                "_stdout": '{"ok":false,"error":"--readable-amount \\"0.02053879\\" has more decimal places than this token supports (6 decimals)"}',
                "_stdout_parsed": {"ok": False, "error": "--readable-amount \"0.02053879\" has more decimal places than this token supports (6 decimals)"},
                "_stderr": "",
                "_stderr_parsed": None,
            }
        return {"ok": True, "data": {"swapTxHash": "tx123"}}

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    result = onchainos_cli.swap_execute(
        from_addr="SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb",
        to_addr="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        amount="0.0205387903892588",
    )
    assert len(calls) == 2
    amt2 = calls[1][calls[1].index("--readable-amount") + 1]
    assert amt2 == "0.020539"
    assert result["ok"] is True
    assert (
        onchainos_cli._DECIMALS_CACHE["SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb:solana"]
        == 6
    )


def test_swap_execute_decimals_cached_no_retry(monkeypatch):
    """decimals 缓存后直接按 6 位提交，单次调用，无试错。"""
    monkeypatch.setattr(
        onchainos_cli,
        "_DECIMALS_CACHE",
        {"SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb:solana": 6},
    )
    calls = []

    def fake_run(*args, **_kw):
        calls.append(args)
        return {"ok": True, "data": {"swapTxHash": "tx456"}}

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    onchainos_cli.swap_execute(
        from_addr="SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb",
        to_addr="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        amount="0.0205387903892588",
    )
    assert len(calls) == 1
    amt = calls[0][calls[0].index("--readable-amount") + 1]
    assert amt == "0.020539"


def test_swap_execute_non_decimals_error_no_retry(monkeypatch):
    """非 decimals 错误（如 52001 资金不足）不重试，保持单次调用。"""
    monkeypatch.setattr(onchainos_cli, "_DECIMALS_CACHE", {})
    calls = []

    def fake_run(*args, **_kw):
        calls.append(args)
        return {
            "_exit_code": 1,
            "_stdout": '{"ok":false,"error":"[52001] Insufficient balance"}',
            "_stdout_parsed": {"ok": False, "error": "[52001] Insufficient balance"},
            "_stderr": "",
            "_stderr_parsed": None,
        }

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    onchainos_cli.swap_execute(
        from_addr="SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb",
        to_addr="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        amount="0.0205387903892588",
    )
    assert len(calls) == 1
