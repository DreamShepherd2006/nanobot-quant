"""Shared pytest fixtures for nanobot-quant tests.

The CI/test container does not install lumibot (a heavy optional runtime
dependency used only by the backtest strategy).  The strategy module
imports ``lumibot.strategies.strategy.Strategy`` at module level, so we
inject a minimal stub *before* collection when lumibot is unavailable —
enough for static parameter assertions; engine/params/handler tests do
not touch lumibot at all.
"""

import logging
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
            self.portfolio_value = float(kwargs.get("initial_cash", 0.0))
            self.cash = self.portfolio_value
            self.logger = logging.getLogger("lumibot-stub")
            # td_live/backtest driver 构造传 broker=... → 保存供
            # get_historical_prices 委托 broker.data_source。
            broker = kwargs.get("broker")
            if broker is not None:
                self.broker = broker

        def get_historical_prices(self, asset, length, timestep="", **kwargs):
            # 镜像 lumibot v4.5.78：Strategy.get_historical_prices 先归一化
            # str symbol → Asset，再委托 broker.data_source（回测驱动 Step 3
            # 依赖此链路；策略实盘传 self.symbol 字符串）。
            ds = getattr(getattr(self, "broker", None), "data_source", None)
            if ds is None:
                raise RuntimeError("Strategy.get_historical_prices: no broker.data_source")
            if isinstance(asset, str):
                from lumibot.entities import Asset

                asset = Asset(asset, "crypto")
            return ds.get_historical_prices(asset, length, timestep=timestep, **kwargs)

        def create_order(self, asset, quantity, side, **kwargs):
            from lumibot.entities import Asset, Order

            # 镜像 lumibot v4.5.78：create_order 接受 str symbol 或 Asset。
            if isinstance(asset, str):
                asset = Asset(asset, "crypto")
            return Order(
                strategy=self, asset=asset, quantity=quantity, side=side, **kwargs
            )

        def get_position(self, symbol):
            # 镜像 lumibot v4.5.78：从 broker 当前持仓查询（回测驱动
            # BacktestBroker._positions 为内存账本；str stub 归一化）。
            positions = getattr(getattr(self, "broker", None), "_positions", None)
            if isinstance(positions, dict) and symbol in positions:
                from lumibot.entities import Asset, Position

                pos = Position(self, Asset(symbol, "crypto"), positions[symbol])
                pos.current_price = 0.0
                return pos
            return None

    _strategy_mod.Strategy = _Strategy
    _strategies.strategy = _strategy_mod
    _lumibot.strategies = _strategies

    # onchainos_broker imports lumibot.brokers.Broker (base class) and
    # lumibot.entities.{Asset,Position} lazily in _pull_positions.
    _brokers = types.ModuleType("lumibot.brokers")

    class _Broker:
        def __init__(self, *args, **kwargs):
            # 镜像 lumibot v4.5.78：Broker 保存 data_source（策略
            # get_historical_prices 委托 broker.data_source，回测驱动依赖）。
            self.data_source = kwargs.get("data_source")
            self.name = kwargs.get("name", "stub")

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

    class _Order:
        # 镜像 lumibot v4.5.78 entities.Order 的必用成员：
        # set_filled 只设 event 不更新 status（backtest/cex broker 手动同步 status="fill"）、
        # custom_params 默认 None（写入前需先置 dict）、identifier 默认 None。
        def __init__(self, strategy=None, identifier=None, asset=None, quantity=0,
                     side="buy", status="new", limit_price=None, stop_price=None,
                     custom_params=None, error=None):
            self.strategy = strategy
            self.identifier = identifier
            self.asset = asset
            self.quantity = quantity
            self.side = side
            self.status = status
            self.limit_price = limit_price
            self.stop_price = stop_price
            self.custom_params = custom_params
            self.error = error
            self.filled = False
            self._event = None

        def set_filled(self):
            self._event = "fill"
            self.filled = True

        def set_error(self, msg):
            self._event = "error"
            self.error = msg

        def set_canceled(self):
            self._event = "cancel"

        def set_identifier(self, oid):
            self.identifier = oid

        def is_filled(self):
            return self.filled is True or self.status == "fill"

    _entities.Asset = _Asset
    _entities.Position = _Position
    _entities.Bars = _Bars
    _entities.Order = _Order

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

    # gate-api SDK stub (not installed in the test container) — gate_sdk.py
    # imports these names at module level; individual SDK methods are mocked
    # per-test via monkeypatch on nanobot_quant.gate_sdk. The stub mirrors the
    # real SDK surface used by the wrapper (model kwargs + ApiException fields)
    # so test_gate_sdk.py can build fake models/errors.
    _gate_api = types.ModuleType("gate_api")

    class _ApiClient:
        pass

    class _Configuration:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _ApiException(Exception):
        def __init__(self, status=None, reason=None, body=None):
            super().__init__(f"{status} {reason or ''}".strip())
            self.status = status
            self.reason = reason
            self.body = body

    class _Model:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

        def to_dict(self):
            # mirror official SDK model serialization: skip None fields
            return {k: v for k, v in self.__dict__.items() if v is not None}

    _gate_api.ApiClient = _ApiClient
    _gate_api.ApiException = _ApiException
    _gate_api.Configuration = _Configuration
    _gate_api.Order = _Model
    _gate_api.SpotApi = _Model
    _gate_api.SubAccountBalance = _Model
    _gate_api.SubAccountTransfer = _Model
    _gate_api.WalletApi = _Model
    _gate_api.CurrencyPair = _Model
    sys.modules.setdefault("gate_api", _gate_api)
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

try:
    import okx  # noqa: F401
except ImportError:
    # okx_sdk (期权线统一接入层) imports okx.{public,market,account} at
    # module level. The CI/test container does not install the official
    # python-okx SDK, so inject minimal stubs: classes carry the real
    # 1.0.9 constructor signatures + API_URL attribute; method bodies are
    # never exercised (tests monkeypatch okx_sdk.public/market/account_for).
    _okx = types.ModuleType("okx")

    class _Public:
        API_URL = "https://www.okx.com"

        def __init__(self, flag="0", **kwargs):
            self.flag = flag

        def get_instruments(self, **kwargs):
            return {"code": "0", "data": [], "msg": ""}

        def get_opt_summary(self, **kwargs):
            return {"code": "0", "data": [], "msg": ""}

    class _Market:
        API_URL = "https://www.okx.com"

        def __init__(self, flag="0", **kwargs):
            self.flag = flag

        def get_ticker(self, **kwargs):
            return {"code": "0", "data": [], "msg": ""}

        def get_tickers(self, **kwargs):
            return {"code": "0", "data": [], "msg": ""}

        def get_history_candles(self, **kwargs):
            return {"code": "0", "data": [], "msg": ""}

    class _Account:
        API_URL = "https://www.okx.com"

        def __init__(self, key="", secret="", passphrase="", flag="0", **kwargs):
            self.key = key
            self.secret = secret
            self.passphrase = passphrase
            self.flag = flag

        def get_config(self):
            return {"code": "0", "data": [], "msg": ""}

        def get_balance(self, **kwargs):
            return {"code": "0", "data": [], "msg": ""}

        def get_positions(self, **kwargs):
            return {"code": "0", "data": [], "msg": ""}

        def get_trade_fee(self, **kwargs):
            return {"code": "0", "data": [], "msg": ""}

    _pub_mod = types.ModuleType("okx.public")
    _pub_mod.Public = _Public
    _mkt_mod = types.ModuleType("okx.market")
    _mkt_mod.Market = _Market
    _acc_mod = types.ModuleType("okx.account")
    _acc_mod.Account = _Account
    sys.modules.setdefault("okx", _okx)
    sys.modules.setdefault("okx.public", _pub_mod)
    sys.modules.setdefault("okx.market", _mkt_mod)
    sys.modules.setdefault("okx.account", _acc_mod)


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

@pytest.fixture(autouse=True)
def _isolate_price_cache():
    """测试隔离 CexBroker 价格缓存（2026-08-22 类级共享）。

    价格缓存是类属性（同轮三场景 broker 共享），测试之间会互相污染——
    前一个测试缓存的价（TTL 15s 未过）会让后一个测试的取价调用命中缓存、
    calls 断言失败。每个测试前后清空。
    """
    from nanobot_quant.brokers.cex_broker import CexBroker
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    CexBroker._price_cache.clear()
    TdSequentialStrategy._price_cache.clear()
    yield
    CexBroker._price_cache.clear()
    TdSequentialStrategy._price_cache.clear()

