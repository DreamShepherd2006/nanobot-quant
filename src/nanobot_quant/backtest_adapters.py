"""OnchainOS ↔ Lumibot bridge for crypto backtesting.

Provides a Lumibot-compatible data source that fetches kline data from
OnchainOS and feeds it into the backtesting engine.

Usage::

    from nanobot_quant.backtest_adapters import create_onchainos_backtesting

    ds_class = create_onchainos_backtesting("WETH/USDC", "2024-01-01", "2025-01-01")
    TdSequentialStrategy.run_backtest(ds_class, start_dt, end_dt, ...)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from nanobot_quant.onchainos_data import CHAIN_IDS, TOKENS, fetch_kline_range

logger = logging.getLogger(__name__)

# ── Crypto pair → (chain, base_token, quote_token) mapping ─────────
CRYPTO_PAIRS: dict[str, tuple[str, str, str]] = {
    # EVM chains (Arbitrum, Ethereum)
    "WETH/USDC": ("arbitrum", "WETH", "USDC"),
    "WBTC/WETH": ("arbitrum", "WBTC", "WETH"),
    "WBTC/USDC": ("arbitrum", "WBTC", "USDC"),
    "WETH/USDT": ("ethereum", "WETH", "USDT"),
    "WBTC/USDT": ("ethereum", "WBTC", "USDT"),
    # Solana SPL pairs
    "SOL/USDC": ("solana", "SOL", "USDC"),
    "CRCLx/USDC": ("solana", "CRCLx", "USDC"),
}


def _resolve_pair(pair: str) -> tuple[str, str, str]:
    """Convert a trading pair like 'WETH/USDC' to (chain, base_addr, quote).
    
    Checks CRYPTO_PAIRS first, then tries to resolve tokens by looking
    up TOKENS for each symbol independently with a default chain.
    """
    key = pair.upper()
    if key in CRYPTO_PAIRS:
        return CRYPTO_PAIRS[key]

    parts = pair.split("/")
    if len(parts) != 2:
        raise ValueError(
            f"Unsupported pair format: {pair!r}. Expected 'BASE/QUOTE' like 'WETH/USDC'."
        )

    base_sym, quote_sym = parts[0].strip().upper(), parts[1].strip().upper()

    # Find a chain that has both tokens
    from nanobot_quant.onchainos_data import TOKENS as T

    base_addrs = T.get(base_sym, {})
    for chain_name in sorted(base_addrs.keys()):
        quote_addrs = T.get(quote_sym, {})
        if chain_name in quote_addrs:
            return (chain_name, base_sym, quote_sym)

    raise ValueError(
        f"No common chain found for pair {pair!r}. "
        f"Supported tokens: {list(T.keys())}. "
        f"Add the pair to CRYPTO_PAIRS in backtest_adapters.py."
    )


def _parse_date(date_str: str) -> datetime:
    """Parse a date string like '2024-01-01' to UTC datetime."""
    return datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)


def fetch_crypto_kline(
    pair: str,
    start: str | datetime,
    end: str | datetime,
    bar: str = "1D",
) -> pd.DataFrame:
    """Fetch historical kline data for a crypto pair via OnchainOS.

    Args:
        pair: Trading pair like 'WETH/USDC'.
        start: Start date as 'YYYY-MM-DD' or datetime.
        end: End date as 'YYYY-MM-DD' or datetime.
        bar: Candle interval (1D, 1H, 4H, etc.).

    Returns:
        DataFrame with columns [open, high, low, close, volume],
        timestamp index (UTC).
    """
    chain, base_sym, quote_sym = _resolve_pair(pair)

    if isinstance(start, str):
        start = _parse_date(start)
    if isinstance(end, str):
        end = _parse_date(end)

    # Resolve base token address
    from nanobot_quant.onchainos_data import resolve_token

    base_addr = resolve_token(chain, base_sym)
    logger.info(
        "Fetching OnchainOS kline: pair=%s chain=%s base=%s(%s) %s→%s",
        pair, chain, base_sym, base_addr, start.date(), end.date(),
    )

    df = fetch_kline_range(chain, base_addr, start, end, bar=bar)
    if df.empty:
        logger.warning("OnchainOS returned empty data for %s %s→%s", pair, start, end)
    else:
        logger.info("Got %d candles for %s", len(df), pair)

    return df


def create_onchainos_backtesting(
    pair: str,
    start: str | datetime,
    end: str | datetime,
    bar: str = "1D",
) -> type:
    """Create a Lumibot PandasDataBacktesting subclass backed by OnchainOS.

    Returns a *class* (not an instance) so it can be passed directly
    to ``Strategy.run_backtest()``.

    Example::

        ds = create_onchainos_backtesting("WETH/USDC", "2024-01-01", "2025-01-01")
        TdSequentialStrategy.run_backtest(ds, start_dt, end_dt, ...)
    """
    import lumibot
    from lumibot.backtesting import PandasDataBacktesting
    from lumibot.entities import Asset, Data

    df = fetch_crypto_kline(pair, start, end, bar=bar)
    if df.empty:
        raise RuntimeError(f"No kline data for {pair}")

    # Build a DataFrame in Lumibot's expected format:
    # columns: [open, high, low, close, volume, dividend]
    # index must be datetime64[ns] (no tz)
    df_lumibot = df.copy()
    # Make index tz-naive: if tz-aware, convert to UTC then drop tz
    idx = pd.DatetimeIndex(df_lumibot.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    df_lumibot.index = idx
    # Normalize to midnight so daily bars align with Lumibot's backtest
    # session (opens at 09:30 ET / 13:30 UTC). OnchainOS bars land at
    # 16:00 UTC — shifting to 00:00 prevents a 16 h gap.
    df_lumibot.index = df_lumibot.index.normalize()
    if "dividend" not in df_lumibot.columns:
        df_lumibot["dividend"] = 0.0

    # Normalize column names: lower case
    col_map = {}
    for c in df_lumibot.columns:
        if c.lower() in ("open", "high", "low", "close", "volume"):
            col_map[c] = c.lower()
    df_lumibot = df_lumibot.rename(columns=col_map)

    # Add synthetic bid/ask for Lumibot broker fill simulation
    df_lumibot = _add_synthetic_quotes(df_lumibot)

    base, quote = pair.split("/")
    base_sym = base.strip()
    quote_sym = quote.strip()

    asset_obj = Asset(symbol=base_sym, asset_type="stock")
    quote_obj = Asset(symbol=quote_sym, asset_type="stock")

    data_obj = Data(
        asset=asset_obj,
        quote=quote_obj,
        df=df_lumibot,
        timestep="day" if bar.endswith("D") else "minute",
    )

    # Create a subclass with preloaded data
    class OnchainOSBacktesting(PandasDataBacktesting):
        SOURCE = "ONCHAINOS"

        def __init__(self, datetime_start, datetime_end, **kwargs):
            pd_data = {asset_obj: data_obj}
            if "pandas_data" in kwargs:
                extra = kwargs.pop("pandas_data")
                if extra:
                    pd_data.update(extra)
            super().__init__(
                datetime_start=datetime_start,
                datetime_end=datetime_end,
                pandas_data=pd_data,
                **kwargs,
            )
            # Also register under (asset, forex-quote) tuple for
            # _pull_source_symbol_bars lookups that use the 2-tuple key form.
            _usd = Asset(symbol="USD", asset_type="forex")
            _base_stock = Asset(symbol=base_sym, asset_type="stock")
            self._data_store[(_base_stock, _usd)] = data_obj
            self._data_store[(asset_obj, quote_obj)] = data_obj

    return OnchainOSBacktesting


def create_okx_cex_backtesting(
    symbol: str,
    start: str | datetime,
    end: str | datetime,
    bar: str = "1D",
) -> type:
    """Create a Lumibot PandasDataBacktesting subclass backed by OKX CEX.

    Returns a *class* (not an instance) so it can be passed directly
    to ``Strategy.run_backtest()``.

    Example::

        ds = create_okx_cex_backtesting("XSPCX", "2024-07-01", "2025-07-01")
        TdSequentialStrategy.run_backtest(ds, start_dt, end_dt, ...)
    """
    from lumibot.backtesting import PandasDataBacktesting
    from lumibot.entities import Asset, Data

    from nanobot_quant.okx_cex_data import fetch_kline_range

    if isinstance(start, datetime):
        start = start.strftime("%Y-%m-%d")
    if isinstance(end, datetime):
        end = end.strftime("%Y-%m-%d")

    df = fetch_kline_range(symbol, start, end, bar=bar)
    if df.empty:
        raise RuntimeError(f"No kline data for {symbol}")

    # Detect asset type: tokenised stocks start with "X" (e.g. "XAAPL"),
    # crypto tickers do not (e.g. "BTC", "ETH")
    asset_type = "stock" if symbol.upper().startswith("X") else "crypto"

    # Build a DataFrame in Lumibot's expected format
    df_lumibot = df.copy()
    idx = pd.DatetimeIndex(df_lumibot.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    df_lumibot.index = idx
    # Normalize to midnight so daily bars align with Lumibot's backtest
    # session (opens at 09:30 ET / 13:30 UTC). OnchainOS bars land at
    # 16:00 UTC — shifting to 00:00 prevents a 16 h gap.
    df_lumibot.index = df_lumibot.index.normalize()
    if "dividend" not in df_lumibot.columns:
        df_lumibot["dividend"] = 0.0

    # Normalize column names: lower case
    col_map = {}
    for c in df_lumibot.columns:
        if c.lower() in ("open", "high", "low", "close", "volume"):
            col_map[c] = c.lower()
    df_lumibot = df_lumibot.rename(columns=col_map)

    # Add synthetic bid/ask for Lumibot broker fill simulation
    df_lumibot = _add_synthetic_quotes(df_lumibot)

    asset_obj = Asset(symbol=symbol, asset_type=asset_type)
    quote_obj = Asset(symbol="USDT", asset_type=asset_type)

    data_obj = Data(
        asset=asset_obj,
        quote=quote_obj,
        df=df_lumibot,
        timestep="day" if bar.endswith("D") else "minute",
    )

    class OKXCexBacktesting(PandasDataBacktesting):
        SOURCE = "OKX_CEX"

        def __init__(self, datetime_start, datetime_end, **kwargs):
            pd_data = {asset_obj: data_obj}
            if "pandas_data" in kwargs:
                extra = kwargs.pop("pandas_data")
                if extra:
                    pd_data.update(extra)
            super().__init__(
                datetime_start=datetime_start,
                datetime_end=datetime_end,
                pandas_data=pd_data,
                **kwargs,
            )
            _usd = Asset(symbol="USD", asset_type="forex")
            _base_stock = Asset(symbol=symbol, asset_type="stock")
            self._data_store[(_base_stock, _usd)] = data_obj
            self._data_store[(asset_obj, quote_obj)] = data_obj

    return OKXCexBacktesting

def _add_synthetic_quotes(df: pd.DataFrame, spread_pct: float = 0.001) -> pd.DataFrame:
    """Add synthetic bid/ask columns for Lumibot broker fill simulation.

    OnchainOS kline data only provides OHLCV. Lumibot's backtesting broker
    requires bid/ask/bid_size/ask_size columns to fill market orders.
    This generates them from close price with a configurable spread.

    Args:
        df: DataFrame with at least a 'close' column.
        spread_pct: Half-spread as fraction (default 0.001 = 0.1%).
            bid = close × (1 - spread_pct)
            ask = close × (1 + spread_pct)

    Returns:
        DataFrame with added 'bid', 'ask', 'bid_size', 'ask_size' columns.
    """
    df = df.copy()
    df["bid"] = df["close"] * (1 - spread_pct)
    df["ask"] = df["close"] * (1 + spread_pct)
    # Large sizes so backtesting broker never rejects for low liquidity
    df["bid_size"] = 1_000_000
    df["ask_size"] = 1_000_000
    return df
