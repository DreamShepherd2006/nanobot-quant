"""Lumibot DataSource for OKX USDSⓈ-M linear crypto options (option_source).

挂到 ``Broker(option_source=...)``：lumibot 策略端用 ``self.get_chains(base)``
拿期权链、用报价/历史接口取权利金。数据侧**只读**：链、盘口、mark K 线全部走
``okx_options_data``（官方 python-okx + 公共行情端点），不下单、不碰台账。

mark K 线而非成交 K 线：期权成交稀疏，mark 价由交易所模型持续计算、无成交
时段也连续，是权利金曲线的正确载体（docs/quant-system.md §33.19）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from lumibot.data_sources import DataSource
from lumibot.entities import Asset, Bars

from nanobot_quant import okx_options_data as ood
from nanobot_quant.okx_options_assets import asset_to_inst, family_of, lot_of

logger = logging.getLogger("nanobot_quant.data.okx_options")

# lumibot timestep → OKX 期权 mark K 线周期（与 okx_options_data.LIFECYCLE_BARS 同源）
_BAR_MAP = {
    "minute": "1m", "1min": "1m", "3min": "3m", "5min": "5m",
    "15min": "15m", "30min": "30m", "hour": "1H", "1hour": "1H",
    "2hour": "2H", "4hour": "4H", "6hour": "6H", "12hour": "12H",
    "day": "1D", "1day": "1D", "3day": "3D", "week": "1W",
    "7day": "7D", "30day": "30D",
}
_DEFAULT_BAR = "15m"


def _is_option(asset) -> bool:
    return str(getattr(asset, "asset_type", "") or "").lower() == "option" or \
        getattr(asset, "strike", None) is not None


class OkxOptionsDataSource(DataSource):
    """OKX 期权行情（链 / 报价 / mark K 线），只读。"""

    SOURCE = "okx_options"
    MIN_TIMESTEP = "minute"
    IS_BACKTESTING_DATA_SOURCE = False

    # ── 期权链 ────────────────────────────────────────────────────────────
    def get_chains(self, asset=None, quote=None) -> dict:
        """lumibot 期权链结构：

        ``{"Multiplier": "0.1",
           "Chains": {"CALL": {"2026-09-18": [90.0, 92.0]},
                      "PUT":  {"2026-09-18": [90.0, 92.0]}}}``

        Multiplier 取每张面值（SOL 0.1 / BTC 0.01），与 Asset 构造时传入的
        multiplier 同一来源——lumibot 用它换算合约张数↔币数。
        """
        base = str(getattr(asset, "symbol", "") or "").upper()
        family = family_of(base)
        data = ood.fetch_chain(family, spot_pct_range=None)  # 全链，不按 ±20% 过滤
        chains: dict = {"CALL": {}, "PUT": {}}
        for group in data.get("groups", []):
            exp_date = group.get("date") or ""
            calls, puts = [], []
            for row in group.get("rows", []):
                strike = float(row.get("strike"))
                if (row.get("C") or {}).get("inst_id"):
                    calls.append(strike)
                if (row.get("P") or {}).get("inst_id"):
                    puts.append(strike)
            if calls and exp_date:
                chains["CALL"][exp_date] = sorted(calls)
            if puts and exp_date:
                chains["PUT"][exp_date] = sorted(puts)
        return {"Multiplier": str(data.get("lot_coin") or lot_of(base) or ""),
                "Chains": chains}

    # ── 报价 ──────────────────────────────────────────────────────────────
    def get_last_price(self, asset, quote=None, exchange=None):
        if _is_option(asset):
            try:
                q = ood.get_ticker_bid_ask(asset_to_inst(asset))
            except Exception as e:  # 取价失败不抛给策略循环（fail-soft + 日志）
                logger.warning("options last price failed: %s", e)
                return None
            bid, ask = q.get("bid"), q.get("ask")
            if bid and ask:
                return (bid + ask) / 2.0
            return bid or ask or None
        # 标的主资产（非期权）→ 现货参考价
        return ood.spot_price(family_of(str(getattr(asset, "symbol", ""))))

    def get_quote(self, asset, quote=None, exchange=None):
        if not _is_option(asset):
            px = self.get_last_price(asset)
            if px is None:
                return None
            from lumibot.entities import Quote
            return Quote(asset, px, px, px)
        q = ood.get_ticker_bid_ask(asset_to_inst(asset))
        from lumibot.entities import Quote
        return Quote(asset, q.get("bid"), q.get("ask"), self.get_last_price(asset))

    # ── 历史（mark K 线） ────────────────────────────────────────────────
    def get_historical_prices(self, asset, length, timestep: str = "",
                              timeshift=None, exchange=None,
                              include_after_hours: bool = True, quote=None,
                              return_polars: bool = False, **kwargs):
        """返回期权 mark 价的 lumibot Bars。

        单值序列 → open=high=low=close=mark、volume=0（无需成交量的策略端
        只关心权利金水平）；列名小写以对齐 lumibot Bars 契约。
        """
        import pandas as pd

        bar = _BAR_MAP.get(str(timestep or "").lower().removeprefix("bar:"), _DEFAULT_BAR)
        data = ood.fetch_lifecycle(asset_to_inst(asset), bar=bar)
        rows = data.get("rows") or []
        if not rows:
            return None
        rows = rows[-max(1, int(length)):]
        df = pd.DataFrame({
            "datetime": [datetime.fromtimestamp(r["ts"] / 1000.0, tz=timezone.utc).replace(tzinfo=None)
                         for r in rows],
            "open": [float(r["mark_px"]) for r in rows],
            "high": [float(r["mark_px"]) for r in rows],
            "low": [float(r["mark_px"]) for r in rows],
            "close": [float(r["mark_px"]) for r in rows],
            "volume": [0.0] * len(rows),
        }).set_index("datetime")
        return Bars(df, self.SOURCE, asset)

    def get_timestamp(self):
        return datetime.now(timezone.utc).replace(tzinfo=None)
