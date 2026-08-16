"""Integration tests for the signal-structurizer MCP server (stdio).

These spawn the real server as a subprocess and drive it through the
official `mcp` Python SDK client — validating the full JSON-RPC stdio
round trip (initialize -> tools/list -> tools/call) end to end.
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_PARAMS = StdioServerParameters(
    command=sys.executable,
    args=["-m", "nanobot_quant.signal_mcp_server"],
    env={
        **os.environ,
        "PYTHONPATH": os.path.abspath("src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    },
)

EXPECTED_TOOLS = {
    "run_td_sequential",
    "structurize_signal",
    "execute_signal",
    "run_research_chain",
    "get_chain_result",
    "get_execution_outcome",
    "run_backtest",
    "get_backtest_result",
    "wallet_login_init",
    "wallet_login_poll",
    "wallet_payment_set",
    "wallet_setup",
    "wallet_login_status",
    "wallet_status",
    "wallet_addresses",
    "wallet_balance",
    "wallet_chains",
    "wallet_history",
    "wallet_add",
    "wallet_switch",
    "cex_sub_order",
}


def _run(coro):
    return asyncio.run(coro)


def test_initialize_and_list_20_tools():
    async def scenario():
        async with stdio_client(SERVER_PARAMS) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "signal-structurizer"
                assert init.protocolVersion

                result = await session.list_tools()
                names = {t.name for t in result.tools}
                assert names == EXPECTED_TOOLS

                desc_by_name = {t.name: t.description for t in result.tools}
                # every tool must keep a non-trivial description
                for name in EXPECTED_TOOLS:
                    assert desc_by_name[name] and len(desc_by_name[name]) > 10, name

    _run(scenario())


def test_call_get_chain_result_unknown_run():
    """Side-effect-free tool call: get_chain_result must return parseable JSON."""

    async def scenario():
        async with stdio_client(SERVER_PARAMS) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(
                    "get_chain_result", {"run_id": "definitely-not-a-real-run"}
                )
                assert res.content and res.content[0].type == "text"
                payload = json.loads(res.content[0].text)
                assert isinstance(payload, dict)
                assert "status" in payload or "error" in payload

    _run(scenario())


def test_call_execute_signal_rejects_bad_json():
    """Argument validation must surface as a tool error, not a crash."""

    async def scenario():
        async with stdio_client(SERVER_PARAMS) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(
                    "execute_signal", {"ticker_signal_json": "not json"}
                )
                assert res.isError
                text = res.content[0].text if res.content else ""
                assert "traceback" not in text.lower()
                assert (
                    "invalid" in text.lower() or "error" in text.lower()
                ), text[:200]

    _run(scenario())
