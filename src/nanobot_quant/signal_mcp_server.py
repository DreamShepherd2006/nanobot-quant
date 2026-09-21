"""MCP server: signal-structurizer — quant / vt_research execution tools.

Protocol: stdio JSON-RPC (MCP), built on the official `mcp` Python SDK
(mcp.server.fastmcp.FastMCP) — same framework as squad-delegate.

Tool implementations live in tools/:
  tools_wallet.py      wallet_setup, wallet_login_status, wallet_login_init, ...
  tools_analysis.py    run_td_sequential
  tools_backtest.py    run_backtest
  tools_f1.py          analyze_f1_td, analyze_f1_drawdown
  tools_structurize.py structurize_signal
  tools_execute.py     execute_signal
"""

from __future__ import annotations

import logging
import os
import sys

# ── Suppress library stdout during imports ──────────────────────
# Disable lumibot runtime telemetry at the MCP-server process level: the
# emitter spawns a background thread writing LUMIBOT_TELEMETRY lines through
# a stdout-bound logging handler, which corrupts the stdio JSON-RPC channel.
# The env toggle is the only reliable kill-switch (handler-level cleanup is
# order-dependent because lumibot imports lazily inside tool calls).
os.environ.setdefault("LUMIBOT_TELEMETRY", "0")

# Backtest progress bar writes "\rProgress |…" to stdout WITHOUT a trailing
# newline: it shares the MCP stdio buffer with the JSON-RPC response and the
# merged line fails client-side JSON parsing → the response is lost → the
# 30s MCP tool timeout fires even though the backtest finished in 9s.
# Constants are read at lumibot import time, so set these at process level.
# BACKTESTING_QUIET_LOGS silences INFO logs ("LumiBot v4.5.78 starting",
# "Getting historical prices …") that otherwise flood the channel with
# parse-error noise.
os.environ.setdefault("BACKTESTING_SHOW_PROGRESS_BAR", "0")
os.environ.setdefault("BACKTESTING_QUIET_LOGS", "true")

logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)
# Clear handlers on the ENTIRE lumibot logger tree (sub-loggers like
# lumibot.brokers.broker register their own stdout handlers, polluting
# the MCP stdio JSON-RPC channel).
for _lg_name in list(logging.Logger.manager.loggerDict):
    if _lg_name == "lumibot" or _lg_name.startswith("lumibot."):
        _lg = logging.getLogger(_lg_name)
        _lg.handlers.clear()
        _lg.propagate = True
        _lg.setLevel(logging.WARNING)

SERVER_NAME = "signal-structurizer"
SERVER_VERSION = "2.0.0"

from nanobot_quant.tools.tools_wallet import (
    wallet_login_init,
    wallet_login_poll,
    wallet_payment_set,
    wallet_setup,
    wallet_status,
    wallet_addresses,
    wallet_balance,
    wallet_chains,
    wallet_history,
    wallet_add,
    wallet_switch,
    wallet_login_status,
)
from nanobot_quant.tools.tools_analysis import run_td_sequential
from nanobot_quant.tools.tools_backtest import get_backtest_result, run_backtest
from nanobot_quant.tools.tools_cex import cex_sub_order
from nanobot_quant.tools.tools_options import options_broker_selftest
from nanobot_quant.tools.tools_structurize import structurize_signal
from nanobot_quant.tools.tools_execute import (
    _redirect_lumibot_console_to_stderr,
    execute_signal,
    get_execution_outcome,
)
from nanobot_quant.tools.tools_research_chain import get_chain_result, run_research_chain
from nanobot_quant.tools.tools_f1 import (
    analyze_f1_drawdown,
    analyze_f1_td,
    get_f1_result,
    run_f1_analysis,
)

# ``lumibot/__init__._log_startup_version()`` logs "LumiBot vX starting" at
# import time through a stdout-bound StreamHandler, BEFORE any handler
# cleanup can run. Reuse the pre-existing-console-handler path: register a
# stderr handler now so the banner (and the console handler it installs)
# stays off the MCP stdio channel for every tool in this process.
_redirect_lumibot_console_to_stderr()

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(SERVER_NAME, log_level="WARNING")


# ── Tool registry ───────────────────────────────────────────────
# Descriptions are preserved verbatim from the pre-SDK hand-written
# schema (tool prompt quality must not regress).  Input schemas are
# now derived automatically from the function signatures via FastMCP.

_TOOL_DESCRIPTIONS = {
    "run_td_sequential": (
        "Run TD Sequential analysis on a Solana token. "
        "Fetches daily K-line data from OnchainOS, "
        "computes DeMark TD Setup/Countdown/TDST/score, "
        "and returns a structured TickerSignal with "
        "recommendation (BUY/SELL/HOLD), setup count, "
        "countdown count, score, support/resistance levels."
    ),
    "structurize_signal": (
        "Convert VT Swarm investment committee debate transcript "
        "into a structured TickerSignal JSON. Call this after every "
        "swarm analysis to produce machine-readable signals for "
        "the Aggregator pipeline."
    ),
    "execute_signal": (
        "Execute the trading pipeline on structured signal(s). "
        "Passes signal through Risk → Position Sizing → Order "
        "generation. Accepts a JSON signal string (single object "
        "or list), returns risk checks and suggested orders. "
        "Pass live=true to attempt on-chain execution — this only "
        "works if the WebUI live trading toggle (/config/live) is "
        "enabled; otherwise the order is not executed (dry-run)."
    ),
    "run_backtest": (
        "Start a full backtest on a token symbol in the BACKGROUND. "
        "Resolves ticker → fetches historical K-lines → runs TD Sequential "
        "strategy → Lumibot backtest engine. Returns status=started + run_id "
        "immediately (a non-trivial range exceeds the 30s MCP tool timeout); "
        "poll get_backtest_result(run_id) for the performance metrics."
    ),
    "get_backtest_result": (
        "Return the persisted outcome of a background backtest started by "
        "run_backtest: reads <data_root>/legion/backtests/<run_id>.json. "
        "Returns status=done + result (total_return_pct, sharpe_ratio, "
        "total_trades, win_rate_pct, …), status=error, or a hint when the "
        "run is still in progress."
    ),
    "wallet_login_init": (
        "Initiate onchainos social (Google/Apple/email) wallet login. "
        "Returns a loginUrl that the user must open in a browser. "
        "After browser confirmation, call wallet_login_poll to complete. "
        "Required after every Factory Rebuild (session data lost). "
        "The keyring data is stored in ~/.onchainos/ (file-based on Linux)."
    ),
    "wallet_login_poll": (
        "Poll for social login completion. Blocks up to 310 seconds. "
        "Call this after the user confirms login in their browser. "
        "Returns session data on success."
    ),
    "wallet_payment_set": (
        "Set onchainos payment default tier. Must call AFTER wallet login "
        "is complete (wallet_login_poll succeeded). Required for Market API "
        "tools (market_kline etc.) to work without QUOTA errors."
    ),
    "wallet_setup": (
        "One-shot onchainos wallet bootstrap. Call this REPEATEDLY until phase=done. "
        "First call starts login (returns login_url). After user authorizes in browser, "
        "call again to complete poll + payment setup. When fully done, returns phase=done."
    ),
    "wallet_login_status": (
        "Check onchainos login and payment status without side effects. "
        "Returns: logged_in, payment_basic, payment_premium booleans."
    ),
    "wallet_status": (
        "Show current onchainos wallet status: email, loginType, "
        "currentAccountId, currentAccountName, accountCount, policy."
    ),
    "wallet_addresses": (
        "List wallet addresses for the current account, grouped by chain "
        "category (XLayer, EVM, Solana). Optional --chain filter: chain "
        "name or ID (e.g. 'solana' or '501', 'ethereum' or '1')."
    ),
    "wallet_balance": (
        "Query onchainos wallet balances. Use all_accounts=true to query all "
        "accounts' assets; chain filters by chain name/ID; token_address "
        "filters by token contract (requires chain); force bypasses caches "
        "and re-fetches from API."
    ),
    "wallet_chains": (
        "List all chains supported by onchainos wallet (cached locally)."
    ),
    "wallet_history": (
        "Query onchainos wallet transaction history. Optional filters: chain "
        "(name/ID), address, limit (page size), page_num (page cursor)."
    ),
    "wallet_add": (
        "Create a new sub-wallet account (up to 50 per wallet)."
    ),
    "wallet_switch": (
        "Switch the active wallet account to the given account_id."
    ),
    "run_research_chain": (
        "All-in-one research-to-execution: starts a VT investment_committee "
        "swarm debate, then automatically chains structurize_signal -> "
        "run_td_sequential (TD check) -> execute_signal once the debate "
        "completes. No further agent orchestration needed after this call. "
        "Returns the swarm run_id immediately; the chain runs in a "
        "background thread and its outcome is written to "
        "<data_root>/legion/research_chains/<run_id>.json (query via "
        "get_chain_result). Fails fast (status=error, no swarm started) "
        "if the symbol is not a native/resolvable token on the chain."
    ),
    "get_chain_result": (
        "Return the persisted outcome of a run_research_chain execution: "
        "reads <data_root>/legion/research_chains/<run_id>.json.  Lets "
        "agents/WebUI audit whether the debate was executed, blocked, or "
        "still pending — without touching the swarm run directory."
    ),
    "get_execution_outcome": (
        "[退役] Loop 模式已由 P2 B3 移除 — execute_signal 现在总是同步直调，"
        "结果直接包含在 execute_signal 的响应中。此工具仅返回 retired 说明。"
    ),
    "cex_sub_order": (
        "在指定子账号（gate_bot1..5）用其自身 API Key 下 Gate 市价单（官方 gate-api SDK）。"
        "用于验证子账号交易链路（P3 TD 分批下单前置）及手动子账号下单/对账。"
        "side=buy → amount 为 USDT 金额；side=sell → amount 为基础币数量。"
        "返回 status=filled（成交明细）/ pending（已提交未 closed）/ error（明确原因）。"
    ),
    "options_broker_selftest": (
        "期权执行层只读自检（不下单/不改台账/不动保证金）：一次确认期权链、"
        "Asset↔instId 与每张面值 multiplier（lumibot fork patch 是否生效）、"
        "期权子账号配置与余额、当前期权持仓。返回 status=ok/partial/error + checks。"
    ),
    "analyze_f1_td": (
        "把波动率序列 F1（= ATR_n / ATR_n[lookback]，lookback 按 3 小时语义随周期"
        "换算：1m→180、15m→12、1H→3）喂给 TD Sequential，检验 setup 达到阈值"
        "（默认 9）之后的**衰竭方向**——触发后 k 根终值比起点涨/跌了多少"
        "（信号数 n / 中位幅度 / 方向命中率 / 随机对照 p 值），以及同一数据上"
        "「价格 TD」对照组。注意：本工具量的是「会不会收回来」，**不量回撤深度**；"
        "要看「中间跌多深 / 尾部风险」用 analyze_f1_drawdown。标的可为 A股/ETF"
        "（588000、600519）、美股（AAPL）或加密（BTC-USDT）；数据源按标的自动判断"
        "（A股/美股→东财、*-USDT→OKX），也可显式 source= 指定。"
        "**显著性必须看主字段（first_cross）；`*_all` 是累加期重复计数，"
        "只作参考，不可用它判显著。**"
        "只读分析工具：不下单、不改任何配置。"
    ),
    "analyze_f1_drawdown": (
        "F1 上的 TD 触发后 **回撤有多深**（ATR 单位比值口径），用来回答"
        "「这个信号的尾部风险如何」。三项指标：整段（min(low) 传统口径）、"
        "单根（最坏的那一根 bar，即插针）、插针次数（单根跌<−2% 的根数）。"
        "每项对比同段随机位置，输出**比值**：<1 = 触发后更浅，>1 = 更深。"
        "支持 F1 分位过滤（qmin/qmax）、多 horizon（ks）、时间样本外切分（split）、"
        "价格 TD 对照组。\n"
        "**实证已定论（详见 docs/quant-system.md §33.33/§33.34）**："
        "加密 15m/1H 上 buy9 使回撤幅度系统变浅（0.828/0.712，6 标的 36/36），"
        "**加 Q3（0.6–0.8）分位过滤压到 0.612**；sell9 严格镜像（Q3 = 1.461）。"
        "但**幅度收窄 ≠ 尾部风险降低**：插针次数不降（1.0–1.2）。"
        "分布是**倒 U**（Q3 最安全 0.62，Q0/Q4 ≈ 1），**绝不可用「F1 越低越安全」**。"
        "A股 结论不同：日线样本不足、30m 无方向、5m 反向，仅 15m 宽基有微弱迹象。\n"
        "只读分析工具：不下单、不改任何配置。"
    ),
    "run_f1_analysis": (
        "**异步**启动一轮 F1 分析（WebUI「📊 F1 模式回测」分栏同款契约）："
        "kind='f1_td'（触发统计，同 analyze_f1_td）或 'f1_drawdown'（回撤诊断，"
        "同 analyze_f1_drawdown）；symbols / periods / source 必填，其余参数"
        "（k / ks / atr_n / threshold / limit / qmin / qmax / split）透传。"
        "立即返回 {status: started, run_id}，用 get_f1_result(run_id) 轮询。"
        "**为什么异步**：多标的 × 多周期会超 30s MCP 硬超时。只读，不下单。"
        "周期可用性由源决定：sina 无 1m、eastmoney 云端不可达（会报错）。"
    ),
    "get_f1_result": (
        "轮询 run_f1_analysis 的结果。返回 {status, run_id, result}；"
        "status=running 时 result 未出，done 时完整，error 时带原因。"
        "result 里带 **markdown** 字段（已渲染好的三段报告：参数快照 / 触发统计 /"
        "回撤诊断），可直接拷贝给用户看；回撤诊断的三列（段回撤比 / 单根比 /"
        "插针比）必须一起读——**幅度可预测 ≠ 风险可降**。"
    ),
}

_TOOL_DISPATCH = {
    "run_td_sequential": run_td_sequential,
    "structurize_signal": structurize_signal,
    "execute_signal": execute_signal,
    "run_research_chain": run_research_chain,
    "get_chain_result": get_chain_result,
    "get_execution_outcome": get_execution_outcome,
    "run_backtest": run_backtest,
    "get_backtest_result": get_backtest_result,
    "cex_sub_order": cex_sub_order,
    "options_broker_selftest": options_broker_selftest,
    "analyze_f1_td": analyze_f1_td,
    "analyze_f1_drawdown": analyze_f1_drawdown,
    "run_f1_analysis": run_f1_analysis,
    "get_f1_result": get_f1_result,
    "wallet_login_init": wallet_login_init,
    "wallet_login_poll": wallet_login_poll,
    "wallet_payment_set": wallet_payment_set,
    "wallet_setup": wallet_setup,
    "wallet_login_status": wallet_login_status,
    "wallet_status": wallet_status,
    "wallet_addresses": wallet_addresses,
    "wallet_balance": wallet_balance,
    "wallet_chains": wallet_chains,
    "wallet_history": wallet_history,
    "wallet_add": wallet_add,
    "wallet_switch": wallet_switch,
}

for _tool_name, _tool_fn in _TOOL_DISPATCH.items():
    mcp.add_tool(_tool_fn, name=_tool_name, description=_TOOL_DESCRIPTIONS[_tool_name])


def main() -> None:
    """Run the MCP stdio server (official mcp SDK, no banner output)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
