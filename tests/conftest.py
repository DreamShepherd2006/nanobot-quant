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

import pytest

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
        # 与 lumibot v4.5.78 真实签名一致：无 current_price 参数
        # （注释明确 current_price 等属性须在构造后赋值）
        def __init__(self, strategy=None, asset=None, quantity=0,
                     orders=None, hold=0, available=0, avg_fill_price=None):
            self.strategy = strategy
            self.asset = asset
            self.quantity = quantity
            self.orders = orders
            self.hold = hold
            self.available = available
            self.avg_fill_price = avg_fill_price
            self.current_price = avg_fill_price

    class _Bars:
        def __init__(self, df, source, asset, quote=None, raw=None, return_polars=False, tzinfo=None):
            # 镜像 lumibot v4.5.78 Bars.__init__：无 return 列时（needs_derived=True）
            # 派生 return 列访问小写列 df["close"]——列名契约不一致（如 gate_cex
            # 大写列）直接抛 KeyError，让单测能捕获真实实盘路径的崩溃
            # （2026-08-17 A 修复的回归保护）。
            if hasattr(df, "columns") and "return" not in df.columns and "close" not in df.columns:
                raise KeyError("close")
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

    # backtest_runner imports lumibot.backtesting.YahooDataBacktesting
    # at module level — provide a stub so tests can import the module.
    _backtesting = types.ModuleType("lumibot.backtesting")

    class _YahooBacktesting:
        pass

    _backtesting.YahooDataBacktesting = _YahooBacktesting
    _lumibot.backtesting = _backtesting
    _lumibot.__path__ = []  # mark as package so submodules can import

    sys.modules.setdefault("lumibot", _lumibot)
    sys.modules.setdefault("lumibot.strategies", _strategies)
    sys.modules.setdefault("lumibot.strategies.strategy", _strategy_mod)
    sys.modules.setdefault("lumibot.brokers", _brokers)
    sys.modules.setdefault("lumibot.entities", _entities)
    sys.modules.setdefault("lumibot.data_sources", _data_sources)
    sys.modules.setdefault("lumibot.backtesting", _backtesting)

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

try:
    import gate_api  # noqa: F401
except ImportError:
    # nanobot_quant.gate_sdk imports gate_api at module level (direct
    # import, no fallback — same policy as lumibot). The test container
    # does not install the gate-api SDK, so inject a minimal stub before
    # collection: model objects support kwargs + to_dict() (None values
    # dropped, mirroring real SDK serialization) and API classes record
    # calls for gate_sdk tests.
    _gate = types.ModuleType("gate_api")

    class _Configuration:
        def __init__(self, key="", secret="", **kwargs):
            self.key = key
            self.secret = secret

    class _ApiClient:
        def __init__(self, configuration=None, **kwargs):
            self.configuration = configuration

    class _ApiException(Exception):
        def __init__(self, status=None, reason=None, http_resp=None):
            super().__init__(status, reason)
            self.status = status
            self.reason = reason
            self.http_resp = http_resp

    class _Model:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def to_dict(self):
            return {k: v for k, v in self.__dict__.items() if v is not None}

    class _Order(_Model):
        pass

    class _CurrencyPair(_Model):
        pass

    class _SubAccountTransfer(_Model):
        pass

    class _SubAccountBalance(_Model):
        pass

    class _SpotApi:
        def __init__(self, api_client=None, **kwargs):
            self.api_client = api_client

        def create_order(self, order, **kwargs):
            return order

        def get_order(self, order_id, currency_pair, **kwargs):
            return _Order(id=order_id, currency_pair=currency_pair, status="closed")

        def cancel_order(self, order_id, currency_pair, **kwargs):
            return _Order(id=order_id, currency_pair=currency_pair, status="cancelled")

        def get_currency_pair(self, currency_pair, **kwargs):
            return _CurrencyPair(currency_pair=currency_pair, trade_status="tradable")

        def list_tickers(self, **kwargs):
            return []

    class _WalletApi:
        def __init__(self, api_client=None, **kwargs):
            self.api_client = api_client

        def transfer_with_sub_account(self, sub_account_transfer, **kwargs):
            return sub_account_transfer

        def list_sub_account_balances(self, sub_uid=None, page=None, limit=None, **kwargs):
            return []

    for _name, _obj in {
        "Configuration": _Configuration,
        "ApiClient": _ApiClient,
        "ApiException": _ApiException,
        "Order": _Order,
        "CurrencyPair": _CurrencyPair,
        "SubAccountTransfer": _SubAccountTransfer,
        "SubAccountBalance": _SubAccountBalance,
        "SpotApi": _SpotApi,
        "WalletApi": _WalletApi,
    }.items():
        setattr(_gate, _name, _obj)
    sys.modules.setdefault("gate_api", _gate)


@pytest.fixture(autouse=True)
def _isolate_batches_path(tmp_path, monkeypatch):
    """隔离批次台账写盘路径（2026-08-20 S1）。

    batches_path() 硬编码探测 /data、/mnt/workspace——测试环境（Nightly
    容器存在 /data）会写真实 /data/legion/credentials/batches.*.json。
    """
    from nanobot_quant import batches as _batches

    def fake_batches_path(symbol=None, channel=None, scene=None):
        if channel and symbol:
            fname = (
                f"batches.{channel}.{scene}.{symbol}.json"
                if scene
                else f"batches.{channel}.{symbol}.json"
            )
        elif symbol:
            fname = f"batches.{symbol}.json"
        else:
            fname = "batches.json"
        return tmp_path / fname

    monkeypatch.setattr(_batches, "batches_path", fake_batches_path)


@pytest.fixture(autouse=True)
def _isolate_exec_params(tmp_path, monkeypatch):
    """隔离 exec_params.json 候选路径（2026-08-21 页面场景化）。

    exec_params_path() 探测 /data、/mnt/workspace 真实持久卷——测试环境
    （Nightly 容器存在 /data）会读到真实 exec_params.json（如
    execution_channel=gate），导致 td-table 页面测试默认源解析为真实
    Gate 数据源、走网络调用，与 mock（仅覆盖 onchainos）不符而失败。
    隔离后 load_exec_params() 读不到文件 → 返回默认（okx_dex、无 scenes）。
    """
    from nanobot_quant import exec_params as _ep

    monkeypatch.setattr(_ep, "exec_params_path", lambda: tmp_path / "exec_params.json")
    from nanobot_quant import exec_params as _ep

    monkeypatch.setattr(_ep, "exec_params_path", lambda: tmp_path / "exec_params.json")


@pytest.fixture(autouse=True)
def _reset_td_stop_requested():
    """测试隔离 stop_requested（2026-08-21 延迟停止方案）。

    stop_requested 是 td_live_state 模块级 Event：runner.stop() 测试会
    置位（延迟停止后台线程），若不清除会污染后续 test_td_sequential_strategy
    的 on_trading_iteration 测试（装饰器守卫直接 return 导致断言失败）。
    每个测试结束后复位。
    """
    from nanobot_quant import td_live_state

    yield
    td_live_state.stop_requested.clear()
