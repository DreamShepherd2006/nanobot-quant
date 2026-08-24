"""Data source registry — discover all registered feeds.

仿 channel_bindings 模式：新增数据源 = data_sources/ 下一个文件 +
本文件一行 register()，消费方（td-table / td_live / 取价 / 回测 /
grounding）统一从 ``get_data_source(name)`` 取。
"""

from __future__ import annotations

from nanobot_quant.data_sources.base import (
    DataSourceSpec,
    REGISTRY,
    data_source_for_channel,
    executable_sources,
    get_data_source,
    list_data_sources,
    register,
    research_sources,
)
from nanobot_quant.data_sources import eastmoney, gate_cex, okx_cex, onchainos, yfinance

register(DataSourceSpec(
    name="gate_cex",
    display="Gate CEX",
    kind="executable",
    exchange="gate",
    fetch_kline=gate_cex.fetch_kline,
    get_price=gate_cex.get_price,
    order_book=gate_cex.order_book,
    ticker=gate_cex.ticker,
    bars=("1m", "3m", "5m", "15m", "30m",
          "1H", "2H", "4H", "6H", "8H", "12H",
          "1D", "3D", "1W", "7D", "30D"),
    # Gate API 实测 18 个 interval（2026-08-24），秒级 1s/10s 对 TD 无意义不纳入
    interval_map={
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1H": "1h", "2H": "2h", "4H": "4h", "6H": "6h",
        "8H": "8h", "12H": "12h",
        "1D": "1d", "3D": "3d", "1W": "1w", "7D": "7d", "30D": "30d",
    },
))
register(DataSourceSpec(
    name="onchainos",
    display="链上 DEX (OnchainOS)",
    kind="executable",
    exchange="dex",
    fetch_kline=onchainos.fetch_kline,
    get_price=onchainos.get_price,
    bars=("1m", "5m", "15m", "1H", "4H", "1D", "1W"),
))
register(DataSourceSpec(
    name="okx_cex",
    display="OKX CEX",
    kind="research",
    exchange="okx",
    fetch_kline=okx_cex.fetch_kline,
    get_price=okx_cex.get_price,
    order_book=okx_cex.order_book,
    ticker=okx_cex.ticker,
    bars=("1m", "5m", "15m", "1H", "4H", "1D", "1W"),
))
register(DataSourceSpec(
    name="eastmoney",
    display="股票（东财）",
    kind="research",
    fetch_kline=eastmoney.fetch_kline,
    bars=("1m", "5m", "15m", "1H", "1D", "1W"),
))
register(DataSourceSpec(
    name="yfinance",
    display="股票（yfinance）",
    kind="research",
    fetch_kline=yfinance.fetch_kline,
    bars=("1m", "5m", "15m", "1H", "1D", "1W"),
))

__all__ = [
    "DataSourceSpec",
    "REGISTRY",
    "data_source_for_channel",
    "executable_sources",
    "get_data_source",
    "list_data_sources",
    "register",
    "research_sources",
]
