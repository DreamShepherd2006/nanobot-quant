"""normalize_source: 回测源注册表校验（ValueError 语义）。"""

import pytest

from nanobot_quant.backtest_runner import normalize_source


class TestNormalizeSource:
    def test_aliases_yahoo_to_yfinance(self):
        assert normalize_source("yahoo") == "yfinance"

    def test_registry_names_pass(self):
        assert normalize_source("yfinance") == "yfinance"
        assert normalize_source("onchainos") == "onchainos"
        assert normalize_source("okx_cex") == "okx_cex"

    def test_unknown_source_raises_valueerror(self):
        with pytest.raises(ValueError, match="未知数据源"):
            normalize_source("coinbase")

    def test_unimplemented_backtest_source_raises_valueerror(self):
        """gate_cex/eastmoney 是展示/实盘源，回测未实现必须明确报错
        （ValueError，MCP 工具路径可捕获；绝不静默回退到 Yahoo）。"""
        with pytest.raises(ValueError, match="回测适配未实现"):
            normalize_source("gate_cex")
        with pytest.raises(ValueError, match="回测适配未实现"):
            normalize_source("eastmoney")
