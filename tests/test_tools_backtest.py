"""run_backtest MCP 工具：异步化 + MCP stdio 污染防护测试。

回归点（2026-08-15 实盘暴露）：
- 真实回测（CLI 拉 K 线 + Lumibot 启动 + 策略循环）超出 MCP 30s 硬超时
  （实测：5d ≈ 9s OK，180d > 30s 超时）→ run_backtest 改 run_id + 后台线程
- lumibot 进度条写 stdout 且不换行（\\r），与 JSON-RPC 响应混行导致响应
  丢失 → 30s 超时（回测实际 9s 完成）
- "LumiBot vX starting" 在 lumibot import 时经 stdout handler 打印
- backtest_runner 的 print 进度行若直落 stdout 会污染 stdio 通道
"""

import logging
import os
import time

import nanobot_quant.backtest_runner as br
from nanobot_quant.tools.tools_backtest import get_backtest_result, run_backtest


def _poll_done(run_id, tries=50, delay=0.05):
    for _ in range(tries):
        out = get_backtest_result(run_id)
        if out.get("status") in ("done", "error"):
            return out
        time.sleep(delay)
    return out


def test_run_backtest_returns_run_id(monkeypatch):
    """异步契约：立即返回 started + run_id，不阻塞等待回测。"""
    monkeypatch.setattr(br, "run", lambda **kw: {})

    result = run_backtest("SOL/USDC", "2026-07-01", "2026-07-05")

    assert result["status"] == "started"
    assert result["run_id"]


def test_get_backtest_result_polls_to_done(monkeypatch, tmp_path, capsys):
    """后台线程完成回测后，get_backtest_result 能读到结果（含 fake run 的 print）。"""
    def fake_run(**kw):
        print("============================================================")
        print(f"  {kw.get('symbol')} | {kw.get('start')} → {kw.get('end')}  (source: {kw.get('source')})")
        return {"total_return_pct": 1.23, "total_trades": 2}

    monkeypatch.setattr(br, "run", fake_run)
    monkeypatch.setattr(
        "nanobot_quant.onchainos_cli.backtests_dir",
        lambda roots=("/data", "/mnt/workspace"): tmp_path,
    )

    run_id = run_backtest("SOL/USDC", "2026-07-01", "2026-07-05", source="onchainos")["run_id"]
    out = _poll_done(run_id)

    assert out["status"] == "done"
    assert out["result"]["total_return_pct"] == 1.23
    # 后台线程的进度 print 不得污染 stdout（MCP JSON-RPC 通道）
    captured = capsys.readouterr()
    assert "====" not in captured.out
    assert "SOL/USDC" not in captured.out


def test_get_backtest_result_missing_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "nanobot_quant.onchainos_cli.backtests_dir",
        lambda roots=("/data", "/mnt/workspace"): tmp_path,
    )
    out = get_backtest_result("nope-123456")
    assert "error" in out
    assert "no backtest result" in out["error"]


def test_run_backtest_sets_lumibot_env(monkeypatch):
    """后台线程必须确保 lumibot 相关 env 开关已设置（进度条/日志静音/遥测）。"""
    for key in ("LUMIBOT_TELEMETRY", "BACKTESTING_SHOW_PROGRESS_BAR", "BACKTESTING_QUIET_LOGS"):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(br, "run", lambda **kw: {})
    run_id = run_backtest("SOL/USDC", "2026-07-01", "2026-07-05")["run_id"]
    _poll_done(run_id)

    assert os.environ.get("BACKTESTING_SHOW_PROGRESS_BAR") == "0"
    assert os.environ.get("BACKTESTING_QUIET_LOGS") == "true"
    assert os.environ.get("LUMIBOT_TELEMETRY") == "0"


def test_lumibot_banner_goes_to_stderr(capsys):
    """预置 stderr console handler：模拟 lumibot import banner 输出不得落 stdout。"""
    _lb = logging.getLogger("lumibot")
    _lb.handlers.clear()

    from nanobot_quant.tools.tools_execute import _redirect_lumibot_console_to_stderr
    _redirect_lumibot_console_to_stderr()

    _lb.info("LumiBot v4.5.78 starting")
    # 模拟 lumibot/__init__._log_startup_version() 的 setLevel（默认 INFO）
    # —— level 提高后 info 才真正 emit，验证输出目标（stderr 而非 stdout）。
    _lb.setLevel(logging.INFO)
    _lb.info("LumiBot v4.5.78 starting")

    captured = capsys.readouterr()
    assert "LumiBot v4.5.78" not in captured.out
    assert "LumiBot v4.5.78" in captured.err
