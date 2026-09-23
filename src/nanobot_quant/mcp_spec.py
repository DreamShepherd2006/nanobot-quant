"""MCP server spec registry for nanobot-quant.

Register MCP servers that nanobot-quant wants to inject into agent
configs.  nanobot-legion's squad_config_sync discovers these specs
at startup and merges them into each agent's config.json.
"""

from __future__ import annotations

from dataclasses import dataclass

_registry: dict[str, MCPSpec] = {}


@dataclass
class MCPSpec:
    """Declares an MCP server to inject into agent configs.

    command + args form the `type: stdio` MCP server config.

    env: static env vars passed to the MCP server process.
    env_provider_keys: {ENV_VAR: provider_name} — resolved at sync
        time by reading the target agent's provider config and
        extracting apiKey, then injected into the MCP server env.
    env_provider_model_keys: {ENV_VAR: provider_name} — resolved
        at sync time from agents.defaults.model.

    env_from_credential points to a credential name registered via
    credential_registry; launch.sh reads that credential and exports
    the necessary environment variables before starting agents.

    tool_timeout: 单个 MCP 工具调用的超时秒数（nanobot 侧
    ``MCPServerConfig.tool_timeout``，上游默认 30）。None = 不写该
    字段、吃上游默认；长任务工具（数据体检/分析/委派等）应显式调高，
    否则会被 MCP client 在 30s 处直接掐断（工具侧收不到任何通知）。
    """

    name: str
    display: str
    command: str
    args: list[str]
    target_agents: list[str] | None = None
    env_from_credential: str | None = None
    env: dict[str, str] | None = None
    env_provider_keys: dict[str, str] | None = None
    env_provider_model_keys: dict[str, str] | None = None
    tool_timeout: int | None = None


def register(spec: MCPSpec) -> None:
    _registry[spec.name] = spec


def discover() -> dict[str, MCPSpec]:
    return dict(_registry)


# ── MCP 工具调用超时（nanobot client 侧 MCPServerConfig.tool_timeout）──
# 上游默认 30s（nanobot/config/schema.py），官方自己的 MCP preset 也普遍调高到
# 45/60。我们的 server 全是研究/量化类长任务，30s 太紧：2026-09-23 实测
# probe_ashare_sources（串行版 ~30s）被掐断、工具侧收不到任何通知。
# 该值只抬 client 等待上限：不限制工具自身耗时，最坏只是多等 30s。
MCP_TOOL_TIMEOUT_S = 60


# ── Vibe-Trading MCP server ────────────────────────────────────

vt_mcp = MCPSpec(
    name="vibe-trading",
    display="Vibe-Trading",
    command="vibe-trading-mcp",
    args=[],
    target_agents=["vt_research"],
    env={
        "LANGCHAIN_PROVIDER": "deepseek",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
    },
    env_provider_keys={
        "DEEPSEEK_API_KEY": "deepseek",
    },
    env_provider_model_keys={
        "LANGCHAIN_MODEL_NAME": "deepseek",
    },
    tool_timeout=MCP_TOOL_TIMEOUT_S,
)
register(vt_mcp)


# ── Squad Delegate MCP server ────────────────────────────────────

squad_del_mcp = MCPSpec(
    name="squad-delegate",
    display="Squad Delegate",
    command="python3",
    args=["-m", "nanobot_legion.tools.squad_delegate"],
    target_agents=["neo"],
    # delegate_to_agent 同步等目标 agent 回复（relay 实测可 30–90s）
    tool_timeout=MCP_TOOL_TIMEOUT_S,
)
register(squad_del_mcp)



# ── Signal Structurizer MCP server ───────────────────────────
# Converts VT Swarm debate → TickerSignal JSON for the Aggregator.

signal_mcp = MCPSpec(
    name="signal-structurizer",
    display="Signal Structurizer",
    command="python3",
    args=["-m", "nanobot_quant.signal_mcp_server"],
    target_agents=["vt_research", "quant"],
    env={
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "LANGCHAIN_PROVIDER": "deepseek",
    },
    env_provider_keys={
        "DEEPSEEK_API_KEY": "deepseek",
    },
    env_provider_model_keys={
        "LANGCHAIN_MODEL_NAME": "deepseek",
    },
    # 本 server 汇集长任务工具（数据体检、F1/IV 分析、回测等）
    tool_timeout=MCP_TOOL_TIMEOUT_S,
)
register(signal_mcp)
