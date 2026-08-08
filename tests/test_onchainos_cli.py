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
        return {
            "details": [
                {"accountId": "a1", "tokenAssets": [
                    {"symbol": "RENDER", "balance": "6.06", "usdValue": "7.93"},
                ]},
            ],
        }

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    out = get_wallet_balance()
    assert isinstance(out, list)
    assert all(isinstance(t, dict) for t in out)
    assert out[0]["symbol"] == "RENDER"


def test_broker_positions_survive_dict_shape(monkeypatch):
    """OnchainOSBroker._pull_positions 对 dict 形状余额不再崩溃（回归）。

    CLI v4.3.1 的 ``wallet balance`` 返回 ``data.details[0].tokenAssets``
    （dict 包裹）；get_wallet_balance 归一化为 list[dict] 后 broker 才能
    安全迭代 t.get()。mock 底层 _run 走真实归一化路径。
    """
    from nanobot_quant import onchainos_cli
    from nanobot_quant.brokers.onchainos_broker import OnchainOSBroker

    def fake_run(*args, **_kw):
        return {
            "details": [
                {
                    "accountId": "a1",
                    "tokenAssets": [
                        {"symbol": "RENDER", "balance": "6.06", "price": "1.3", "usdValue": "7.93"},
                        {"symbol": "SOL", "balance": "0.0899", "price": "160", "usdValue": "14.4"},
                    ],
                },
            ],
        }

    monkeypatch.setattr(onchainos_cli, "_run", fake_run)
    broker = OnchainOSBroker(tokens_json=[], slippage="0.01", sol_buffer_pct=0.05)
    positions = broker._pull_positions(None)
    # SOL 不计入持仓；RENDER 为唯一持仓
    assert len(positions) == 1
    assert positions[0].asset.symbol == "RENDER"
    assert abs(positions[0].quantity - 6.06) < 1e-9
