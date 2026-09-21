"""回归：strategies 包顶层不得被动导入 lumibot（MCP stdio 污染，2026-09-21）。

背景：``tools_f1`` 顶层 ``from nanobot_quant.strategies.td_sequential import ...`` ——
Python 会先执行包 ``__init__``；若其中顶层导入 ``TdSequentialStrategy``，lumibot 会在
MCP 子进程启动时被拉入，其 "LumiBot vX starting" 横幅打到 stdout，污染 stdio
JSON-RPC（客户端刷 "Failed to parse JSONRPC message"）。修复：包内改 PEP 562 懒加载。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

_PROBE = (
    "import sys\n"
    "import nanobot_quant.strategies as s\n"
    "print('lazy_module', 'nanobot_quant.strategies.td_sequential_strategy' in sys.modules)\n"
    "print('pure_engine', 'nanobot_quant.strategies.td_sequential' in sys.modules)\n"
    "print('lumibot', 'lumibot' in sys.modules)\n"
    "print('pure_engine_callable', callable(s.calculate))\n"
)


def _probe() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")])
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert out.returncode == 0, out.stderr
    return dict(line.split(" ", 1) for line in out.stdout.strip().splitlines())


def test_package_import_does_not_pull_lumibot():
    got = _probe()
    assert got["lazy_module"] == "False", "包顶层仍拉入了 td_sequential_strategy（lumibot 依赖）"
    assert got["pure_engine"] == "True", "纯 Python 引擎应随包导入即可用"
    assert got["pure_engine_callable"] == "True"
    assert got["lumibot"] == "False", "纯引擎路径不应把 lumibot 拉进 MCP 子进程"


def test_package_level_reexport_still_works():
    """惰加载不得破坏 ``from nanobot_quant.strategies import TdSequentialStrategy``。"""
    from nanobot_quant.strategies import TdSequentialStrategy

    assert isinstance(TdSequentialStrategy, type)


def test_unknown_attribute_raises_attribute_error():
    import nanobot_quant.strategies as strategies

    with pytest.raises(AttributeError):
        strategies.NoSuchStrategy  # noqa: B018
