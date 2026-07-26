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

    env_from_credential points to a credential name registered via
    credential_registry; launch.sh reads that credential and exports
    the necessary environment variables before starting agents.
    """

    name: str
    display: str
    command: str
    args: list[str]
    env_from_credential: str | None = None
    env: dict[str, str] | None = None
    env_provider_keys: dict[str, str] | None = None


def register(spec: MCPSpec) -> None:
    _registry[spec.name] = spec


def discover() -> dict[str, MCPSpec]:
    return dict(_registry)


# ── OnchainOS (OKX) MCP server ─────────────────────────────────

okx_mcp = MCPSpec(
    name="onchainos",
    display="OnchainOS (OKX)",
    command="/usr/local/bin/onchainos",
    args=["mcp"],
    env_from_credential="okx",
)
register(okx_mcp)


# ── Vibe-Trading MCP server ────────────────────────────────────

vt_mcp = MCPSpec(
    name="vibe-trading",
    display="Vibe-Trading",
    command="vibe-trading-mcp",
    args=[],
    env={
        "LANGCHAIN_PROVIDER": "deepseek",
        "LANGCHAIN_MODEL_NAME": "deepseek-v4-pro",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
    },
    env_provider_keys={
        "DEEPSEEK_API_KEY": "deepseek",
    },
)
register(vt_mcp)


# ── Signal Structurizer MCP server ──────────────────────────────

signal_mcp = MCPSpec(
    name="signal-structurizer",
    display="Signal Structurizer",
    command="python3",
    args=["-m", "nanobot_quant.tools.signal_structurizer"],
    env_provider_keys={
        "DEEPSEEK_API_KEY": "deepseek",
    },
)
register(signal_mcp)
