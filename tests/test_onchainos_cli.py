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
