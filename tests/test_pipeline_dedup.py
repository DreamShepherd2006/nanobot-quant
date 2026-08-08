"""pipeline 同标的去重（P2 B3）— _position_exists 纯函数测试。

集成行为（BUY 被 position_exists 拦截）由 HF Space 实测验证（B4）；
此处覆盖去重判定规则：排除 gas/quote 币、异常安全（fail-open 不拦单）。
"""

from __future__ import annotations

from nanobot_quant import pipeline


class _Pos:
    def __init__(self, symbol: str):
        self.asset = type("A", (), {"symbol": symbol})()


class _Broker:
    def __init__(self, positions):
        self.positions = positions
        self.calls = 0

    def _pull_positions(self, strategy):
        self.calls += 1
        return self.positions


def test_gas_and_quote_never_block():
    b = _Broker([_Pos("SOL"), _Pos("USDC")])
    for sym in ("SOL", "USDC", "USDT", "sol", "usdc"):
        assert pipeline._position_exists(b, sym) is False


def test_existing_position_detected_case_insensitive():
    b = _Broker([_Pos("CRCLX")])
    assert pipeline._position_exists(b, "CRCLX") is True
    assert pipeline._position_exists(b, "crclx") is True


def test_no_position_returns_false():
    b = _Broker([_Pos("SOL"), _Pos("USDC")])
    assert pipeline._position_exists(b, "RENDER") is False


def test_pull_failure_is_fail_open():
    class _BadBroker:
        def _pull_positions(self, strategy):
            raise RuntimeError("chain rpc down")

    # 去重检查失败不得拦单（fail-open），由风控/门控兜底
    assert pipeline._position_exists(_BadBroker(), "CRCLX") is False
