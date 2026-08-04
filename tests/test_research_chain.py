"""Tests for the research-chain safety gates and helpers.

Covers:
- ``onchainos_cli`` symbol helpers (bare_symbol / is_contract_address /
  supported_symbols / chain_results_dir) — runnable anywhere.
- ``pipeline.run_from_signals`` fail-closed token gate — requires pandas
  + lumibot (Nightly container), guarded with importorskip.
"""

import json
from unittest import mock

import pytest

from nanobot_quant.onchainos_cli import (
    bare_symbol,
    chain_results_dir,
    is_contract_address,
    supported_symbols,
)

SOLANA_ADDR = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
EVM_ADDR = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"


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
    def test_dir_name(self):
        assert chain_results_dir().name == "research_chains"


class TestPipelineTokenGate:
    """fail-closed gate: unsupported symbols never reach the broker."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        # pipeline.py imports pandas at module level; lumibot is only
        # imported lazily inside the live-broker branch.
        return pytest.importorskip("pandas")

    def test_rejects_unsupported_symbol(self):
        from nanobot_quant import pipeline as pl

        from nanobot_quant.signal import TickerSignal

        signal = TickerSignal(
            ticker="BTC",
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
        agg = pl.AnalysisPipeline()
        with mock.patch(
            "nanobot_quant.onchainos_cli.resolve_token_address",
            return_value=None,
        ):
            results = agg.run_from_signals([signal])
        assert len(results) == 1
        r = results[0]
        assert r["risk_passed"] is False
        assert r["risk_details"]["error"] == "unsupported token"
        assert r["tx_hash"] is None
        assert r["suggested_order"] is None
        assert "SOL" in r["risk_details"]["supported"]

    def test_accepts_native_token(self):
        from nanobot_quant import pipeline as pl

        from nanobot_quant.signal import TickerSignal

        signal = TickerSignal(
            ticker="SOL",
            recommendation="BUY",
            score=6.0,
            price=180.0,
            confidence=0.6,
            setup_buy=9,
            setup_sell=0,
            cd_buy=0,
            cd_sell=0,
            tdst_support=170.0,
            tdst_resistance=190.0,
            rvol=1.1,
        )
        agg = pl.AnalysisPipeline()
        with mock.patch(
            "nanobot_quant.onchainos_cli.resolve_token_address",
            return_value="So11111111111111111111111111111111111111112",
        ):
            results = agg.run_from_signals([signal])
        assert len(results) == 1
        # the gate passed; the signal proceeds to the risk engine
        assert results[0]["risk_details"] != {"error": "unsupported token"}

    def test_contract_address_ticker_passes_gate(self):
        from nanobot_quant import pipeline as pl

        from nanobot_quant.signal import TickerSignal

        signal = TickerSignal(
            ticker=SOLANA_ADDR,
            recommendation="BUY",
            score=5.0,
            price=1.0,
            confidence=0.5,
            setup_buy=9,
            setup_sell=0,
            cd_buy=0,
            cd_sell=0,
            tdst_support=0.99,
            tdst_resistance=1.01,
            rvol=1.0,
        )
        agg = pl.AnalysisPipeline()
        # resolve returns None for the "symbol" (it is an address), but the
        # gate recognises the ticker as an already-resolved address
        with mock.patch(
            "nanobot_quant.onchainos_cli.resolve_token_address",
            return_value=None,
        ):
            results = agg.run_from_signals([signal])
        assert len(results) == 1
        assert results[0]["risk_details"] != {"error": "unsupported token"}
