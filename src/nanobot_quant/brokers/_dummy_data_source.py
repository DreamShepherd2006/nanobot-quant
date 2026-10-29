"""lumibot Broker 的占位数据源（两个 CEX broker 共用，单一事实源）。

Broker 基类要求 ``data_source`` 非空，但 CEX 通道的 K 线走 ``KlineCache`` +
数据源注册表（不经 lumibot DataSource）。占位源只需满足：

* ``get_chains``   —— Broker 基类初始化校验用
* ``get_datetime`` —— ``Strategy.get_datetime()`` 每轮被
  ``StrategyExecutor._handle_lifecycle_methods`` 调用；缺失会抛
  ``AttributeError: '_DummyDataSource' object has no attribute 'get_datetime'``，
  整个执行循环被 ``on_bot_crash`` 打死（2026-10-29 okx_cex 通道实测，
  此前两个 broker 各写一份占位类、都漏了该方法）

加密 24/7 连续市场，占位源返回当前 UTC 时间即以（与墙钟一致，不引入回测式的
bar 时间语义）。
"""

from __future__ import annotations

from datetime import datetime, timezone


class DummyDataSource:
    """占位数据源：满足 lumibot Broker / Strategy 的最小接口。"""

    def __init__(self, *args, **kwargs):
        pass

    def get_chains(self, asset=None, quote=None):
        """Broker 基类校验用；CEX 通道无链概念。"""
        return {}

    def get_datetime(self, adjust_for_delay: bool = False):
        """当前时间（**aware UTC**）。

        必须 aware：lumibot ``StrategyExecutor._handle_lifecycle_methods`` 会把它
        与 ``broker.market_close_time()``（经 ``utc_to_local`` 必为 aware）比较；
        返回 naive 会在每一轮抛
        ``TypeError: can't compare offset-naive and offset-aware datetimes``，
        进而 ``on_bot_crash`` 打死整个 TD 循环（2026-10-29 okx_cex 通道实测）。
        """
        return datetime.now(timezone.utc)

    def get_last_price(self, asset=None, quote=None):
        """占位源不提供行情（取价统一走数据源注册表）；返回 None 不抛。"""
        return None
