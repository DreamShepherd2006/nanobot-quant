"""Step 1：回测进度写入（BacktestDriver.progress_path）。

运行期间 driver 将 {status: running, progress: {...}} 写入进度文件
（与结果文件同路径 <run_id>.json），完成后由 tools_backtest worker
覆写为 done/error 完整结果。本文件只测 driver 侧的 running 写入：
单调推进、字段齐全、写失败静默（不阻塞回测）。
"""

import json
from pathlib import Path

import pytest

from nanobot_quant.backtest.driver import BacktestDriver

from test_backtest_driver import _FakeFetcher, _flat_then_falling, _params


def _driver(progress_path, closes=None, ledger_dir=None, **kw):
    return BacktestDriver(
        scene="mid",
        params=_params(),
        fetcher=_FakeFetcher(closes or _flat_then_falling(60, 40)),
        ledger_dir=ledger_dir,
        progress_path=progress_path,
        **kw,
    )


def _falling(n: int = 100, base: float = 100.0, step: float = 0.5) -> list[float]:
    """连续递减（setup_buy 数到 9+ 触发买入，产生成交）——进度 fills>0。

    注意：纯递减序列 setup 计数不启动（TD 需前一 bar 反向翻转确认），
    必须平盘/上涨后再递减（见 _flat_then_falling）。
    """
    return [base - i * step for i in range(1, n + 1)]


def test_run_writes_monotonic_progress(tmp_path):
    """run() 期间进度文件单调推进：最终 bars_done==total、pct==100。"""
    p = tmp_path / "run.json"
    driver = _driver(p)
    out = driver.run()

    assert p.is_file()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "running"  # done 由 worker 层覆写，driver 层只写 running
    prog = data["progress"]
    assert prog["stage"] == "replay"
    assert prog["bars_done"] == out["bars"]
    assert prog["bars_total"] == out["bars"]
    assert prog["pct"] == 100.0
    assert prog["fills"] == out["fills"]
    assert prog["ts"] is not None


def test_progress_tracks_fills(tmp_path):
    """下跌行情产生成交 → 进度 fills 与结果一致且 >0。"""
    p = tmp_path / "run.json"
    driver = _driver(p)
    out = driver.run()
    assert out["fills"] > 0  # 序列设计：下跌 100 根必触发 setup_buy>=9

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["progress"]["fills"] == out["fills"]


def test_write_progress_prefetch_stage(tmp_path):
    """prefetch 阶段：bars_done=0 / bars_total=None / pct=None。"""
    p = tmp_path / "run.json"
    driver = _driver(p)
    driver._write_progress("prefetch", 0, None, None, 0)

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "running"
    prog = data["progress"]
    assert prog["stage"] == "prefetch"
    assert prog["bars_done"] == 0
    assert prog["bars_total"] is None
    assert prog["pct"] is None


def test_no_progress_path_skips_writes(tmp_path):
    """不传 progress_path → 不写任何进度文件，run() 正常。"""
    driver = _driver(None)
    out = driver.run()
    assert out["bars"] > 0


def test_progress_write_error_silent(tmp_path):
    """进度写失败（OSError，如目标目录不存在）静默——回测不中断。"""
    driver = _driver(tmp_path / "no_such_dir" / "run.json", ledger_dir=tmp_path)
    out = driver.run()
    assert out["bars"] > 0  # 进度写失败不影响回测结果
