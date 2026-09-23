"""MCP spec 的 tool_timeout 契约。

背景（2026-09-23 实测）：nanobot MCP client 侧 ``MCPServerConfig.tool_timeout``
默认 30s，超时直接掐断工具调用、工具侧收不到任何通知——quant 在 Space 内调用
``probe_ashare_sources``（串行版 ~30s）实测返回
``(MCP tool call timed out after 30s)``。默认值由上游定、但每 server 可配，
故由 MCPSpec 显式声明、squad_config_sync 写入 agent config.json。
"""

from __future__ import annotations

from nanobot_quant.mcp_spec import MCP_TOOL_TIMEOUT_S, MCPSpec, discover


def test_registered_specs_declare_tool_timeout():
    """所有注册的 server 都要显式声明超时（不吃上游 30s 默认）。"""
    specs = discover()
    assert specs, "registry 为空 —— discover() 失效"
    missing = [name for name, spec in specs.items() if spec.tool_timeout is None]
    assert not missing, f"以下 MCP server 未声明 tool_timeout（会吃上游默认 30s）：{missing}"


def test_tool_timeout_is_above_upstream_default():
    """必须高于上游默认 30s，否则失去意义；也要有上界，避免无限等待。"""
    for name, spec in discover().items():
        assert 30 < spec.tool_timeout <= 300, f"{name}: tool_timeout={spec.tool_timeout}"


def test_long_task_servers_explicitly_configured():
    """已知长任务 server 必须带上超时（回归：这三个是本次修复对象）。"""
    specs = discover()
    for name in ("signal-structurizer", "squad-delegate", "vibe-trading"):
        assert specs[name].tool_timeout == MCP_TOOL_TIMEOUT_S


def test_field_defaults_to_none_for_backward_compat():
    """不传时保持 None → sync 不写该字段 → 行为同上游默认（旧 spec 零影响）。"""
    spec = MCPSpec(name="x", display="X", command="python3", args=[])
    assert spec.tool_timeout is None
