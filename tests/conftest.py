"""Shared pytest fixtures for nanobot-quant tests.

The CI/test container does not install lumibot (a heavy optional runtime
dependency used only by the backtest strategy).  The strategy module
imports ``lumibot.strategies.strategy.Strategy`` at module level, so we
inject a minimal stub *before* collection when lumibot is unavailable —
enough for static parameter assertions; engine/params/handler tests do
not touch lumibot at all.
"""

import sys
import types

try:
    import lumibot  # noqa: F401
except ImportError:
    _lumibot = types.ModuleType("lumibot")
    _strategies = types.ModuleType("lumibot.strategies")
    _strategy_mod = types.ModuleType("lumibot.strategies.strategy")

    class _Strategy:
        """Minimal stand-in — supports class-level ``parameters`` access."""

        parameters = {}

        def __init__(self, *args, **kwargs):
            self.parameters = dict(self.parameters)

    _strategy_mod.Strategy = _Strategy
    _strategies.strategy = _strategy_mod
    _lumibot.strategies = _strategies

    # onchainos_broker imports lumibot.brokers.Broker (base class) and
    # lumibot.entities.{Asset,Position} lazily in _pull_positions.
    _brokers = types.ModuleType("lumibot.brokers")

    class _Broker:
        def __init__(self, *args, **kwargs):
            pass

    _brokers.Broker = _Broker

    _entities = types.ModuleType("lumibot.entities")

    class _Asset:
        def __init__(self, symbol="", asset_type=""):
            self.symbol = symbol
            self.asset_type = asset_type

    class _Position:
        def __init__(self, strategy=None, asset=None, quantity=0, current_price=0):
            self.asset = asset
            self.quantity = quantity
            self.current_price = current_price

    class _Bars:
        def __init__(self, df, source, asset, quote=None, raw=None, return_polars=False, tzinfo=None):
            self.df = df
            self.source = str(source).upper()
            self.asset = asset
            self.symbol = getattr(asset, "symbol", "")
            self.quote = quote

    _entities.Asset = _Asset
    _entities.Position = _Position
    _entities.Bars = _Bars

    # onchainos_data_source imports lumibot.data_sources.DataSource
    _data_sources = types.ModuleType("lumibot.data_sources")

    class _DataSource:
        SOURCE = "stub"

        def __init__(self, *args, **kwargs):
            pass

    _data_sources.DataSource = _DataSource

    _lumibot.brokers = _brokers
    _lumibot.entities = _entities
    _lumibot.data_sources = _data_sources
    _lumibot.__path__ = []  # mark as package so submodules can import

    sys.modules.setdefault("lumibot", _lumibot)
    sys.modules.setdefault("lumibot.strategies", _strategies)
    sys.modules.setdefault("lumibot.strategies.strategy", _strategy_mod)
    sys.modules.setdefault("lumibot.brokers", _brokers)
    sys.modules.setdefault("lumibot.entities", _entities)
    sys.modules.setdefault("lumibot.data_sources", _data_sources)

try:
    import yfinance  # noqa: F401
except ImportError:
    _yf = types.ModuleType("yfinance")

    def _download(*args, **kwargs):
        raise RuntimeError("yfinance stub — not installed in test container")

    _yf.download = _download
    sys.modules.setdefault("yfinance", _yf)

try:
    import pandas  # noqa: F401
except ImportError:
    # pipeline.py imports pandas at module level but only uses it for
    # type annotations (pd.DataFrame) and an isinstance check
    # (pd.MultiIndex); a minimal stub keeps pipeline tests runnable
    # without the heavy pandas/numpy stack.
    _pd = types.ModuleType("pandas")

    class _MultiIndex:
        pass

    _pd.DataFrame = object
    _pd.MultiIndex = _MultiIndex
    sys.modules.setdefault("pandas", _pd)
