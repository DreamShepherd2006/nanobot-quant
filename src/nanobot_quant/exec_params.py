"""On-chain execution parameters — defaults, storage & validation.

The WebUI page ``/config/exec`` edits these parameters and persists them
to ``{data_root}/credentials/exec_params.json`` (next to ``live.json``).
Every consumer (pipeline, execute_signal, broker) reloads the latest file
on each call, so changes take effect immediately — no restart required.

Control-plane design (2026-08-08):

- These parameters are SYSTEM-LEVEL policy: they are locked by the
  WebUI and are NOT exposed through MCP (LLM cannot pass them in
  execute_signal).  Only portfolio_value / quantity (call-level sizing)
  stay in the MCP schema.
- ``max_position_pct`` is enforced live by RiskEngine on every order.
- ``slippage`` / ``sol_buffer_pct`` are passed to OnchainOSBroker for
  actual swap execution.
- ``max_drawdown_pct`` / ``stop_loss_pct`` are effective in backtest
  today; on the execute_signal path they are formal checks
  (no position context yet) — the parameters are configured here so a
  future position-context integration picks them up automatically.
- ``td_*`` / ``quantity_mode`` drive the TD autonomous StrategyExecutor
  loop (P2 B3): ``td_enabled`` is the WebUI on/off switch.

P1 loop mode (execution_mode / loop_interval_seconds) was retired in B3:
execute_signal is synchronous only (direct).

Missing / invalid file → DEFAULT_EXEC_PARAMS, which is byte-for-byte
identical to the pre-parameterisation hardcoded behaviour (20% position
limit, 15% drawdown, 10% stop-loss, 1% slippage, 5% SOL buffer).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot_quant.data_sources.periods import DISPLAY_NAMES, PERIODS

# ── Schema / defaults ────────────────────────────────────────────────────

#: 多场景（多时间框架）定义（2026-08-20 S1，第二十二章）。
#: 三个场景各自独立周期/参数/子账号池，捕捉不同尺度的能量衰减：
#:   high = 高频短线（1m，~5次/天）；mid = 中频波段（15m/1H，~1次/天）；
#:   low = 低频趋势（1D，~1次/周）。
#: S1 仅配置层：high 场景与扁平参数双向同步（td_live 仍消费扁平），
#: mid/low 独立配置但暂不消费（S3 调度分频接入）。
SCENES: dict[str, dict[str, str]] = {
    "high": {"label": "高频", "freq": "1m", "desc": "~5次/天 · 短线能量衰减"},
    "mid": {"label": "中频", "freq": "15m/1H", "desc": "~1次/天 · 波段能量衰减"},
    "low": {"label": "低频", "freq": "1D", "desc": "~1次/周 · 趋势能量衰减"},
}

#: 场景字段 → 扁平参数键（high 场景与扁平双向同步；sub_accounts 无扁平对应）。
SCENE_FIELD_MAP: dict[str, str] = {
    "enabled": "td_enabled",
    "sleeptime": "td_sleeptime",
    "symbols": "td_symbols",
    "quantity_mode": "quantity_mode",
    "td_quantity": "td_quantity",
    "td_fixed_amount": "td_fixed_amount",
    "batches": "td_batches",
    "exit_order": "exit_order",
    "stop_loss_pct": "stop_loss_pct",
    "take_profit_pct": "take_profit_pct",
    "td_start_slot": "td_start_slot",
    "min_account_value": "min_account_value",
}

#: 场景级 TD 阈值字段（S3b-2；min_hold_bars 2026-08-28）：值域同 td_params 或
#: exec_params 全局键（min_hold_bars），但缺省 None = 回退全局
#: （td_params.json / 扁平 exec_params 键），不参与 high↔扁平同步。
SCENE_THRESHOLD_FIELDS: tuple[str, ...] = (
    "entry_setup", "entry_countdown", "exit_setup", "exit_countdown",
    "min_hold_bars",
)

#: 场景专属参数（无扁平对应；high 高9 出场逻辑 2026-08-25；cd13 通道 2026-08-27；
#:   动能判断双档 2026-08-29 拍板落地）。
#: sell_only_profit_high / _low = 高9 毛浮盈门上下档（2026-08-29 拍板双档）：
#:   pnl ≥ high 无条件卖；pnl < low 死扛；[low, high) 区间由动能判断（momentum_exit）。
#:   毛口径 (price−entry)/entry 未扣手续费，用户自行计算含成本阈值（如 Gate 双程 0.2% → 0.002）。
#: momentum_exit = 高9 动能判断开关（true=拍板主方案；false=回退 high 简单门，方案 B）。
#: cd_stall_n = 动能停滞阈值：cd_sell 连续 N 根无 +1 判定动能弱（下门落袋）。
#: td_sell_all = 高9 一次平掉所有满足盈利门的 open 批次（false=每轮只平一个，现有行为）。
#: cd_exit_min_profit = cd 13 通道保本门（≥此值卖；<死扛；0=不亏本金就走，承担交易成本）。
#: cd_exit_all = cd 13 一次平掉所有 ≥ 保本门的 open 批次（false=每轮只平一个）。
#: cd_entry_setup_gap = cd 入场时效门槛（2026-08-29 拍板 B）：cd_buy ≥ entry_countdown 触发做多时，
#:   setup 最近一次归零（>0 → 0）距当前 ≤ N 根才允许——保留「setup 结构尚在进行」的 cd 确认
#:   （LINK 场景），过滤跨多轮周期的陈旧信号；setup 从未归零（结构连续）恒允许；0=关闭（回归
#:   旧 cd 独立触发行为）。cd 进场门限建立在 setup 结构延续基础上，非独立计算。
SCENE_ONLY_FIELDS: tuple[str, ...] = (
    "sell_only_profit_high", "sell_only_profit_low", "momentum_exit", "cd_stall_n",
    "td_sell_all", "cd_exit_min_profit", "cd_exit_all", "cd_entry_setup_gap",
)

#: 场景卡片字段渲染顺序。
SCENE_FIELD_ORDER: tuple[str, ...] = (
    "enabled", "sleeptime", "symbols", "quantity_mode",
    "td_quantity", "td_fixed_amount", "batches", "sub_accounts",
    "entry_setup", "entry_countdown", "cd_entry_setup_gap",
    "exit_setup", "exit_countdown",
    "min_hold_bars",
    "exit_order", "stop_loss_pct", "take_profit_pct",
    "sell_only_profit_high", "sell_only_profit_low", "momentum_exit", "cd_stall_n",
    "td_sell_all", "cd_exit_min_profit", "cd_exit_all",
    "td_start_slot", "min_account_value",
)

#: 默认 Gate 子账号池（S1 配置层默认值；S3 消费时按 gate.json 实际列表）。
DEFAULT_SUB_ACCOUNTS: tuple[str, ...] = (
    "gate_bot1", "gate_bot2", "gate_bot3", "gate_bot4", "gate_bot5",
)

#: 场景默认配置（mid/low 默认停用；high 默认与扁平 td_* 一致）。
DEFAULT_SCENES: dict[str, dict[str, Any]] = {
    "high": {
        "enabled": False, "sleeptime": "1m", "symbols": ["SOL"],
        "quantity_mode": "fixed", "td_quantity": 10, "td_fixed_amount": 10.0,
        "batches": 4,
        "sub_accounts": ["gate_bot1", "gate_bot2", "gate_bot3", "gate_bot4"],
        "entry_setup": None, "entry_countdown": None, "cd_entry_setup_gap": 5,
        "exit_setup": None, "exit_countdown": None,
        "min_hold_bars": None,
        "exit_order": "fifo", "stop_loss_pct": 0.05, "take_profit_pct": 0.03,
        "sell_only_profit_high": 0.0, "sell_only_profit_low": 0.002,
        "momentum_exit": True, "cd_stall_n": 3,
        "td_sell_all": False,
        "cd_exit_min_profit": 0.0, "cd_exit_all": True,
        "td_start_slot": 1, "min_account_value": 0,
    },
    "mid": {
        "enabled": False, "sleeptime": "15m", "symbols": ["SOL"],
        "quantity_mode": "fixed", "td_quantity": 10, "td_fixed_amount": 10.0,
        "batches": 3,
        "sub_accounts": ["gate_bot5", "gate_bot6", "gate_bot7"],
        "entry_setup": None, "entry_countdown": None, "cd_entry_setup_gap": 5,
        "exit_setup": None, "exit_countdown": None,
        "min_hold_bars": None,
        "exit_order": "fifo", "stop_loss_pct": 0.10, "take_profit_pct": 0.05,
        "sell_only_profit_high": 0.0, "sell_only_profit_low": 0.002,
        "momentum_exit": True, "cd_stall_n": 3,
        "td_sell_all": False,
        "cd_exit_min_profit": 0.0, "cd_exit_all": True,
        "td_start_slot": 1, "min_account_value": 0,
    },
    "low": {
        "enabled": False, "sleeptime": "1D", "symbols": ["SOL"],
        "quantity_mode": "fixed", "td_quantity": 10, "td_fixed_amount": 10.0,
        "batches": 3,
        "sub_accounts": ["gate_bot8", "gate_bot9", "gate_bot10"],
        "entry_setup": None, "entry_countdown": None, "cd_entry_setup_gap": 5,
        "exit_setup": None, "exit_countdown": None,
        "min_hold_bars": None,
        "exit_order": "fifo", "stop_loss_pct": 0.15, "take_profit_pct": 0.10,
        "sell_only_profit_high": 0.0, "sell_only_profit_low": 0.002,
        "momentum_exit": True, "cd_stall_n": 3,
        "td_sell_all": False,
        "cd_exit_min_profit": 0.0, "cd_exit_all": True,
        "td_start_slot": 1, "min_account_value": 0,
    },
}


#: Full execution parameter set. Defaults == old hardcoded values.
DEFAULT_EXEC_PARAMS: dict[str, Any] = {
    # ── ① Risk control ───────────────────────────────────────────────
    "max_position_pct": 0.20,   # float (0,1] — single-order value ≤ pv × pct
    "max_drawdown_pct": 0.15,   # float (0,1] — account drawdown threshold
    # 扁平 stop_loss_pct 保留：execute_signal/pipeline RiskEngine 直调默认
    # （TD live 批次止损用 scenes.*.stop_loss_pct；保存时 high 值同步回此键）
    "stop_loss_pct": 0.10,      # float [0,1] — per-position stop-loss (0=disabled)
    # ── ② Execution quality ──────────────────────────────────────────
    "fee_rate": 0.001,         # float [0,0.01] — 单边交易成本（Gate taker 0.1%）；
                                # 止损/止盈按净值触发：pnl_net = pnl_gross − 2×fee_rate
    "slippage": 0.01,           # float [0,1) — swap slippage tolerance in percent (1 = 1%)
    "sol_buffer_pct": 0.05,     # float [0,1) — extra SOL reserved on buys
    "execution_channel": "okx_dex", # enum — 实例名（okx_dex=OKX DEX 链上 / gate=Gate.io 交易所）；旧值 dex/cex 自动迁移
    "min_position_value": 1.0, # float ≥0 — 对账导入阈值(USD)：链上持仓价值低于该值视为 dust 不导入（0=关闭）
    # ── ③ TD 循环运行（全局）──────────────────────────────────────────
    "td_bars": 120,             # int 20-300 — TD 每轮拉取最近 N 根 K 线（固定窗口）
    "kline_concurrency": 4,     # int 1-20 — TD 每轮并发拉取各标的 K 线的线程数（1=串行；标的池大时加大提速）
    "min_hold_bars": 10,        # int 0-300 — 买入后 N 根 bar 内 TD SELL（高9/cd13）不触发（0=关闭；止损/止盈不受限，2026-08-28）
    "trend_period": "1H",      # enum — 大周期趋势过滤周期（TD 趋势状态按该周期 K 线计算；默认 1H，可改 15m/4H/1D 等）
    # ── 贝叶斯闸门（2026-08-31，回测侧，默认关）────────────────────────
    #  gate_enabled 只作用于回测（driver 注入 strategy.parameters）；
    #  td_live 不消费 → 实盘 TD 恒保持原样，实盘接入待回测裁决后另行讨论。
    #  F1×时段后验 P(被套) ≥ gate_red_min 禁买（负反馈只拦不买；
    #  SELL/止损/止盈不碰）。0.45=red-only（只拦高危），0.20=yellow+red。
    "gate_enabled": False,      # bool — 回测接入贝叶斯闸门（默认关=回测与原版完全一致）
    "gate_red_min": 0.45,       # float 0.05-0.9 — 闸门禁买阈值（后验≥该值禁买）
    # ── ⑤ UI ───────────────────────────────────────────────────────────
    "td_ui_refresh_s": 10,    # int 3-300 — /config/td-table 实时监控 tab 自动刷新间隔（秒）
    "position_display_min_usd": 1.0,  # float 0-100 — 持仓小节显示阈值（<$X 不显示，0=全部；2026-08-26）
    # ── ④ 多场景（S1 配置层，2026-08-20；high ↔ 扁平同步）──────────────
    #  td_enabled / td_symbols / td_sleeptime / quantity_mode / td_quantity /
    #  td_fixed_amount / td_batches / exit_order / take_profit_pct /
    #  td_start_slot / min_account_value 移入 scenes（high 与扁平双向同步），
    #  min_position_value 归入 ② 执行质量。
    "scenes": DEFAULT_SCENES,
}

#: Valid TD main-loop cadences (lumibot sleeptime strings).
#: 2026-08-24 方案 C：全量 16 周期（与数据源注册表 spec.bars 对齐，UI 按执行
#: 通道过滤——cex=Gate 16 项 / dex=OnchainOS 7 项）。
TD_SLEEPTIMES: tuple[str, ...] = tuple(PERIODS)
TD_SLEEPTIME_LABELS: dict[str, str] = dict(DISPLAY_NAMES)

#: Valid position-sizing modes for the TD autonomous strategy.
#:  fixed = 固定数量（td_quantity 个币）；value = pv_slot × max_position_pct（百分比仓位）；
#:  fixed_amount = 固定金额（td_fixed_amount U，每笔建仓花固定稳定币金额，2026-08-19 新增）。
QUANTITY_MODES: tuple[str, ...] = ("fixed", "value", "fixed_amount")

#: Valid batch exit orders.
EXIT_ORDERS: tuple[str, ...] = ("fifo", "lifo")

#: Valid execution channels — concrete broker instances (spec names).
#: 大类（dex/cex）由 spec.family 表达，UI 按大类分组显示（方案 C，2026-08-17）：
#:   链上 DEX 组：okx_dex；交易所（CEX）组：gate。
#: 新增交易所 = 注册 BrokerSpec + 此处加一项 + enum_groups 加一项，上层零改动。
EXECUTION_CHANNELS: tuple[str, ...] = ("okx_dex", "gate")

#: Legacy channel values auto-migrated on load/save (2026-08-17 方案 C).
LEGACY_CHANNEL_ALIASES: dict[str, str] = {"dex": "okx_dex", "cex": "gate"}


def normalize_execution_channel(value: Any) -> str:
    """Normalize an execution-channel value to the concrete instance name.

    Idempotent: legacy ``dex``/``cex`` → ``okx_dex``/``gate``; unknown values
    pass through unchanged (callers fail closed later, never silently map).
    """
    v = str(value) if value is not None else ""
    return LEGACY_CHANNEL_ALIASES.get(v, v)

#: Human-readable bounds used by the WebUI form validation + display.
PARAM_META: dict[str, dict[str, Any]] = {
    "max_position_pct": {
        "group": "risk", "min": 0.01, "max": 1.0, "step": 0.05, "std": 0.20,
        "label": "单仓上限", "hint": "单笔订单价值 ≤ 组合 × 该比例（实盘真实生效）",
    },
    "max_drawdown_pct": {
        "group": "risk", "min": 0.01, "max": 1.0, "step": 0.05, "std": 0.15,
        "label": "回撤阈值", "hint": "组合净值从峰值回撤超限触发风控（回测/纸交易生效；实盘待持仓上下文）",
    },
    "stop_loss_pct": {
        "group": "scene", "min": 0.0, "max": 1.0, "step": 0.05, "std": 0.10,
        "label": "止损阈值", "hint": "该场景每批净浮亏（已扣双边交易成本 fee_rate）≥ 阈值强制平仓（实盘生效，逐批独立；0=关闭；execute_signal 直调用 high 值）",
    },
    "fee_rate": {
        "group": "exec", "min": 0.0, "max": 0.01, "step": 0.001, "std": 0.001,
        "label": "交易成本(费率)", "hint": "单边交易成本（Gate taker 0.1%=0.001）。止损/止盈按净值触发：净盈亏 = 毛盈亏 − 2×费率（双边）；0=不计成本",
    },
    "slippage": {
        "group": "exec", "min": 0.0, "max": 1.0, "step": 0.01, "std": 0.01,
        "channels": "dex",
        "label": "滑点容忍", "hint": "swap 滑点容忍（百分比，1=1%，如 0.5=0.5%）；过小易滑点超限失败（82112），过大成交价劣",
    },
    "sol_buffer_pct": {
        "group": "exec", "min": 0.0, "max": 1.0, "step": 0.01, "std": 0.05,
        "channels": "dex",
        "label": "SOL 缓冲", "hint": "BUY 时按比例预留 SOL 覆盖 gas 与报价-成交间价格波动（仅 DEX 通道）",
    },
    "execution_channel": {
        "group": "exec", "type": "enum", "enum": list(EXECUTION_CHANNELS), "std": "okx_dex",
        "enum_labels": {
            "okx_dex": "OKX DEX（链上 · OnchainOS 子钱包）",
            "gate": "Gate.io（交易所 · 子账号）",
        },
        "enum_groups": {
            "链上 DEX（TEE 签名 · 子钱包）": ["okx_dex"],
            "交易所 CEX（API Key · 子账号）": ["gate"],
        },
        "label": "执行通道", "hint": "两大执行家族是完全不同的概念：dex=链上 DEX（OKX OnchainOS 子钱包，TEE 签名，默认）；cex=交易所（当前实例 Gate.io，子账号 API Key）。只影响之后的新下单（execute_signal / TD 循环），不迁移持仓；切到交易所时 TD 循环 K 线数据源联动切换为同所 Gate CEX 公共端点。未来多家 CEX/DEX 在此下拉分组内扩展",
    },
    "td_enabled": {
        "group": "scene", "type": "bool", "std": False,
        "label": "TD 自主运行", "hint": "开启后 TD 自主策略在 quant agent 进程内驻留 StrategyExecutor 主循环（标的/周期/数量见下）",
    },
    "td_symbols": {
        "group": "scene", "type": "list", "std": ["SOL"],
        "label": "TD 标的池", "hint": "多标的扫描：每轮遍历池子算 TD，谁 Setup 9 谁执行（同 bar 按池子顺序全部处理）。从 /config/tokens 登记代币选（SOL 登记后可选，稳定币不列入）",
    },
    "td_sleeptime": {
        "group": "scene", "type": "enum", "enum": list(TD_SLEEPTIMES),
        "enum_labels": TD_SLEEPTIME_LABELS, "std": "1D",
        "period_field": True,
        "label": "TD 周期", "hint": "主循环周期 = lumibot sleeptime 与 K 线粒度（1D 默认；切换执行通道后下拉按该所支持周期过滤）",
    },
    "quantity_mode": {
        "group": "scene", "type": "enum", "enum": list(QUANTITY_MODES), "std": "fixed",
        "label": "数量模式", "hint": "fixed=固定 td_quantity（默认 10，回测语义不变）；value=按实时 slot 总资产 × 单仓上限；fixed_amount=每笔固定金额（td_fixed_amount）",
    },
    "td_quantity": {
        "group": "scene", "min": 1, "max": 100000, "step": 1, "std": 10, "integer": True,
        "show_if": {"quantity_mode": "fixed"},
        "label": "TD 固定数量", "hint": "quantity_mode=fixed 时的下单数量（默认 10）",
    },
    "td_fixed_amount": {
        "group": "scene", "min": 1.0, "max": 5000.0, "step": 1.0, "std": 10.0,
        "show_if": {"quantity_mode": "fixed_amount"},
        "label": "TD 固定金额", "hint": "quantity_mode=fixed_amount 时的每笔建仓金额（U：CEX=USDT / DEX=USDC）。固定金额模式跳过单仓上限（max_position_pct）校验（金额即用户显式仓位），但资金检查保留；CEX 通道需 ≥3U（Gate 最小单），DEX 无下限",
    },
    "td_bars": {
        "group": "td", "min": 20, "max": 300, "step": 1, "std": 120, "integer": True,
        "label": "K 线窗口", "hint": "TD 每轮拉取最近 N 根 K 线（固定窗口，不累积增长；300 = onchainos CLI 单次上限）。与分析页 K 线数设一致可完全对照",
    },
    "kline_concurrency": {
        "group": "td", "min": 1, "max": 20, "step": 1, "std": 4, "integer": True,
        "label": "K 线并发拉取", "hint": "TD 每轮并发拉取各标的 K 线的线程数（1=串行）。串行 12 标的 ≈30s+/轮，并发 4 ≈10s；标的池大时加大提速。Gate 公共端点限流 200 次/10s/端点（IP），并发数 ≤ 池子大小即可",
    },
    "min_hold_bars": {
        "group": "td", "min": 0, "max": 300, "step": 1, "std": 10, "integer": True,
        "label": "最短持有期（bar）",
        "hint": "买入后 N 根 bar 内 TD SELL（高9/cd13）不触发——避免震荡市双向 countdown 刚买就卖（UNI 49 秒案例）；止损/止盈不受限（风控优先）；0=关闭；场景卡片内留空 = 跟随全局值（2026-08-28）",
    },
    "trend_period": {
        "group": "td", "type": "enum", "enum": list(PERIODS),
        "enum_labels": dict(DISPLAY_NAMES), "std": "1H",
        "period_field": True,
        "label": "趋势过滤周期",
        "hint": "TD 趋势状态（涨势/跌势/弹簧）按该周期 K 线计算，用于大周期方向过滤（单向闸门，Step 3 接入交易；当前仅展示，默认 1H）。15m 更灵敏、4H/1D 更钝；只读展示阶段零交易影响",
    },
    "gate_enabled": {
        "group": "td", "type": "bool", "std": False,
        "label": "贝叶斯闸门（回测）",
        "hint": "回测 BUY 接入 F1×时段贝叶斯闸门：后验 P(被套|F1,时段) ≥ 阈值禁买（负反馈只拦不买，SELL/止损/止盈不碰）。默认关=回测与原版完全一致；实盘 TD 不消费此开关，实盘接入待回测裁决后另行讨论（2026-08-31）",
    },
    "gate_red_min": {
        "group": "td", "min": 0.05, "max": 0.9, "step": 0.05, "std": 0.45,
        "label": "闸门禁买阈值",
        "hint": "后验 P(被套) ≥ 该值禁买。0.45=red-only（只拦高危组合，样本外被套率 34%+）；0.20=yellow+red（拦所有高于平均风险组合）；回测裁决两档对比后定默认",
    },
    # ── ④ 仓位与分批 ──────────────────────────────────────────────────
    "td_batches": {
        "group": "scene", "min": 1, "max": 50, "step": 1, "std": 1, "integer": True,
        "label": "批次数量（子钱包）",
        "label_cex": "批次数量（子账号）",
        "hint": "1=单仓模式（现状）；>1 时每批绑定一个 Agentic Wallet 子钱包，保存后自动创建不足的子钱包并建立映射",
        "hint_cex": "1=单仓模式（现状）；>1 时每批绑定一个 Gate 子账号（slot↔gate_bot1-5），子账号下单用自身 API Key",
    },
    "exit_order": {
        "group": "scene", "type": "enum", "enum": list(EXIT_ORDERS), "std": "fifo",
        "label": "平仓顺序", "hint": "TD SELL 信号/止损/止盈命中多批时按此顺序平仓：fifo=先买先卖（默认）/ lifo=后买先卖",
    },
    "take_profit_pct": {
        "group": "scene", "min": 0.0, "max": 1.0, "step": 0.01, "std": 0.0,
        "label": "止盈线", "hint": "每批净浮盈（已扣双边交易成本 fee_rate）≥ 该值即平仓（0=关闭，纯 TD SELL + 止损；如 0.05 = 净 5% 落袋）",
    },
    "td_start_slot": {
        "group": "scene", "min": 1, "max": 50, "step": 1, "std": 1, "integer": True,
        "label": "建仓起始批次", "hint": "BUY 从该 slot 开始扫描（完整循环 + 起点偏移；设 3 → 3→4→5→1→2；资金不足自动跳下一 slot）",
    },
    "min_account_value": {
        "group": "scene", "min": 0, "max": 1000000, "step": 10, "std": 0,
        "label": "子账户最小资金(USD)", "hint": "BUY 时目标 slot 子钱包总资产低于该值则跳过该槽位（TD SLOT SKIP min_account_value），避免小资金碎仓；0=关闭。SELL/止损/止盈平仓不受限（平仓永远允许）",
    },
    "min_position_value": {
        "group": "exec", "min": 0, "max": 1000000, "step": 1, "std": 1.0,
        "label": "对账导入阈值(USD)",
        "hint": "启动对账时链上持仓价值低于该值视为 dust 不导入（slot 保持可建仓），避免微量残留（如卖出后尾仓 $0.13）占用资金槽位；0=关闭。DEX/CEX 通用（2026-08-26 起 CEX 不再用 Gate min_quote 动态阈值，与交易门槛解耦）",
    },
    "entry_setup": {
        "group": "scene", "min": 1, "max": 20, "step": 1, "std": 9, "integer": True,
        "label": "入场 Setup 阈值", "hint": "场景级覆盖（S3b-2）；留空 = 跟随全局 td_params（策略选择页设置）",
    },
    "entry_countdown": {
        "group": "scene", "min": 1, "max": 20, "step": 1, "std": 13, "integer": True,
        "label": "入场 Countdown 阈值", "hint": "场景级覆盖；留空 = 跟随全局 td_params（策略选择页设置）。cd_buy ≥ N 触发做多（与 setup 双信号 OR）",
    },
    "cd_entry_setup_gap": {
        "group": "scene", "min": 0, "max": 300, "step": 1, "std": 5, "integer": True,
        "label": "CD 入场时效门槛(bar)",
        "hint": "cd_buy ≥ 入场 Countdown 阈值触发做多时，setup 最近一次归零距当前 ≤ N 根才允许——保留「setup 结构尚在进行」的 cd 确认（LINK 场景），过滤跨多轮周期的陈旧信号（2026-08-29 拍板 B）；setup 从未归零（结构连续）恒允许；0=关闭（回归旧 cd 独立触发行为）",
    },
    "exit_setup": {
        "group": "scene", "min": 1, "max": 20, "step": 1, "std": 9, "integer": True,
        "label": "平仓 Setup 阈值", "hint": "场景级覆盖（S3b-2）；留空 = 跟随全局 td_params（策略选择页设置）",
    },
    "exit_countdown": {
        "group": "scene", "min": 1, "max": 20, "step": 1, "std": 13, "integer": True,
        "label": "平仓 Countdown 阈值", "hint": "场景级覆盖（S3b-2）；留空 = 跟随全局 td_params（策略选择页设置）",
    },
    "sell_only_profit_high": {
        "group": "scene", "min": 0.0, "max": 1.0, "step": 0.001, "std": 0.0,
        "label": "高9盈利门(毛·上)",
        "hint": "高9 出场上限门：毛浮盈 ≥ 该值无条件卖（让利润奔跑的终点，2026-08-29 双档）；0=关闭（无条件卖，现有行为）。毛口径=(现价−入场)/入场，未扣手续费，用户自行计算含成本阈值（如 Gate 双程 0.2% → 0.002）",
    },
    "sell_only_profit_low": {
        "group": "scene", "min": 0.0, "max": 1.0, "step": 0.001, "std": 0.002,
        "label": "高9盈利门(毛·下)",
        "hint": "高9 出场下限门：毛浮盈 < 该值死扛（不卖）；[下, 上) 区间由动能判断（见「高9动能判断」）。0=仅浮亏死扛（[0, 上) 全部动能判断）",
    },
    "momentum_exit": {
        "group": "scene", "type": "bool", "std": True,
        "label": "高9动能判断",
        "hint": "true=启用动能判断（2026-08-29 拍板主方案）：毛浮盈在 [下, 上) 区间时，动能强（setup_sell≥10 或 cd_sell>0 且未停滞）持有至上门；动能弱（cd_sell 连续 N 根无 +1）下门落袋；未知（首次高9）持有。false=动能判断整体关闭、回固定上门简单门（方案 B）",
    },
    "cd_stall_n": {
        "group": "scene", "min": 1, "max": 30, "step": 1, "std": 3, "integer": True,
        "label": "动能停滞阈值(bar)",
        "hint": "cd_sell 连续 N 根无 +1 判定动能弱（下门落袋）；仅在动能判断开启时生效（2026-08-29 拍板）",
    },
    "td_sell_all": {
        "group": "scene", "type": "bool", "std": False,
        "label": "高9全平",
        "hint": "true=高9 一次平掉所有满足盈利门的 open 批次；false=每轮只平一个（现有行为）",
    },
    "cd_exit_min_profit": {
        "group": "scene", "min": 0.0, "max": 1.0, "step": 0.001, "std": 0.0,
        "label": "cd13保本门(毛)",
        "hint": "cd_sell 到达平仓 Countdown 阈值时，毛浮盈 ≥ 该值的批次平仓（保本离场）；< 该值死扛（负浮盈恒不卖）。默认 0 = 不亏本金就走（承担交易成本，2026-08-27 拍板）",
    },
    "cd_exit_all": {
        "group": "scene", "type": "bool", "std": True,
        "label": "cd13全平",
        "hint": "true=cd 13 一次平掉所有 ≥ 保本门的 open 批次；false=每轮只平一个",
    },
    "sub_accounts": {
        "group": "scene", "type": "list", "std": ["gate_bot1"],
        "label": "子账号池", "hint": "本场景使用的 Gate 子账号（slot i ↔ 列表第 i 个；S3 调度分频起消费，S1 仅配置）",
    },
    # ── ⑤ UI ────────────────────────────────────────────────────────────
    "td_ui_refresh_s": {
        "group": "td", "min": 3, "max": 300, "step": 1, "std": 10, "integer": True,
        "label": "监控刷新(秒)", "hint": "/config/td-table「实时监控」tab 自动刷新间隔",
    },
    "position_display_min_usd": {
        "group": "td", "min": 0, "max": 100, "step": 0.5, "std": 1.0,
        "label": "持仓显示阈值(USDT)",
        "hint": "实时监控「持仓（open 批次）」价值低于该值不显示（0=显示全部）。2026-08-26 用户拍板：显示阈值独立于交易门槛 min_quote（<$3 但 ≥$1 的仓位可显示）",
    },
}

GROUP_TITLES = {
    "risk": "① 风险控制（WebUI 锁死 — LLM 不可改）",
    "exec": "② 执行通道与质量（WebUI 锁死 — LLM 不可改）",
    "td": "③ TD 循环运行（全局）",
    "scene": "④ 多场景（多时间框架）",
}


# ── Path / load / save ───────────────────────────────────────────────────

def exec_params_path() -> Path:
    """Path to the persisted exec_params.json (WebUI 业务管理 → 执行参数)."""
    for root in ("/data", "/mnt/workspace"):
        d = Path(root) / "legion" / "credentials"
        try:
            if d.exists():
                return d / "exec_params.json"
        except OSError:
            continue
    return Path.home() / ".exec_params.json"


def validate_exec_param(key: str, value: Any) -> str | None:
    """Return an error message for an invalid value, or None if valid."""
    meta = PARAM_META.get(key)
    if meta is None:
        return "未知参数"
    vtype = meta.get("type", "float")
    if vtype == "bool":
        return None if isinstance(value, bool) else "必须是布尔值"
    if vtype == "list":
        if not isinstance(value, list) or not value:
            return "必须是非空列表"
        if not all(isinstance(v, str) and v.strip() for v in value):
            return "列表项必须是非空字符串"
        return None
    if vtype == "enum":
        if value not in meta["enum"]:
            return f"必须是 {'/'.join(meta['enum'])} 之一"
        return None
    if vtype == "str":
        if not isinstance(value, str) or not value.strip():
            return "不能为空"
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"必须是数字（{meta['min']}–{meta['max']}）"
    lo, hi = meta["min"], meta["max"]
    if meta.get("integer") and int(value) != value:
        return f"必须是整数（{lo}–{hi}）"
    if value < lo or value > hi:
        return f"超出范围 {lo}–{hi}"
    return None


def load_exec_params() -> dict[str, Any]:
    """Load persisted params, merged over defaults (validated keys only).

    Missing / invalid file → defaults.  A key saved with a value that no
    longer validates is ignored (falls back to the default), so a WebUI
    range change can never poison execution.

    2026-08-20 S1：旧文件无 scenes → 自动从扁平参数迁移生成（high=当前
    扁平配置，mid/low=默认停用）；加载后 high 场景与扁平参数同步（场景
    为未来主配置，S1 阶段 td_live 仍消费扁平——两者保持一致）。
    """
    merged = dict(DEFAULT_EXEC_PARAMS)
    raw = _read_raw()
    if raw is None:
        raw = {}
    # 迁移：旧版 execution_channel 大类值（dex/cex）→ 实例名（okx_dex/gate），2026-08-17 方案 C
    if "execution_channel" in raw:
        raw["execution_channel"] = normalize_execution_channel(raw["execution_channel"])
    for key in merged:
        if key == "scenes":
            continue
        if key in raw and validate_exec_param(key, raw[key]) is None:
            merged[key] = raw[key]
    # 迁移：旧版单标的 td_symbol → td_symbols（标的池，2026-08-10）
    if "td_symbols" not in raw and raw.get("td_symbol"):
        raw["td_symbols"] = [raw["td_symbol"]]
    # S1：scenes 加载/迁移（2026-08-20）
    merged["scenes"] = _load_scenes(raw)
    # high 场景 → 扁平同步（td_live 消费扁平 = high 场景配置）
    _sync_flat_from_high(merged)
    return merged


def _load_scenes(raw: dict) -> dict[str, dict[str, Any]]:
    """从原始文件加载 scenes：缺失时从扁平参数迁移生成。"""
    raw_scenes = raw.get("scenes")
    if not isinstance(raw_scenes, dict):
        return _migrate_scenes_from_flat(raw)
    scenes: dict[str, dict[str, Any]] = {}
    for sk, sdef in DEFAULT_SCENES.items():
        src = raw_scenes.get(sk)
        scene = dict(sdef)
        if isinstance(src, dict):
            for fk, fv in src.items():
                if fk == "sub_accounts":
                    if (
                        isinstance(fv, list) and fv
                        and all(isinstance(x, str) and x.strip() for x in fv)
                    ):
                        scene["sub_accounts"] = [str(x).strip() for x in fv]
                elif fk in SCENE_FIELD_MAP:
                    if validate_exec_param(SCENE_FIELD_MAP[fk], fv) is None:
                        scene[fk] = fv
                elif fk in SCENE_THRESHOLD_FIELDS:
                    # S3b-2：场景级 TD 阈值，空/非法 → None（回退全局 td_params）
                    if fv in (None, ""):
                        scene[fk] = None
                    elif validate_exec_param(fk, fv) is None:
                        scene[fk] = int(fv)
                elif fk in SCENE_ONLY_FIELDS:
                    # 2026-08-25：场景专属参数（无扁平键）；空/非法 → 保持默认
                    if fv in (None, ""):
                        continue
                    if validate_exec_param(fk, fv) is None:
                        scene[fk] = fv
                elif fk == "sell_only_profit":
                    # 2026-08-29 迁移：旧单值高9盈利门 → sell_only_profit_high
                    # （low 用默认 0.002，动能判断自动随 momentum_exit 生效）
                    if fv not in (None, "") and validate_exec_param(
                        "sell_only_profit_high", fv
                    ) is None:
                        scene["sell_only_profit_high"] = fv
        scenes[sk] = scene
    return scenes


def _migrate_scenes_from_flat(raw: dict) -> dict[str, dict[str, Any]]:
    """旧扁平 exec_params.json → scenes（S1 迁移）。

    high = 当前扁平配置（td_live 继续消费扁平，high 与其同步）；
    mid/low = 默认停用。子账号池按当前 td_batches 截取默认池。
    """
    scenes = {sk: dict(sdef) for sk, sdef in DEFAULT_SCENES.items()}
    high = scenes["high"]
    for fk, pk in SCENE_FIELD_MAP.items():
        if pk in raw:
            high[fk] = raw[pk]
    n = int(high.get("batches", 4) or 1)
    high["sub_accounts"] = list(DEFAULT_SUB_ACCOUNTS[:n]) or ["gate_bot1"]
    scenes["high"] = high
    return scenes


def _sync_flat_from_high(merged: dict[str, Any]) -> None:
    """场景 → 扁平参数同步（S1：td_live 仍消费扁平）。

    td_enabled = 任一场景启用（2026-08-21 修复：此前绑定 high.enabled，
    导致只开 low/mid 时 TD 不启动）。其余字段仍从 high 同步（旧式扁平即
    high 场景的旧表示，execute_signal 直调兼容）。
    """
    scenes = merged.get("scenes")
    if not isinstance(scenes, dict):
        return
    merged["td_enabled"] = any(
        isinstance(s, dict) and bool(s.get("enabled")) for s in scenes.values()
    )
    high = scenes.get("high")
    if not isinstance(high, dict):
        return
    for fk, pk in SCENE_FIELD_MAP.items():
        if fk == "enabled":
            continue  # td_enabled 已按 any(scenes.*.enabled) 计算
        if fk in high:
            merged[pk] = high[fk]


def _apply_scenes_from_params(params: dict, merged: dict) -> dict | None:
    """校验并应用请求体中的 scenes；返回错误 dict 或 None。

    保存后 high 场景写回扁平参数（调用方再调 _sync_flat_from_high）。
    """
    src_scenes = params.get("scenes")
    if not isinstance(src_scenes, dict):
        return None  # 未提交 scenes → 保持现有
    merged["scenes"] = {sk: dict(sdef) for sk, sdef in DEFAULT_SCENES.items()}
    for sk in DEFAULT_SCENES:
        src = src_scenes.get(sk)
        if not isinstance(src, dict):
            continue
        scene = merged["scenes"][sk]
        for fk in scene:
            if fk == "sub_accounts":
                v = src.get("sub_accounts")
                if isinstance(v, list) and v and all(
                    isinstance(x, str) and x.strip() for x in v
                ):
                    scene["sub_accounts"] = [str(x).strip() for x in v]
                # 空/缺失 → 保持默认
                continue
            if fk in SCENE_THRESHOLD_FIELDS:
                # S3b-2：场景级 TD 阈值；空/缺失 → None（回退全局 td_params）
                v = src.get(fk)
                if v in (None, ""):
                    scene[fk] = None
                else:
                    err = validate_exec_param(fk, v)
                    if err is not None:
                        label = SCENES[sk]["label"]
                        return {"ok": False, "error": f"场景{label}.{fk}: {err}"}
                    scene[fk] = int(v)
                continue
            if fk in SCENE_ONLY_FIELDS:
                # 2026-08-25：场景专属参数（无扁平键）；空/缺失 → 默认
                v = src.get(fk)
                if v in (None, ""):
                    scene[fk] = DEFAULT_SCENES[sk].get(fk)
                    continue
                err = validate_exec_param(fk, v)
                if err is not None:
                    label = SCENES[sk]["label"]
                    return {"ok": False, "error": f"场景{label}.{fk}: {err}"}
                scene[fk] = v
                continue
            if fk not in src:
                continue
            err = validate_exec_param(SCENE_FIELD_MAP[fk], src[fk])
            if err is not None:
                label = SCENES[sk]["label"]
                return {"ok": False, "error": f"场景{label}.{fk}: {err}"}
            scene[fk] = src[fk]
    return None


def save_exec_params(params: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist the full parameter set.

    Returns dict with "ok" and optional "error".  ``params == {"reset":
    True}`` removes the file and returns defaults (WebUI 恢复默认 button).
    """
    merged = dict(DEFAULT_EXEC_PARAMS)
    # 深拷贝 scenes：浅拷贝下 merged["scenes"] 仍指向全局 DEFAULT_SCENES，
    # 旧式扁平调用（不带 scenes 键）就地写入会污染全局默认值（2026-08-20）。
    merged["scenes"] = {sk: dict(sdef) for sk, sdef in DEFAULT_SCENES.items()}
    if not isinstance(params, dict):
        return {"ok": False, "error": "请求体必须为 JSON 对象"}
    if params.get("reset") is True:
        path = exec_params_path()
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            return {"ok": False, "error": f"重置失败: {exc}"}
        return {"ok": True, "message": "已恢复默认执行参数", "params": dict(DEFAULT_EXEC_PARAMS)}
    # 兼容：扁平场景字段（td_enabled/td_symbols/... 旧调用方直接传）→ scenes.high。
    # 页面收集场景字段为 scenes.*（带前缀），此处仅兼容旧式扁平调用；
    # 扁平值总是覆盖 scenes.high 对应字段（旧式扁平即 high 场景的旧表示）。
    # 注意：不在 params["scenes"] 上就地写入（body 可能引用 DEFAULT_SCENES，
    # 直接写会污染全局默认值）——收集到独立 patch，应用时写到 merged。
    flat_scene_patch: dict[str, Any] = {}
    for fk, pk in SCENE_FIELD_MAP.items():
        if pk in params:
            flat_scene_patch[fk] = params[pk]
    # 迁移：保存时旧版大类值自动归一化为实例名（WebUI 保存即迁移，2026-08-17 方案 C）
    if "execution_channel" in params:
        params["execution_channel"] = normalize_execution_channel(params["execution_channel"])
    for key in merged:
        if key == "scenes":
            continue
        if key in params:
            err = validate_exec_param(key, params[key])
            if err is not None:
                label = PARAM_META.get(key, {}).get("label", key)
                return {"ok": False, "error": f"{label}: {err}"}
            merged[key] = params[key]

    # S1：scenes 应用（2026-08-20）——校验 + high 场景同步扁平
    scene_err = _apply_scenes_from_params(params, merged)
    if scene_err is not None:
        return scene_err
    # 扁平场景字段（旧式调用）覆盖 scenes.high（页面提交 scenes.* 时此 patch 为空）
    for fk, v in flat_scene_patch.items():
        err = validate_exec_param(SCENE_FIELD_MAP[fk], v)
        if err is not None:
            label = SCENES["high"]["label"]
            return {"ok": False, "error": f"场景{label}.{fk}: {err}"}
        merged["scenes"]["high"][fk] = v
    _sync_flat_from_high(merged)

    path = exec_params_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "error": f"写入失败: {exc}"}
    return {"ok": True, "params": merged}


# ── Internal helpers ──────────────────────────────────────────────────────

def _read_raw() -> dict | None:
    """Parse exec_params.json; None when missing or invalid JSON."""
    try:
        raw = json.loads(exec_params_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None
