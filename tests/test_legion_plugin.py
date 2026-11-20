"""Legion Module 契约实现测试（nanobot_quant.legion_plugin）。

锁定三件事：
1. 契约字段（name / display / sdk / route_prefix）与契约文本一致
2. ROUTES 覆盖全部业务页面，且每个「模块 + 注册函数」真实存在（防漏挂 / 防改名）
3. 单个页面失败不影响其他页面；sections() 结构合法、状态按配置计算
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from nanobot_quant import legion_plugin as lp

PKG_DIR = Path(lp.__file__).resolve().parent


class _Ctx:
    def __init__(self, data_root: str, gatekeeper=None):
        self.data_root = data_root
        self.gatekeeper = gatekeeper
        self.logs: list[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(msg)


# ── MCP 工具声明（契约可选钩子）─────────────────────────────────


def test_module_exposes_optional_mcp_specs_hook():
    assert callable(getattr(lp.QuantModule, "mcp_specs", None))


def test_mcp_specs_returns_registered_servers():
    specs = lp.MODULE.mcp_specs(_Ctx("/tmp/none"))
    assert isinstance(specs, dict)
    assert set(specs) == {"vibe-trading", "squad-delegate", "signal-structurizer"}
    for name, spec in specs.items():
        assert spec.name == name
        assert isinstance(spec.command, str) and spec.command
        assert isinstance(spec.args, list)
        assert hasattr(spec, "target_agents")


def test_mcp_specs_matches_discover():
    from nanobot_quant.mcp_spec import discover

    assert lp.MODULE.mcp_specs(_Ctx("/tmp/none")) == discover()


def test_mcp_specs_does_not_pull_heavy_deps():
    """契约要求 plugin 顶层保持轻量：调 mcp_specs 不得连带引入重依赖。"""
    import os
    import subprocess
    import sys as _sys

    code = (
        "import sys\n"
        "from nanobot_quant import legion_plugin as lp\n"
        "lp.MODULE.mcp_specs(None)\n"
        "bad = [m for m in ('lumibot', 'pandas', 'okx', 'gate_api') if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(PKG_DIR.parent)}
    out = subprocess.run(
        [_sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


# ── 契约字段 ──────────────────────────────────────────────────────


def test_module_contract_fields():
    m = lp.MODULE
    assert m.name == "quant"
    assert m.display.startswith("📊")
    assert isinstance(m.sdk, int) and m.sdk == lp.SDK_VERSION == 1
    # 历史模块保留 /config 前缀（新模块才用 /apps/<name>）
    assert m.route_prefix == "/config"
    assert callable(m.register_routes)
    assert callable(m.sections)


# ── 页面注册表 ────────────────────────────────────────────────────


def test_routes_cover_all_business_pages():
    assert len(lp.ROUTES) == 15
    names = [r[0] for r in lp.ROUTES]
    assert len(set(names)) == len(names), "模块名不应重复"
    # 关键页面必须在册（回归保护：新增页面漏进表即失败）
    for expected in (
        "exec_params_handlers",
        "td_table_handlers",
        "backtest_handlers",
        "okx_options_handlers",
        "f1_backtest_handlers",
        "ashare_handlers",
    ):
        assert expected in names


@pytest.mark.parametrize("module_name,func_name,label", lp.ROUTES)
def test_route_target_exists(module_name, func_name, label):
    """每个注册目标的源文件与函数必须真实存在（不 import，避免重型依赖）。"""
    src = PKG_DIR / f"{module_name}.py"
    assert src.exists(), f"缺少模块文件 {src}"
    text = src.read_text(encoding="utf-8")
    assert f"def {func_name}(" in text, f"{module_name}.py 缺少 {func_name}"


def test_register_routes_registers_all(monkeypatch, tmp_path):
    calls: list[tuple[str, object, object]] = []

    def _install(module_name: str, func_name: str):
        mod = types.ModuleType(f"nanobot_quant.{module_name}")

        def _register(app, gatekeeper):
            calls.append((module_name, app, gatekeeper))

        setattr(mod, func_name, _register)
        monkeypatch.setitem(sys.modules, f"nanobot_quant.{module_name}", mod)

    for module_name, func_name, _ in lp.ROUTES:
        _install(module_name, func_name)

    gk = object()
    app = object()
    ctx = _Ctx(str(tmp_path), gatekeeper=gk)
    lp.MODULE.register_routes(app, ctx)

    assert len(calls) == len(lp.ROUTES)
    # handler 仍按 (app, gatekeeper) 调用（过渡期兼容）
    assert all(c[1] is app and c[2] is gk for c in calls)
    assert any(f"{len(lp.ROUTES)}/{len(lp.ROUTES)}" in m for m in ctx.logs)


def test_register_routes_isolates_failures(monkeypatch, tmp_path):
    ok_module, ok_func, _ = lp.ROUTES[0]
    bad_module, bad_func, _ = lp.ROUTES[1]

    good = types.ModuleType(f"nanobot_quant.{ok_module}")

    def _ok(app, gatekeeper):
        return None

    setattr(good, ok_func, _ok)
    monkeypatch.setitem(sys.modules, f"nanobot_quant.{ok_module}", good)

    broken = types.ModuleType(f"nanobot_quant.{bad_module}")

    def _boom(app, gatekeeper):
        raise RuntimeError("mount failed")

    setattr(broken, bad_func, _boom)
    monkeypatch.setitem(sys.modules, f"nanobot_quant.{bad_module}", broken)

    # 其余模块导入失败（模拟缺失）
    for module_name, _, _ in lp.ROUTES[2:]:
        monkeypatch.setitem(sys.modules, f"nanobot_quant.{module_name}", None)

    ctx = _Ctx(str(tmp_path))
    lp.MODULE.register_routes(object(), ctx)

    assert any("路由挂载失败" in m for m in ctx.logs)
    assert any("导入失败" in m for m in ctx.logs)
    assert any(f"1/{len(lp.ROUTES)}" in m for m in ctx.logs)


# ── sections() ───────────────────────────────────────────────────


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_sections_structure(tmp_path):
    sections = lp.MODULE.sections(_Ctx(str(tmp_path)))
    headings = [s.heading for s in sections]
    assert headings == [
        "⚙️ 当前状态",
        "⚙️ 交易控制",
        "📐 策略与信号",
        "📊 回测验证",
        "🔑 账户与资产",
        "🟤 OKX 期权",
        "🇨🇳 A 股期权",
    ]
    links = [r.link for s in sections for r in s.rows if r.link]
    assert links, "至少要有可点击入口"
    assert all(l.startswith("/config/") or l.startswith("http") for l in links)


def test_sections_status_follows_config(tmp_path):
    _write(tmp_path / "legion" / "mode.json", {"mode": "research"})
    _write(tmp_path / "credentials" / "live.json", {"live": True})
    _write(tmp_path / "credentials" / "okx_options_params.json", {"live": {"enabled": True}})

    status = {r.label: r.status for r in lp.MODULE.sections(_Ctx(str(tmp_path)))[0].rows}
    assert "Research" in status["交易模式"]
    assert "🟢" in status["实盘开关"]
    assert "期权巡检" in status


def test_sections_without_options_config_hides_option_row(tmp_path):
    status = {r.label: r.status for r in lp.MODULE.sections(_Ctx(str(tmp_path)))[0].rows}
    assert "交易模式" in status and "实盘开关" in status
    assert "期权巡检" not in status, "期权模块无配置时不应显示该状态行"


def test_sections_defaults_when_config_missing(tmp_path):
    status = {r.label: r.status for r in lp.MODULE.sections(_Ctx(str(tmp_path)))[0].rows}
    assert "Quant" in status["交易模式"]
    assert "⚪" in status["实盘开关"]


def test_credential_rows_from_registry(monkeypatch, tmp_path):
    class _Spec:
        icon = "🔑"
        display = "OKX OnchainOS"
        description = "OKX DEX API 凭证"

    monkeypatch.setattr(
        "nanobot_quant.credential_registry.discover", lambda: {"okx": _Spec()}, raising=False
    )
    rows = lp.QuantModule._credential_rows()
    assert len(rows) == 1
    assert rows[0].link == "/config/credentials/okx"
    assert "OKX" in rows[0].label


def test_credential_rows_survive_discovery_error(monkeypatch):
    def _boom():
        raise RuntimeError("registry broken")

    monkeypatch.setattr("nanobot_quant.credential_registry.discover", _boom, raising=False)
    assert lp.QuantModule._credential_rows() == []


def test_log_falls_back_to_stderr(capsys):
    class _NoLog:
        data_root = "/tmp"

    lp._log(_NoLog(), "hello-stderr")
    assert "hello-stderr" in capsys.readouterr().err
