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
    _lumibot.__path__ = []  # mark as package so submodules can import

    sys.modules.setdefault("lumibot", _lumibot)
    sys.modules.setdefault("lumibot.strategies", _strategies)
    sys.modules.setdefault("lumibot.strategies.strategy", _strategy_mod)

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
