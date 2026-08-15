"""run_backtest MCP 工具：MCP stdio 污染防护测试。

回归点（2026-08-15 实盘暴露）：
- lumibot 进度条写 stdout 且不换行（\\r），与 JSON-RPC 响应混行导致响应
  丢失 → MCP 30s 超时（回测实际 9s 完成）
- backtest_runner 的 print 进度行若直落 stdout 会污染 stdio 通道
- env 开关必须在 lumibot import 之前设置（常量在 import 时读取）
"""

import os

import nanobot_quant.backtest_runner as br
from nanobot_quant.tools.tools_backtest import run_backtest


def test_run_backtest_keeps_stdout_clean(monkeypatch, capsys):
    """执行期 backtest_runner 的 print 必须去 stderr，stdout（MCP 通道）干净。"""
    def fake_run(**kw):
        print("============================================================")
        print(f"  {kw.get('symbol')} | {kw.get('start')} → {kw.get('end')}  (source: {kw.get('source')})")
        return {"total_return_pct": 1.23, "total_trades": 0}

    monkeypatch.setattr(br, "run", fake_run)

    result = run_backtest("SOL/USDC", "2026-07-01", "2026-07-05", source="onchainos")

    assert result["total_return_pct"] == 1.23
    captured = capsys.readouterr()
    assert "====" not in captured.out          # stdout（MCP JSON-RPC 通道）零污染
    assert "SOL/USDC" not in captured.out
    assert "====" in captured.err               # 进度输出去了 stderr


def test_run_backtest_sets_lumibot_env_before_import(monkeypatch, capsys):
    """工具调用必须确保 lumibot 相关 env 开关已设置（进度条/日志静音/遥测）。"""
    for key in ("LUMIBOT_TELEMETRY", "BACKTESTING_SHOW_PROGRESS_BAR", "BACKTESTING_QUIET_LOGS"):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(br, "run", lambda **kw: {})

    run_backtest("SOL/USDC", "2026-07-01", "2026-07-05")

    assert os.environ.get("BACKTESTING_SHOW_PROGRESS_BAR") == "0"
    assert os.environ.get("BACKTESTING_QUIET_LOGS") == "true"
    assert os.environ.get("LUMIBOT_TELEMETRY") == "0"


def test_run_backtest_silences_lumibot_stdout_handlers(monkeypatch, capsys):
    """清 lumibot logger 树上的 stdout handler（懒 import 后注册的也会被清）。"""
    import logging

    # 模拟 lumibot 子 logger 注册 stdout handler
    fake_logger = logging.getLogger("lumibot.strategies._strategy")
    fake_logger.handlers.clear()
    handler = logging.StreamHandler()  # 默认 sys.stderr？——显式绑 stdout
    import sys as _sys
    handler = logging.StreamHandler(_sys.stdout)
    fake_logger.addHandler(handler)
    fake_logger.propagate = False

    def fake_run(**kw):
        fake_logger.info("Getting historical prices for SOL, 120 bars, day")
        fake_logger.info("LumiBot v4.5.78 starting")
        return {}

    monkeypatch.setattr(br, "run", fake_run)

    result = run_backtest("SOL/USDC", "2026-07-01", "2026-07-05")

    assert result == {}
    captured = capsys.readouterr()
    assert "Getting historical prices" not in captured.out
    assert "LumiBot v4.5.78" not in captured.out
