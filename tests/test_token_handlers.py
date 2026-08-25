"""Unit tests for token WebUI handlers (tokens.json management).

Covers the A+C confirmation scheme surface exposed by ``/config/tokens``:
- routing registration (5 endpoints, same plugin pattern as credentials)
- add: clean entry saved as confirmed=false; questionable entry (EVM addr
  on solana) saved but flagged needs_confirmation; duplicates / built-in
  whitelist / empty fields rejected
- confirm: marks an entry confirmed (resolve_token then passes it)
- edit: changing the address resets confirmation (no stale bypass)
- delete: removes the entry
- ``tools_research_chain._load_tokens_json`` feeds tokens.json into the
  fail-closed pre-check and TD check (L2 tier honoured)

Async handlers are driven via asyncio.run() — plain-sync pytest, no
pytest-asyncio dependency.
"""

import asyncio
import json

import pytest

from nanobot_quant import onchainos_cli
from nanobot_quant.token_handlers import (
    _read_tokens,
    _write_tokens,
    register_token_routes,
)
from nanobot_quant.tools import tools_research_chain

SOLANA_ADDR = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
EVM_ADDR = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"


class _FakeApp:
    """Captures routes registered by register_token_routes."""

    def __init__(self):
        self.routes = []

    def get(self, path):
        def deco(handler):
            self.routes.append(("GET", path, handler))
            return handler

        return deco

    def post(self, path):
        def deco(handler):
            self.routes.append(("POST", path, handler))
            return handler

        return deco


class _FakeRequest:
    def __init__(self, body=None):
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _isolated_tokens(tmp_path, monkeypatch):
    """Point token_json_path (both import sites) at a temp file."""
    target = tmp_path / "tokens.json"

    def fake_path():
        return target

    monkeypatch.setattr(onchainos_cli, "token_json_path", fake_path)
    monkeypatch.setattr(
        "nanobot_quant.token_handlers.token_json_path", fake_path
    )
    yield target


def _call(handler, body=None):
    return asyncio.run(handler(_FakeRequest(body)))


class TestRouteRegistration:
    def test_six_endpoints(self):
        app = _FakeApp()
        register_token_routes(app, None)
        paths = sorted((m, p) for m, p, _ in app.routes)
        assert paths == sorted(
            [
                ("GET", "/config/tokens"),
                ("POST", "/config/tokens/add"),
                ("POST", "/config/tokens/confirm"),
                ("POST", "/config/tokens/edit"),
                ("POST", "/config/tokens/meta"),
                ("POST", "/config/tokens/delete"),
            ]
        )


class TestAdd:
    def test_add_clean_entry(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add

        resp = _call(
            token_add,
            {"symbol": "wEvm", "address": SOLANA_ADDR, "chain": "solana"},
        )
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert data["status"] == "clean"
        entries = _read_tokens()
        assert entries == [
            {"symbol": "WEVM", "address": SOLANA_ADDR,
             "chain": "solana", "confirmed": False, "min_hold": 0.0}
        ]

    def test_add_questionable_entry_flagged(self, _isolated_tokens):
        """EVM address on solana is saved but must NOT pass the gate."""
        from nanobot_quant.token_handlers import token_add

        resp = _call(token_add, {"symbol": "WEVM", "address": EVM_ADDR,
                                 "chain": "solana"})
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert data["status"] == "needs_confirmation"
        assert "issue" in data

        # resolution must be blocked until confirmed (fail-closed)
        resolved = onchainos_cli.resolve_token(
            "WEVM", tokens_json=_read_tokens(), chain="solana"
        )
        assert resolved["ok"] is True          # address is resolvable…
        assert resolved["needs_confirmation"] is True  # …but gated

    def test_add_duplicate_rejected(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add

        _call(token_add, {"symbol": "WEVM", "address": SOLANA_ADDR,
                          "chain": "solana"})
        resp = _call(token_add, {"symbol": "wevm", "address": SOLANA_ADDR,
                                 "chain": "solana"})
        assert resp.status_code == 400

    def test_add_builtin_sol_registered(self, _isolated_tokens):
        """SOL (native coin) registers by symbol: address auto-filled from
        the builtin whitelist, confirmed=True, chain forced to solana."""
        from nanobot_quant.token_handlers import token_add

        resp = _call(token_add, {"symbol": "SOL", "address": "",
                                 "chain": "solana"})
        assert resp.status_code == 200, resp.body
        entries = _read_tokens()
        assert len(entries) == 1
        assert entries[0]["symbol"] == "SOL"
        assert entries[0]["address"] == onchainos_cli._BUILTIN_TOKENS["SOL"]
        assert entries[0]["chain"] == "solana"
        assert entries[0]["confirmed"] is True

    def test_add_builtin_sol_other_chain_rejected(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add

        resp = _call(token_add, {"symbol": "SOL", "address": "",
                                 "chain": "xlayer"})
        assert resp.status_code == 400
        assert _read_tokens() == []

    def test_add_stablecoin_rejected(self, _isolated_tokens):
        """USDC/USDT are stablecoins — no analysis value, kept out of the
        TD target management table."""
        from nanobot_quant.token_handlers import token_add

        for sym in ("USDC", "USDT"):
            resp = _call(token_add, {"symbol": sym, "address": "",
                                     "chain": "solana"})
            assert resp.status_code == 400
            assert "稳定币" in resp.body.decode()
        assert _read_tokens() == []

    def test_add_empty_fields_rejected(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add

        assert _call(token_add, {"symbol": "", "address": SOLANA_ADDR,
                                 "chain": "solana"}).status_code == 400
        assert _call(token_add, {"symbol": "WEVM", "address": "",
                                 "chain": "solana"}).status_code == 400


class TestConfirm:
    def test_confirm_unblocks_execution(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add, token_confirm

        _call(token_add, {"symbol": "WEVM", "address": EVM_ADDR,
                          "chain": "solana"})
        resp = _call(token_confirm, {"symbol": "WEVM", "address": EVM_ADDR})
        assert resp.status_code == 200
        assert json.loads(resp.body.decode())["ok"] is True

        resolved = onchainos_cli.resolve_token(
            "WEVM", tokens_json=_read_tokens(), chain="solana"
        )
        assert resolved["ok"] is True
        assert resolved["address"] == EVM_ADDR

    def test_confirm_wrong_address_rejected(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add, token_confirm

        _call(token_add, {"symbol": "WEVM", "address": EVM_ADDR,
                          "chain": "solana"})
        resp = _call(token_confirm, {"symbol": "WEVM",
                                     "address": "0xdeadbeef"})
        assert resp.status_code == 400
        resolved = onchainos_cli.resolve_token(
            "WEVM", tokens_json=_read_tokens(), chain="solana"
        )
        assert resolved["needs_confirmation"] is True


class TestEdit:
    def test_edit_resets_confirmation(self, _isolated_tokens):
        from nanobot_quant.token_handlers import (
            token_add,
            token_confirm,
            token_edit,
        )

        _call(token_add, {"symbol": "WEVM", "address": EVM_ADDR,
                          "chain": "solana"})
        _call(token_confirm, {"symbol": "WEVM", "address": EVM_ADDR})
        assert _read_tokens()[0]["confirmed"] is True

        # switch to a different (clean) address — confirmation must reset
        resp = _call(token_edit, {"symbol": "WEVM", "address": SOLANA_ADDR,
                                  "chain": "solana"})
        assert resp.status_code == 200
        assert json.loads(resp.body.decode())["status"] == "clean"
        entries = _read_tokens()
        assert entries[0]["address"] == SOLANA_ADDR
        assert entries[0]["confirmed"] is False


class TestDelete:
    def test_delete_removes_entry(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add, token_delete

        _call(token_add, {"symbol": "WEVM", "address": SOLANA_ADDR,
                          "chain": "solana"})
        resp = _call(token_delete, {"symbol": "WEVM"})
        assert resp.status_code == 200
        assert _read_tokens() == []

    def test_delete_unknown_404(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_delete

        assert _call(token_delete, {"symbol": "NOPE"}).status_code == 404


class TestResearchChainTokensLoading:
    def test_load_tokens_json_feeds_resolve(self, tmp_path, monkeypatch):
        """The research-chain pre-check must see tokens.json (L2 tier)."""
        target = tmp_path / "tokens.json"
        target.write_text(
            json.dumps([{"symbol": "WEVM", "address": EVM_ADDR,
                         "chain": "solana", "confirmed": False}]),
            encoding="utf-8",
        )

        def fake_path():
            return target

        monkeypatch.setattr(onchainos_cli, "token_json_path", fake_path)

        tokens = tools_research_chain._load_tokens_json()
        assert tokens == [{"symbol": "WEVM", "address": EVM_ADDR,
                           "chain": "solana", "confirmed": False}]

        # resolve through the same path the pre-check uses: questionable
        # entry must surface as needs_confirmation (no CLI call needed)
        resolved = onchainos_cli.resolve_token(
            "WEVM", tokens_json=tokens, chain="solana"
        )
        assert resolved["ok"] is True
        assert resolved["needs_confirmation"] is True
        assert resolved["category"] == "chain_mismatch"


class TestRenderListGatePair:
    def test_gate_pair_column(self, _isolated_tokens):
        """tokens 页 Gate 交易对列：symbol 回退 + gate_symbol 覆盖。"""
        from nanobot_quant.token_handlers import _render_list

        _write_tokens(
            [
                {"symbol": "CRCLX", "address": SOLANA_ADDR,
                 "chain": "solana", "confirmed": True},
                {"symbol": "SPX", "address": SOLANA_ADDR,
                 "chain": "solana", "confirmed": True,
                 "gate_symbol": "XSPX"},
                {"symbol": "MU", "address": SOLANA_ADDR,
                 "chain": "solana", "confirmed": True},
            ]
        )
        html = _render_list()
        # 表头与回退对（symbol → {SYMBOL}_USDT）
        assert "<th>Gate 交易对</th>" in html
        assert ">CRCLX_USDT<" in html
        assert ">MU_USDT<" in html
        # gate_symbol 覆盖 symbol（精确匹配 td 单元格，避免 XSPX_USDT 含 SPX_USDT 子串）
        assert ">XSPX_USDT<" in html
        assert ">SPX_USDT<" not in html
