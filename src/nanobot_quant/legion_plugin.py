"""Legion Module 契约实现（nanobot-quant）。

Legion（nanobot-legion）作为通用运行时，只负责「发现模块 + 渲染界面」；
业务页面与置顶会话内容由本模块自己声明，Legion 不认识任何 quant 概念。

契约（sdk=1）见 nanobot-legion 仓库 ``docs/legion-module.md``：

- ``register_routes(app, ctx)``：注册全部业务页面
- ``sections(ctx)``：返回置顶会话的结构化内容（Section / Row 原子）

过渡期说明：现有 handler 的签名为 ``register_xxx_routes(app, gatekeeper)``，
故通过 ``ctx.gatekeeper`` 透传网关句柄；新模块不应依赖该句柄。
"""

from __future__ import annotations

import json as _json
import os as _os
import sys as _sys
from dataclasses import dataclass, field

SDK_VERSION = 1

# 契约版本（Legion 据此判断兼容性）
__all__ = ["MODULE", "SDK_VERSION", "Row", "Section", "QuantModule", "ROUTES"]


# ── 渲染原子（与 Legion 契约一致，不返回预拼 Markdown）─────────────


@dataclass
class Row:
    """置顶会话表格的一行。"""

    label: str
    desc: str = ""
    status: str = ""
    link: str = ""


@dataclass
class Section:
    """置顶会话的一个分组。"""

    heading: str
    rows: list[Row] = field(default_factory=list)
    desc: str = ""


# ── 页面注册表：(模块名, 注册函数名, 成功日志) ─────────────────────


ROUTES: tuple[tuple[str, str, str], ...] = (
    ("credential_handlers", "register_credential_routes", "📊 已注册 API 凭证管理路由"),
    ("mode_handlers", "register_mode_routes", "⚙️ 已注册交易模式管理路由"),
    ("live_handlers", "register_live_routes", "⚡ 已注册实盘交易开关路由"),
    ("wallet_handlers", "register_wallet_routes", "👛 已注册钱包管理路由"),
    ("gate_handlers", "register_gate_routes", "🏦 已注册 Gate CEX 账户路由"),
    ("token_handlers", "register_token_routes", "🪙 已注册代币管理路由"),
    ("td_params_handlers", "register_td_params_routes", "📐 已注册 TD 参数路由"),
    ("strategies_handlers", "register_strategy_routes", "📈 已注册策略选择路由"),
    ("exec_params_handlers", "register_exec_params_routes", "🛡️ 已注册执行参数路由"),
    ("td_table_handlers", "register_td_table_routes", "📊 已注册 TD 序列分析路由"),
    ("backtest_handlers", "register_backtest_routes", "📈 已注册回测路由"),
    ("okx_options_handlers", "register_okx_options_routes", "🟤 已注册 OKX 期权链路由"),
    (
        "options_backtest_handlers",
        "register_options_backtest_routes",
        "🟤 已注册期权回测路由（/config/backtest 期权分栏）",
    ),
    ("f1_backtest_handlers", "register_f1_routes", "🧪 已注册 F1 分析路由（/config/backtest/f1）"),
    ("ashare_handlers", "register_ashare_routes", "🇨🇳 已注册 A 股期权路由"),
)


def _log(ctx, msg: str) -> None:
    """写网关日志：优先用 ctx.log，否则落到 stderr（绝不污染 stdout）。"""
    fn = getattr(ctx, "log", None)
    if callable(fn):
        try:
            fn(msg)
            return
        except Exception:  # noqa: BLE001 — 日志失败不得影响业务
            pass
    print(f"[QUANT-MODULE] {msg}", file=_sys.stderr, flush=True)


def _data_root(ctx) -> str:
    root = getattr(ctx, "data_root", None)
    if root:
        return str(root)
    for env in ("DATA_ROOT", "NANOBOT_DATA_ROOT"):
        if _os.environ.get(env):
            return _os.environ[env]
    for guess in ("/data", "/mnt/workspace"):
        if _os.path.isdir(guess):
            return guess
    return ""


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — 配置缺失按默认值处理
        return {}


class QuantModule:
    """nanobot-quant 的 Legion Module 实现。"""

    name = "quant"
    display = "📊 业务管理"
    sdk = SDK_VERSION
    # 历史路径保持 /config（书签、文档、链接不变）；新模块应使用 /apps/<name>
    route_prefix = "/config"

    # ── 页面 ────────────────────────────────────────────────
    def register_routes(self, app, ctx) -> None:
        """注册全部业务页面。单个页面失败只记录，不影响其他页面。"""
        gatekeeper = getattr(ctx, "gatekeeper", None)
        registered = 0
        for module_name, func_name, label in ROUTES:
            try:
                module = __import__(f"nanobot_quant.{module_name}", fromlist=[func_name])
                register = getattr(module, func_name)
            except Exception as e:  # noqa: BLE001 — 插件缺失/损坏必须可见但不阻塞
                _log(ctx, f"⚠️ {module_name} 导入失败: {type(e).__name__}: {e}")
                continue
            try:
                register(app, gatekeeper)
            except Exception as e:  # noqa: BLE001
                _log(ctx, f"⚠️ {module_name} 路由挂载失败: {type(e).__name__}: {e}")
                continue
            registered += 1
            _log(ctx, label)
        _log(ctx, f"📊 nanobot-quant 模块：{registered}/{len(ROUTES)} 个页面已注册")

    # ── 置顶会话内容 ─────────────────────────────────────────
    def sections(self, ctx) -> list[Section]:
        """返回业务管理置顶会话的结构化内容。"""
        root = _data_root(ctx)

        mode = str(_read_json(_os.path.join(root, "legion", "mode.json")).get("mode") or "quant")
        mode_labels = {
            "quant": "📊 Quant 模式（TD Sequential）",
            "research": "🧠 Research 模式（VT Swarm）",
        }
        live = bool(_read_json(_os.path.join(root, "credentials", "live.json")).get("live", False))

        opt_path = _os.path.join(root, "credentials", "okx_options_params.json")
        opt_on: bool | None = None
        if _os.path.exists(opt_path):
            opt_on = bool((_read_json(opt_path).get("live") or {}).get("enabled", False))

        status_rows = [
            Row("交易模式", status=mode_labels.get(mode, mode), link="/config/mode"),
            Row(
                "实盘开关",
                status="🟢 实盘交易已开启" if live else "⚪ 实盘关闭（dry-run，不成交）",
                link="/config/live",
            ),
        ]
        if opt_on is not None:
            status_rows.append(
                Row(
                    "期权巡检",
                    status="🟤 期权巡检 开启" if opt_on else "🟤 期权巡检 未开启",
                    link="/config/okx-options",
                )
            )

        sections = [
            Section(
                "⚙️ 当前状态",
                status_rows,
                desc="各分类入口见下方分组；状态开关即时生效，无需重启空间。",
            ),
            Section(
                "⚙️ 交易控制",
                [
                    Row("交易模式", "Quant（TD 确定性）/ Research（VT Swarm 辩论）二选一", link="/config/mode"),
                    Row("实盘开关", "WebUI 唯一授权（dry-run 不成交 / live 上链）", link="/config/live"),
                    Row("执行参数", "风控上限 / 执行通道 / 标的池 / 场景 / TD 启停", link="/config/exec"),
                ],
            ),
            Section(
                "📐 策略与信号",
                [
                    Row("策略选择", "选择 Quant 路线的信号策略（TD 变体等）", link="/config/strategy"),
                    Row("TD 策略参数", "TD 算法核心参数（权重 / setup / countdown）", link="/config/td-params"),
                    Row("TD 序列分析", "setup/countdown/TDST/score 轨迹 + 历史信号统计", link="/config/td-table"),
                ],
            ),
            Section(
                "📊 回测验证",
                [Row("打开回测", link="/config/backtest")],
                desc="场景回放引擎（与实盘同一策略决策代码）：历史 K 线 + 模拟撮合（手续费/滑点/分批），输出 ROI、成交明细与每 slot 净值。",
            ),
            Section("🔑 账户与资产", self._credential_rows() + [
                Row("钱包管理", "DEX（Agentic Wallet 子钱包）与 CEX（子账号）分两大类", link="/config/wallets"),
                Row("代币地址", "tokens.json 登记（确认门控，执行前校验）", link="/config/tokens"),
            ]),
            Section(
                "🟤 OKX 期权",
                [Row("打开期权链", link="/config/okx-options")],
                desc="U 本位线性期权（美元现金结算）：期权链 + IV/HV + 卖 put 定价辅助；持仓与台账监控；逐仓担保自动追加；到期巡检自动判定（OTM 作废关账 / ITM 赔付补买）。",
            ),
            Section(
                "🇨🇳 A 股期权",
                [Row("打开 A 股期权", link="/config/ashare")],
                desc="只读盘口采集（东财合约清单 + 新浪五档，交易时段每轮落盘）：用于校准回测滑点与判断合约当时是否可成交；不参与任何交易决策。",
            ),
        ]
        return sections

    # ── MCP 工具声明（契约可选钩子）───────────────────────────
    def mcp_specs(self, ctx) -> dict:
        """把 quant 的 MCP server 声明交给 Legion 注入 agent config。

        契约可选钩子：未实现的模块 = 不提供 MCP 工具。这里只读 mcp_spec 的
        内存注册表（登记在 import 时完成），不连网、不读凭证，可幂等调用。
        导入放在函数体内 —— ``legion_plugin`` 顶层必须保持轻量，因为
        ``squad_config_sync`` 会在启动早期 import 每个模块的 plugin 模块。
        """
        from nanobot_quant.mcp_spec import discover

        return discover()

    # ── 内部 ────────────────────────────────────────────────
    @staticmethod
    def _credential_rows() -> list[Row]:
        """已注册凭证类型 → 每类一行入口（发现失败则返回空，不影响其他分组）。"""
        try:
            from nanobot_quant.credential_registry import discover as discover_credentials

            specs = discover_credentials()
        except Exception:  # noqa: BLE001
            return []
        if not specs:
            return []
        rows: list[Row] = []
        for name, spec in specs.items():
            icon = getattr(spec, "icon", "🔑")
            display = getattr(spec, "display", name)
            desc = getattr(spec, "description", "")
            rows.append(Row(f"{icon} {display}", desc, link=f"/config/credentials/{name}"))
        return rows


# Legion 通过 entry point ``nanobot_legion.modules`` 发现该对象
MODULE = QuantModule()
