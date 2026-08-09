"""Tests for the research-chain safety gates and helpers.

Covers:
- ``onchainos_cli`` symbol helpers (bare_symbol / normalize_symbol /
  is_contract_address / supported_symbols / chain_results_dir) — runnable
  anywhere.
- ``resolve_token`` tiered resolution + confirmation state (builtin →
  alias → tokens.json → CLI → structured error) — runnable anywhere.
- ``confirm_token`` confirmation memory (scheme C) — runnable anywhere.
- ``pipeline.run_from_signals`` fail-closed token gate — requires pandas
  + lumibot (Nightly container), guarded with importorskip.
"""

import json
from unittest import mock

import pytest

from nanobot_quant.onchainos_cli import (
    bare_symbol,
    chain_results_dir,
    confirm_token,
    is_contract_address,
    normalize_symbol,
    resolve_token,
    supported_symbols,
)

SOLANA_ADDR = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
EVM_ADDR = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
WSOL_ADDR = "So11111111111111111111111111111111111111112"


class TestBareSymbol:
    def test_pair_suffixes(self):
        assert bare_symbol("BTC-USDT") == "BTC"
        assert bare_symbol("AAPL.US") == "AAPL"
        assert bare_symbol("eth-usd") == "ETH"

    def test_bare_passthrough(self):
        assert bare_symbol("SOL") == "SOL"
        assert bare_symbol("SPCX") == "SPCX"

    def test_contract_address_not_upper_cased(self):
        # base58 is case-sensitive — addresses must pass through unchanged
        assert bare_symbol(SOLANA_ADDR) == SOLANA_ADDR
        assert bare_symbol(EVM_ADDR) == EVM_ADDR


class TestIsContractAddress:
    def test_solana_address(self):
        assert is_contract_address(SOLANA_ADDR) is True

    def test_evm_address(self):
        assert is_contract_address(EVM_ADDR) is True

    def test_symbols_not_addresses(self):
        assert is_contract_address("BTC") is False
        assert is_contract_address("SOL") is False
        assert is_contract_address("") is False

    def test_43_char_solana_address(self):
        """43-char base58 is a common real Solana length (So111…wSOL,
        most mainnet accounts) — must not be rejected."""
        assert is_contract_address("So11111111111111111111111111111111111111112") is True
        assert is_contract_address("SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb") is True
        assert is_contract_address("XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1") is True

    def test_too_short_or_long_rejected(self):
        assert is_contract_address("A" * 31) is False
        assert is_contract_address("A" * 45) is False


class TestSupportedSymbols:
    def test_native_tokens(self):
        syms = supported_symbols()
        assert "SOL" in syms and "USDC" in syms and "USDT" in syms

    def test_user_tokens_appended(self):
        syms = supported_symbols(
            [{"symbol": "wbtc", "address": "abc"}, {"symbol": "SOL"}]
        )
        assert "WBTC" in syms
        # native tokens are not duplicated
        assert syms.count("SOL") == 1


class TestChainResultsDir:
    def test_dir_name(self, tmp_path):
        (tmp_path / "legion").mkdir()
        d = chain_results_dir(roots=(str(tmp_path),))
        assert d == tmp_path / "legion" / "research_chains"
        assert d.name == "research_chains"
        assert d.is_dir()


class TestNormalizeSymbol:
    def test_leading_dollar_and_whitespace(self):
        assert normalize_symbol(" $SOL ") == "SOL"
        assert normalize_symbol("usdc") == "USDC"

    def test_pair_suffixes(self):
        assert normalize_symbol("BTC-USDT") == "BTC"
        assert normalize_symbol("ETH-USD") == "ETH"

    def test_address_passthrough_unchanged(self):
        # base58 is case-sensitive — addresses must pass through unchanged
        assert normalize_symbol(SOLANA_ADDR) == SOLANA_ADDR
        assert normalize_symbol(EVM_ADDR) == EVM_ADDR

    def test_empty(self):
        assert normalize_symbol("") == ""
        assert normalize_symbol("   ") == ""


class TestResolveToken:
    """Tiered resolution: builtin → alias → tokens.json → CLI → error."""

    def test_builtin_native_tokens(self):
        r = resolve_token("SOL")
        assert r["ok"] is True and r["source"] == "builtin"
        assert r["address"] == WSOL_ADDR
        assert r["needs_confirmation"] is False
        assert r["confirmed"] is True
        assert resolve_token("usdc")["address"].startswith("EPjF")

    def test_aliases(self):
        assert resolve_token("SOLANA")["address"] == resolve_token("SOL")["address"]
        assert resolve_token("WRAPPED_SOL")["source"] == "builtin"

    def test_address_passthrough(self):
        r = resolve_token(SOLANA_ADDR)
        assert r["ok"] is True and r["source"] == "address"
        assert r["address"] == SOLANA_ADDR

    def test_tokens_json_valid_entry_passes(self):
        tj = [{"symbol": "WBTC", "address": "3J2H3uUjQGQvVXfY5fW9xGJQ7zHqVfKq8Yp3Vz3R7xUz"}]
        with mock.patch("nanobot_quant.onchainos_cli.search_token", return_value=None):
            r = resolve_token("WBTC", tokens_json=tj)
        assert r["ok"] is True and r["source"] == "tokens_json"
        assert r["needs_confirmation"] is False

    def test_tokens_json_evm_address_needs_confirmation(self):
        tj = [{"symbol": "WEVM", "address": EVM_ADDR}]
        with mock.patch("nanobot_quant.onchainos_cli.search_token", return_value=None):
            r = resolve_token("WEVM", tokens_json=tj)
        # resolvable but questionable on solana → confirmation required
        assert r["ok"] is True
        assert r["needs_confirmation"] is True
        assert r["category"] == "chain_mismatch"
        assert r["confirmed"] is False

    def test_tokens_json_evm_address_confirmed_passes(self):
        tj = [{"symbol": "WEVM", "address": EVM_ADDR, "confirmed": True}]
        with mock.patch("nanobot_quant.onchainos_cli.search_token", return_value=None):
            r = resolve_token("WEVM", tokens_json=tj)
        assert r["ok"] is True and r["needs_confirmation"] is False
        assert r["confirmed"] is True

    def test_tokens_json_invalid_address_needs_confirmation(self):
        tj = [{"symbol": "BAD", "address": "not-an-address"}]
        with mock.patch("nanobot_quant.onchainos_cli.search_token", return_value=None):
            r = resolve_token("BAD", tokens_json=tj)
        assert r["ok"] is True
        assert r["needs_confirmation"] is True
        assert r["category"] == "invalid_address"

    def test_cli_fallback(self):
        with mock.patch(
            "nanobot_quant.onchainos_cli.search_token",
            return_value=WSOL_ADDR,
        ):
            r = resolve_token("SOMECOIN")
        assert r["ok"] is True and r["source"] == "cli"
        assert r["needs_confirmation"] is False

    def test_typo_suggestion(self):
        with mock.patch("nanobot_quant.onchainos_cli.search_token", return_value=None):
            r = resolve_token("USDTT")
        assert r["ok"] is False and r["category"] == "typo"
        assert r["suggestion"] == "USDT"

    def test_not_found_with_chain_hint(self):
        with mock.patch("nanobot_quant.onchainos_cli.search_token", return_value=None):
            r = resolve_token("BTC")
        assert r["ok"] is False and r["category"] == "not_found"
        assert "no native token on Solana" in r["hint"]

    def test_fake_coin_not_found(self):
        with mock.patch("nanobot_quant.onchainos_cli.search_token", return_value=None):
            r = resolve_token("FAKECOIN")
        assert r["ok"] is False and r["category"] == "not_found"
        assert r["suggestion"] is None


class TestConfirmToken:
    """Confirmation memory (scheme C): confirmed=true persists and resets on edit."""

    def _write_tokens(self, tmp_path, entries):
        p = tmp_path / "tokens.json"
        p.write_text(json.dumps(entries), encoding="utf-8")
        return p

    def test_confirm_persists_flag(self, tmp_path):
        p = self._write_tokens(tmp_path, [{"symbol": "WEVM", "address": EVM_ADDR}])
        with mock.patch("nanobot_quant.onchainos_cli.token_json_path", return_value=p):
            out = confirm_token("WEVM", address=EVM_ADDR)
        assert out["ok"] is True
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data[0]["confirmed"] is True

    def test_confirm_stale_address_rejected(self, tmp_path):
        p = self._write_tokens(tmp_path, [{"symbol": "WEVM", "address": EVM_ADDR}])
        with mock.patch("nanobot_quant.onchainos_cli.token_json_path", return_value=p):
            out = confirm_token("WEVM", address="0xdeadbeef")
        assert out["ok"] is False
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data[0].get("confirmed") is not True

    def test_confirmed_entry_passes_without_reask(self, tmp_path):
        p = self._write_tokens(tmp_path, [{"symbol": "WEVM", "address": EVM_ADDR}])
        with mock.patch("nanobot_quant.onchainos_cli.token_json_path", return_value=p):
            confirm_token("WEVM", address=EVM_ADDR)
        with mock.patch("nanobot_quant.onchainos_cli.search_token", return_value=None):
            r = resolve_token(
                "WEVM",
                tokens_json=[{"symbol": "WEVM", "address": EVM_ADDR, "confirmed": True}],
            )
        assert r["ok"] is True and r["needs_confirmation"] is False


class TestPipelineTokenGate:
    """fail-closed gate: unsupported symbols never reach the broker."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        # pipeline.py imports pandas and the lumibot-backed strategy at
        # module level, so the full gate tests need the real dependencies
        # (present in the HF Space container; skipped in this env).
        pytest.importorskip("pandas")
        return pytest.importorskip("lumibot")

    def _signal(self, ticker="BTC"):
        from nanobot_quant.signal import TickerSignal

        return TickerSignal(
            ticker=ticker,
            recommendation="BUY",
            score=6.0,
            price=60000.0,
            confidence=0.4,
            setup_buy=9,
            setup_sell=0,
            cd_buy=0,
            cd_sell=0,
            tdst_support=59000.0,
            tdst_resistance=62000.0,
            rvol=1.2,
        )

    def _resolve_ok(self, addr=WSOL_ADDR):
        return {"ok": True, "address": addr, "source": "builtin",
                "needs_confirmation": False, "issue": None, "confirmed": True,
                "category": None, "suggestion": None, "hint": None}

    def _resolve_bad(self):
        return {"ok": False, "address": None, "source": None,
                "needs_confirmation": False, "issue": None, "confirmed": False,
                "category": "not_found", "suggestion": None,
                "hint": "not supported"}

    def _resolve_needs_confirm(self):
        return {"ok": True, "address": EVM_ADDR,
                "source": "tokens_json", "needs_confirmation": True,
                "issue": "EVM address on solana chain", "confirmed": False,
                "category": "chain_mismatch", "suggestion": None, "hint": None}

    def test_rejects_unsupported_symbol(self):
        from nanobot_quant import pipeline as pl

        with mock.patch(
            "nanobot_quant.onchainos_cli.resolve_token",
            return_value=self._resolve_bad(),
        ):
            results = pl.run_from_signals([self._signal()])
        assert len(results) == 1
        r = results[0]
        assert r["risk_passed"] is False
        assert r["risk_details"]["error"] == "unsupported token"
        assert r["tx_hash"] is None
        assert r["suggested_order"] is None
        assert "SOL" in r["risk_details"]["supported"]

    def test_needs_confirmation_blocked_without_confirm(self):
        from nanobot_quant import pipeline as pl

        with mock.patch(
            "nanobot_quant.onchainos_cli.resolve_token",
            return_value=self._resolve_needs_confirm(),
        ):
            results = pl.run_from_signals([self._signal()])
        assert len(results) == 1
        r = results[0]
        assert r["risk_passed"] is False
        assert r["risk_details"]["error"] == "needs_confirmation"

    def test_needs_confirmation_lifted_with_confirm(self):
        from nanobot_quant import pipeline as pl

        with mock.patch(
            "nanobot_quant.onchainos_cli.resolve_token",
            return_value=self._resolve_needs_confirm(),
        ):
            results = pl.run_from_signals([self._signal()], confirm=True)
        assert len(results) == 1
        # the confirmation gate passed; the signal proceeds to the risk engine
        assert results[0]["risk_details"] != {"error": "needs_confirmation"}

    def test_accepts_native_token(self):
        from nanobot_quant import pipeline as pl

        with mock.patch(
            "nanobot_quant.onchainos_cli.resolve_token",
            return_value=self._resolve_ok(),
        ):
            results = pl.run_from_signals([self._signal("SOL")])
        assert len(results) == 1
        # the gate passed; the signal proceeds to the risk engine
        assert results[0]["risk_details"] != {"error": "unsupported token"}

    def test_contract_address_ticker_passes_gate(self):
        from nanobot_quant import pipeline as pl

        # resolve returns failure for the "symbol" (it is an address), but
        # the gate recognises the ticker as an already-resolved address
        with mock.patch(
            "nanobot_quant.onchainos_cli.resolve_token",
            return_value=self._resolve_bad(),
        ):
            results = pl.run_from_signals([self._signal(SOLANA_ADDR)])
        assert len(results) == 1
        assert results[0]["risk_details"] != {"error": "unsupported token"}
    def test_explicit_quantity_overrides_sizing(self):
        from nanobot_quant import pipeline as pl

        with mock.patch(
            "nanobot_quant.onchainos_cli.resolve_token",
            return_value=self._resolve_ok(),
        ):
            results = pl.run_from_signals(
                [self._signal("SOL")], quantity=0.058
            )
        assert len(results) == 1
        r = results[0]
        assert r["risk_details"] != {"error": "unsupported token"}
        assert r["suggested_order"] is not None
        assert r["suggested_order"]["quantity"] == 0.058
        assert r["position_value"] == pytest.approx(60000.0 * 0.058)

    def test_default_sizing_unchanged(self):
        from nanobot_quant import pipeline as pl
        from nanobot_quant.signal import TickerSignal

        # low price so the default sizing (pv=100000, 20% cap) passes risk:
        # int(100000*0.2/150)=133 → position 133*150=19950 ≤ 20000
        sig = TickerSignal(
            ticker="SOL", recommendation="BUY", score=6.0, price=150.0,
            confidence=0.4, setup_buy=9, setup_sell=0, cd_buy=0, cd_sell=0,
            tdst_support=140.0, tdst_resistance=160.0, rvol=1.2,
        )
        with mock.patch(
            "nanobot_quant.onchainos_cli.resolve_token",
            return_value=self._resolve_ok(),
        ):
            results = pl.run_from_signals([sig])
        assert len(results) == 1
        r = results[0]
        assert r["risk_passed"] is True
        assert r["suggested_order"]["quantity"] == 133
