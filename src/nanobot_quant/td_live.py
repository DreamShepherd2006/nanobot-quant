"""TD 自主 live 循环管理器（P2 B3）。

在 quant agent 进程内驻留 StrategyExecutor 主循环 daemon 线程：

- ``TdSequentialStrategy``（参数来自 exec_params.json：td_symbol /
  td_sleeptime / quantity_mode / td_quantity / 风控参数）
- ``OnchainOSBroker`` + ``OnchainOSDataSource``（B1 已修 live 兼容；
  data_source 挂到 broker，Strategy 数据访问统一走 broker.data_source）
- WebUI 开关 ``td_enabled`` 启停（exec_params.json）

生命周期语义：
- ``sync_from_params()``：WebUI 保存 / execute_signal 调用时同步——
  td_enabled=True 且未运行 → 启动；False 且运行中 → 停止；
  运行中但参数变化 → 重启（stop 旧循环 → start 新参数）。
- ``status()``：当前循环状态（WebUI 展示）。

安全说明：
- StrategyExecutor 是 daemon Thread（lumibot 自带 stop_event），
  stop() 设置事件后主循环 ``while ... and self.should_continue`` 退出。
- 单例：同一时间只有一个 TD live 循环；旧线程未退出时 start() 拒绝
  重复启动（返回 running 状态），避免双循环重复下单。
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Any

# 2026-08-21：TD live 在 gatekeeper 进程内构造 lumibot 对象（Strategy/
# StrategyExecutor）。lumibot import 时会注册 telemetry 线程（每 ~5min 向
# stdout 打健康快照 JSON）并刷"Bot is running"等 INFO 日志——必须在此
# 进程的首次 lumibot import 之前关闭遥测。
# setdefault 在进程 env 已有 LUMIBOT_TELEMETRY 值时失效（HF Space 实测
# 05:52 仍有 LUMIBOT_TELEMETRY 行），改为强制赋值：TD 循环场景遥测只有
# stdout 噪音，无价值。
os.environ["LUMIBOT_TELEMETRY"] = "0"
os.environ.setdefault("BACKTESTING_QUIET_LOGS", "true")

_lock = threading.Lock()
_runner: "_TdLiveRunner | None" = None


def _dust_threshold() -> float:
    """对账导入 dust 阈值（USD）：链上持仓价值低于该值不导入。

    2026-08-11：CRCLX A2 的 $0.13 卖出尾仓（dust）被对账当持仓导入，
    锁住该槽位的 USDC（6.52）导致买9 无资金可用——dust 不占槽位。
    0=关闭（旧行为：任何正持仓都导入）。
    """
    try:
        from nanobot_quant.exec_params import load_exec_params

        return float((load_exec_params() or {}).get("min_position_value") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


class _TdLiveRunner:
    def __init__(self) -> None:
        self._executor: Any = None
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "running": False,
            "started_at": None,
            "last_error": None,
            "symbols": None,
            "sleeptime": None,
            "quantity_mode": None,
        }

    # ── 构造 ──────────────────────────────────────────────────────────
    def _build_executor(self, params: dict[str, Any]) -> Any:
        """构造 StrategyExecutor（lumibot 真包延迟导入，测试容器无 lumibot）。

        S3a 场景调度（2026-08-20）：按 exec_params.scenes 启用的场景构造
        运行时——每场景独立 broker（含独立数据源实例 → KlineCache per-scene
        天然隔离）+ 独立批次台账（场景 batches/sub_accounts → slot 映射）；
        主循环心跳 = 最小场景周期，策略 on_trading_iteration 内按场景到期
        调度（单线程，无并发风险）。仅 high 启用时行为与旧扁平参数一致
        （high 与扁平同步）。
        """
        from lumibot.strategies.strategy_executor import StrategyExecutor

        from nanobot_quant.strategies.registry import load_selected
        from nanobot_quant.strategies.td_sequential_strategy import (
            TdSequentialStrategy,
        )
        from nanobot_quant.td_params import load_td_params
        from nanobot_quant.tokens_store import load_tokens_json

        tokens = load_tokens_json() or []
        # 执行通道（2026-08-14，P2；方案 C 后为实例名）：gate=Gate.io 交易所；
        # okx_dex=链上 DEX（默认，OnchainOS 子钱包）。只影响之后的新下单，不迁移持仓。
        channel = str(params.get("execution_channel", "okx_dex"))
        # 统一 broker 构造：broker 注册表（第十九章，2026-08-17）——
        # 通道值=spec 实例名（gate/okx_dex），旧值 dex/cex 自动归一化，未知通道
        # fail-closed（KeyError），绝不静默回退到别所下单。
        from nanobot_quant.brokers.registry import (
            broker_for_channel,
            spec_for_channel,
        )

        # 通道大类（family）：从 broker spec 解析（gate→cex、okx_dex→dex），
        # 不可直接用 execution_channel 实例名判断（2026-08-17 修复）。
        family = spec_for_channel(channel).family
        strategy_name = load_selected()
        td_params = load_td_params(strategy_name)

        # ── S3a 场景运行时（2026-08-20）────────────────────────────────
        scenes = params.get("scenes")
        if not scenes:
            # 旧扁平调用兜底（测试/execute_signal 直调）：合成 high 场景，
            # 与 exec_params 的扁平→scenes.high 迁移语义一致。
            scenes = {
                "high": {
                    "enabled": True,
                    "sleeptime": str(params.get("td_sleeptime", "1D")),
                    "symbols": list(params.get("td_symbols") or []),
                    "quantity_mode": str(params.get("quantity_mode", "fixed")),
                    "td_quantity": params.get("td_quantity", 10),
                    "td_fixed_amount": float(
                        params.get("td_fixed_amount", 10.0) or 10.0
                    ),
                    "batches": int(params.get("td_batches", 1) or 1),
                    "sub_accounts": [],
                    "exit_order": str(params.get("exit_order", "fifo")),
                    "stop_loss_pct": float(
                        params.get("stop_loss_pct", 0.10) or 0.10
                    ),
                    "take_profit_pct": float(
                        params.get("take_profit_pct", 0.0) or 0.0
                    ),
                    "td_start_slot": int(params.get("td_start_slot", 1) or 1),
                    "min_account_value": float(
                        params.get("min_account_value", 0) or 0
                    ),
                }
            }
        enabled = {
            k: dict(v)
            for k, v in scenes.items()
            if isinstance(v, dict) and v.get("enabled")
        }
        if not enabled:
            raise ValueError("没有启用的场景（scenes.*.enabled 全为 false）")
        # 统一心跳 = 最小场景周期（策略内按场景到期调度）
        heartbeat_str = min(
            (str(sc.get("sleeptime") or "1m") for sc in enabled.values()),
            key=lambda s: _parse_sleeptime_seconds(s),
        )

        # 主 broker：构造策略必需（lumibot Strategy.__init__ 在 broker=None
        # 时直接 raise "No broker is set"，必须构造时传入 broker + data_source）。
        # 实际每轮由 _activate_scene 切换到场景 broker。
        broker = broker_for_channel(
            channel,
            tokens_json=tokens,
            slippage=str(params["slippage"]),
            sol_buffer_pct=float(params["sol_buffer_pct"]),
        )
        strategy = TdSequentialStrategy(
            broker=broker,
            data_source=broker.data_source,
        )
        # 基础参数（场景激活时由 _activate_scene 覆盖场景字段）
        strategy.parameters = dict(
            TdSequentialStrategy.parameters,
            **{
                "symbols": next(iter(enabled.values())).get("symbols") or [],
                "quantity": 10,
                "quantity_mode": "fixed",
                "td_fixed_amount": 10.0,
                # 固定 K 线窗口（方案 B）：每轮拉最近 N 根，不累积增长
                "sleeptime": heartbeat_str,  # 统一心跳 = 最小场景周期
                "max_position_pct": params["max_position_pct"],
                "max_drawdown_pct": params["max_drawdown_pct"],
                "stop_loss_pct": params["stop_loss_pct"],
                "exit_order": "fifo",
                "take_profit_pct": 0.0,
                "td_start_slot": 1,
                "min_account_value": 0,
                "min_history": int(params.get("td_bars", 120) or 120),
                "tokens_json": tokens,
                "live_mode": True,  # 2026-08-11：TD live 模式写信号事件文件
                "strategy_variant": strategy_name,
                # 2026-08-17 Step 0：通道大类（dex/cex）——批次逻辑分叉依据；
                # 取 broker spec.family（gate→cex、okx_dex→dex）。
                "channel_family": family,
                # 2026-08-17 A 修复第二部分：数据源契约（是否已过滤进行中 bar）。
                # CexDataSource（gate_cex，rows_to_df 已过滤 closed=false）=True；
                # OnchainOSDataSource（DEX，含进行中 bar）无该属性=False。
                # 同一通道契约相同，取主 broker 契约即可。
                "drops_in_progress_bars": bool(
                    getattr(
                        getattr(broker, "data_source", None),
                        "drops_in_progress_bars",
                        False,
                    )
                ),
            },
            **td_params,
        )
        # DIAG（2026-08-12）：验证方案 A 参数 merge——strategy.json 变体 +
        # td-params 阈值对 TD 自主循环生效（重启后首次构造时打印一次）。
        print(
            f"[DIAG] td_live 参数: strategy={strategy_name} "
            f"entry_setup={strategy.parameters.get('entry_setup')} "
            f"exit_setup={strategy.parameters.get('exit_setup')} "
            f"setup_period={strategy.parameters.get('setup_period')} "
            f"compare_length={strategy.parameters.get('compare_length')}",
            file=sys.stderr, flush=True,
        )
        # 2026-08-19：记录运行中实际变体到 LIVE_STATE
        try:
            from nanobot_quant.td_live_state import set_strategy
            set_strategy(strategy_name)
        except Exception:  # noqa: BLE001
            pass

        # 每场景独立 broker（独立数据源 → KlineCache per-scene 隔离）+
        # 独立批次台账（场景 batches/sub_accounts → slot 映射）。
        scene_runtimes: dict[str, dict[str, Any]] = {}
        for name, sc in enabled.items():
            sc_broker = broker_for_channel(
                channel,
                tokens_json=tokens,
                slippage=str(params["slippage"]),
                sol_buffer_pct=float(params["sol_buffer_pct"]),
            )
            bms: dict[str, Any] = {}
            scene_ok = True
            try:
                bms = self._prepare_scene_batches(
                    sc, list(sc.get("symbols") or []), channel, name
                )
            except Exception as exc:  # noqa: BLE001
                scene_ok = False
                print(
                    f"[DIAG] td_live 场景 {name} 批次准备失败: {exc}"
                    f"——场景跳过（fail-closed）",
                    file=sys.stderr, flush=True,
                )
            scene_runtimes[name] = {
                "enabled": scene_ok,
                "sleeptime": str(sc.get("sleeptime") or "1m"),
                "params": sc,
                "broker": sc_broker,
                "batch_managers": bms,
                "last_run": None,
                "_diag_shown": False,
            }
        strategy._scene_runtimes = scene_runtimes
        print(
            "[DIAG] td_live 场景: "
            + "; ".join(
                f"{k}[{rt['sleeptime']}, "
                f"{len(rt['params'].get('symbols') or [])}标的, "
                f"{len(rt['batch_managers'])}台账, "
                f"mode={rt['params'].get('quantity_mode')}]"
                for k, rt in scene_runtimes.items()
            )
            + f"（heartbeat={heartbeat_str}）",
            file=sys.stderr, flush=True,
        )
        self._strategy = strategy
        executor = StrategyExecutor(strategy)
        executor.daemon = True
        strategy._executor = executor
        return executor

    def _prepare_scene_batches(
        self, sc: dict, symbols: list[str], channel: str, scene_name: str
    ) -> dict[str, Any]:
        """S3a（2026-08-20）：场景批次台账。

        场景 batches >1 时按场景 sub_accounts 作为 slot 账户池
        （slot i ↔ sub_accounts[i-1]）；sub_accounts 缺失时回退现有
        slot_map/前 N 账户逻辑（与旧扁平行为一致）。
        S3b（2026-08-20 拍板）：台账场景化——
        ``batches.{channel}.{scene}.{symbol}.json``（同标的跨场景
        互不复用）；sub_accounts 长度 < batches 时 fail-closed
        （raise，场景跳过，不静默截断）。
        """
        batches_n = int(sc.get("batches", 1) or 1)
        if batches_n <= 1:
            return {}
        account_ids = sc.get("sub_accounts") or None
        if account_ids and len(account_ids) < batches_n:
            raise ValueError(
                f"场景 {scene_name} 子账号池不足: "
                f"sub_accounts={len(account_ids)} < batches={batches_n}"
            )
        bms: dict[str, Any] = {}
        for sym in symbols:
            bm = self._prepare_batches(
                batches_n, sym, channel, account_ids=account_ids,
                scene=scene_name,
            )
            if bm is not None:
                bms[sym] = bm
        return bms

    def _reconcile_import(
        self, bm: Any, symbol: str, tokens_json: list[dict] | None
    ) -> None:
        """启动对账：链上天然持仓导入台账（2026-08-10 拍板设计）。

        - 按账户对账（slot↔账户固定映射）：遍历 available slot，switch 到
          该账户查该标的实际余额；
        - 导入量 = max(0, 余额 − min_hold)（每账户保留量，SOL 用作 gas 底线）；
        - entry_price = cost_price（WebUI 设置）或对账时当前价兜底；
        - 已 open 的 slot 跳过（TD 自己开过的仓不重复导入）；
        - 结束后还原活跃账户，输出对账报告（DIAG）。
        """
        import sys
        import time as _t

        from nanobot_quant.onchainos_cli import get_token_assets, get_token_price
        from nanobot_quant.tokens_store import token_meta
        from nanobot_quant.tools.tools_wallet import (
            wallet_balance, wallet_status, wallet_switch,
        )

        meta = token_meta(symbol, tokens_json)
        address = str(meta.get("address") or "")
        chain = str(meta.get("chain") or "solana")
        min_hold = float(meta.get("min_hold") or 0.0)
        cost = meta.get("cost_price")
        reports: list[str] = []
        min_pos_value = _dust_threshold()
        # 记录当前活跃账户，对账结束后还原（wallet switch 是全局状态）
        home = None
        try:
            st = wallet_status() or {}
            home = (st.get("data") or {}).get("currentAccountId") or None
        except Exception:  # noqa: BLE001
            home = None
        imported_any = False
        for slot in bm.slots:
            if slot.get("status") != "available":
                continue  # 已 open：TD 自己开的仓，天然持仓不可重复导入
            aid = slot.get("account_id")
            if not aid:
                continue
            try:
                wallet_switch(aid)
            except Exception as exc:  # noqa: BLE001
                reports.append(f"{symbol} 账户{aid[:8]} switch 失败: {exc}")
                continue
            try:
                r = wallet_balance() or {}
            except Exception as exc:  # noqa: BLE001
                reports.append(f"{symbol} 账户{aid[:8]} 余额查询失败: {exc}")
                continue
            bal = 0.0
            tok_price: float | None = None
            for a in get_token_assets(r.get("data") or {}):
                addr = str(
                    a.get("tokenAddress") or a.get("token_address") or ""
                )
                sym = str(a.get("symbol", "")).upper()
                hit = False
                if address and addr and addr.lower() == address.lower():
                    hit = True
                elif sym == symbol.upper():
                    hit = True
                if hit:
                    bal = float(a.get("balance") or 0)
                    try:
                        raw_px = a.get("tokenPrice") or a.get("token_price")
                        tok_price = (
                            float(raw_px) if raw_px not in (None, "") else None
                        )
                    except (TypeError, ValueError):
                        tok_price = None
                    break
            qty = max(0.0, bal - min_hold)
            if qty <= 0:
                continue  # 纯保留量（如 SOL 每账户 0.01 gas）→ 不动
            price = float(cost) if cost else 0.0
            note = "成本价"
            if price <= 0 and tok_price and tok_price > 0:
                price = tok_price
                note = "余额价"  # wallet balance 自带价格，零额外 CLI 调用
            if price <= 0:
                try:
                    price = float(
                        get_token_price(symbol, tokens_json=tokens_json,
                                        chain=chain)
                        or 0
                    )
                    note = "对账价"
                except Exception:  # noqa: BLE001
                    price = 0.0
            if price <= 0:
                reports.append(
                    f"{symbol} 账户{aid[:8]} 链上 {bal}（保留 {min_hold}）"
                    f"→ 无价格，跳过导入"
                )
                continue
            # dust 阈值（2026-08-11）：链上残留价值 < min_position_value
            # 视为 dust 不导入，slot 保持可建仓——否则 $0.13 的卖出尾仓会
            # 锁住整个槽位的 USDC，导致买9 无资金可用。
            if min_pos_value > 0 and qty * price < min_pos_value:
                reports.append(
                    f"{symbol} 账户{aid[:8]} 链上 {bal}（保留 {min_hold}）→ "
                    f"dust ${qty * price:.2f} < ${min_pos_value:g}，跳过导入"
                    f"（slot 保持可建仓）"
                )
                continue
            bm.open_lot(
                qty=qty,
                entry_price=price,
                entry_time=_t.strftime("%Y-%m-%dT%H:%M:%S"),
                slot=slot["slot"],
            )
            imported_any = True
            reports.append(
                f"{symbol} 账户{aid[:8]} 链上 {bal}（保留 {min_hold}）→ "
                f"导入 slot {slot['slot']}（{note} {price:.4g}）"
            )
        if home:
            try:
                wallet_switch(home)
            except Exception:  # noqa: BLE001
                pass
        if imported_any:
            bm.save()
        if reports:
            for r in reports:
                print(f"[DIAG] td_live 对账: {r}", file=sys.stderr, flush=True)
        else:
            print(
                f"[DIAG] td_live 对账: {symbol} 无天然持仓（或全部保留）",
                file=sys.stderr, flush=True,
            )

    def _reconcile_import_cex(
        self, bm: Any, symbol: str, tokens_json: list[dict] | None
    ) -> None:
        """启动对账：CEX 子账号天然持仓导入台账（Step 2，2026-08-18 拍板）。

        - 数据源：主 key `/wallet/sub_account_balances` 一次拉全部子账号，
          按 slot 的 account_id（gate_botN）→ UID 匹配（无状态、无需还原）；
        - 无 gas/min_hold（CEX 无链上 gas），导入量 = 子账号可用余额；
        - 阈值：价值 < Gate min_quote（交易对规则动态拉取，兜底 $3）不导入
          ——CEX 卖出受 min_quote 硬限，导入死 lot 会卡 slot（P2-A）；
        - entry_price = cost_price（WebUI 设置）→ gate ticker 对账时市价
          兜底（P3-A）；
        - 已 open 的 slot 跳过（幂等，重启不重复导入）；主账号不是 slot
          载体，不导入。
        """
        import sys
        import time as _t

        from nanobot_quant.data_sources.base import get_data_source
        from nanobot_quant.gate_credentials import (
            gate_pair,
            load_gate_credentials,
            load_slot_map,
        )
        from nanobot_quant.gate_sdk import get_currency_pair, sub_account_balances
        from nanobot_quant.tokens_store import token_meta

        creds = load_gate_credentials()
        if not creds:
            print(
                f"[DIAG] td_live 对账: {symbol} 无 gate 凭证，跳过",
                file=sys.stderr, flush=True,
            )
            return
        main = creds.get("main") or {}
        api_key = str(main.get("api_key") or "")
        api_secret = str(main.get("api_secret") or "")
        if not api_key or not api_secret:
            print(
                f"[DIAG] td_live 对账: {symbol} 主 key 缺失，跳过",
                file=sys.stderr, flush=True,
            )
            return
        meta = token_meta(symbol, tokens_json)
        cost = meta.get("cost_price")
        slot_map = load_slot_map(creds) or {}
        subs = creds.get("sub_accounts") or {}
        # 子账号名称 → UID（slot.account_id 存的是名称，如 gate_bot1）
        name_to_uid = {
            str(name): str(v.get("uid") or "")
            for name, v in subs.items()
            if isinstance(v, dict) and v.get("uid")
        }
        if not name_to_uid:
            print(
                f"[DIAG] td_live 对账: {symbol} gate.json sub_accounts 无 UID 映射"
                "（凭证页子账号 UID 未配置？）——跳过导入",
                file=sys.stderr, flush=True,
            )
        # P2-A 阈值：交易对 min_quote 动态拉取（与买卖预检同源），失败兜底 $3
        pair = gate_pair(symbol, tokens_json)
        min_quote = 3.0
        try:
            pair_meta = get_currency_pair(api_key, api_secret, pair)
            if isinstance(pair_meta, dict):
                min_quote = float(pair_meta.get("min_quote_amount") or 3.0)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[DIAG] td_live 对账: {symbol} pair meta 失败 {exc}，"
                f"用默认 min_quote=${min_quote:g}",
                file=sys.stderr, flush=True,
            )
        try:
            rows = sub_account_balances(api_key, api_secret) or []
        except Exception as exc:  # noqa: BLE001
            print(
                f"[DIAG] td_live 对账: {symbol} 子账号余额查询失败: {exc}",
                file=sys.stderr, flush=True,
            )
            return
        by_uid: dict[str, dict] = {}
        for r in rows:
            uid = str(r.get("uid") or "")
            if uid:
                by_uid[uid] = dict(r.get("available") or {})
        if not by_uid:
            print(
                f"[DIAG] td_live 对账: {symbol} 子账号余额为空"
                "（主 key 是否开通「子账号」权限？）——跳过导入",
                file=sys.stderr, flush=True,
            )
        # 币种匹配：Gate available 键 = 基础币大写（如 "CRCLX"），
        # 不能用 gate_symbol 原值——它可能是完整 pair（"CRCLX_USDT"）。
        # 从 gate_pair 剥离 quote，与下单/取价同源。
        bal_key = gate_pair(symbol, tokens_json).split("_")[0]
        reports: list[str] = []
        imported_any = False
        px: float | None = None
        for slot in bm.slots:
            if slot.get("status") != "available":
                continue  # 已 open：TD 自己开的仓，天然持仓不可重复导入
            name = str(slot.get("account_id") or "")
            # 名称 → UID；兜底：slot_map 若直接存 UID 也可用
            uid = name_to_uid.get(name) or str(slot_map.get(str(slot["slot"])) or "")
            if not uid or uid not in by_uid:
                continue  # 未配置/无余额记录的子账号跳过
            bal = float(by_uid[uid].get(bal_key) or 0.0)
            if bal <= 0:
                continue
            # 阈值判断与定价：gate ticker（与 CexBroker._price_of 同源）
            if px is None:
                try:
                    px = float(
                        get_data_source("gate_cex").get_price(symbol) or 0.0
                    )
                except Exception:  # noqa: BLE001
                    px = 0.0
            if px <= 0:
                reports.append(
                    f"{symbol} 账户{name} 取价失败，跳过导入（无法判阈值/定价）"
                )
                continue
            if bal * px < min_quote:
                reports.append(
                    f"{symbol} 账户{name} dust ${bal * px:.2f} < "
                    f"${min_quote:g}（min_quote），跳过导入"
                )
                continue
            if cost:
                price = float(cost)
                note = "cost_price"
            else:
                price = px
                note = "gate ticker"
            bm.open_lot(
                qty=bal,
                entry_price=price,
                entry_time=_t.strftime("%Y-%m-%dT%H:%M:%S"),
                slot=slot["slot"],
            )
            imported_any = True
            reports.append(
                f"{symbol} 账户{name}(uid {uid}) 子账号 {bal} → "
                f"导入 slot {slot['slot']}（{note} {price:.4g}）"
            )
        if imported_any:
            bm.save()
        if reports:
            for r in reports:
                print(f"[DIAG] td_live 对账: {r}", file=sys.stderr, flush=True)
        else:
            print(
                f"[DIAG] td_live 对账: {symbol} 无天然持仓（或全部低于阈值）",
                file=sys.stderr, flush=True,
            )

    def _prepare_all_batches(
        self, td_batches: int, symbols: list[str], channel: str = "dex"
    ) -> dict[str, Any]:
        """为标的池中每个标的准备独立 BatchManager（per-symbol 台账）。

        返回 {symbol: BatchManager}；某标的失败（钱包不可用等）时跳过
        （该标的退回单仓模式判定——无 batch_manager 即 batch_mode=False）。
        """
        managers: dict[str, Any] = {}
        for sym in symbols:
            bm = self._prepare_batches(td_batches, sym, channel)
            if bm is not None:
                managers[sym] = bm
        return managers

    def _prepare_batches(
        self,
        td_batches: int,
        symbol: str,
        channel: str = "okx_dex",
        account_ids: list[str] | None = None,
        scene: str | None = None,
    ) -> Any:
        """加载/创建批次台账（通道隔离，2026-08-17 拍板）。

        - 文件按通道独立：``batches.{channel}.{symbol}.json``——DEX 与
          CEX 台账互不复用（此前同路径切换双向覆盖/快照堆积，导致
          DEX lot 被 CEX 通道误卖/误释放）。
        - S3b（2026-08-20 拍板）：scene 时按场景文件
          ``batches.{channel}.{scene}.{symbol}.json``——同标的跨场景
          互不复用；旧无场景文件由 _load_or_migrate 按账号池匹配
          .bak 快照后迁移（幂等不覆盖）。
        - dex（okx_dex）：子钱包映射（wallet_accounts 前 N 个 account_id）
        - cex（gate）：子账号映射（load_slot_map：slot→gate_botN；不足按
          1..N 兜底 fallback）——不创建子钱包（子账号已存在于交易所）
        - 旧格式（无通道前缀）台账由 _load_or_migrate 自动归 okx_dex 命名空间
        - S3a（2026-08-20）：传入 account_ids（场景 sub_accounts）时优先使用
          ——slot i ↔ sub_accounts[i-1]；缺失时回退上述默认映射。
        """
        import sys

        from nanobot_quant.batches import BatchManager, _load_or_migrate
        from nanobot_quant.exec_params import normalize_execution_channel
        from nanobot_quant.brokers.registry import spec_for_channel

        channel = normalize_execution_channel(channel)
        family = spec_for_channel(channel).family  # gate→cex、okx_dex→dex

        bm = _load_or_migrate(symbol, channel, scene=scene, account_ids=account_ids)
        if bm is not None and bm.symbol == symbol and bm.slots:
            if family == "cex" and not all(
                re.match(r"^gate_bot\d+$", str(s.get("account_id") or ""))
                for s in bm.slots
            ):
                # 自愈：CEX 通道台账混入 DEX 子钱包 UUID（历史迁移残留，
                # 2026-08-18 实证 batches.gate.CRCLX.json 为 UUID）——
                # account_id 匹配不上 gate.json sub_accounts，对账/下单
                # 永远静默跳过。全部 available 时快照后重建 gate_botN；
                # 有 open lot 时 fail-closed 等人工处理（不可丢台账）。
                opens = [
                    s["slot"]
                    for s in bm.slots
                    if s.get("status") != "available"
                ]
                if opens:
                    print(
                        f"[DIAG] td_live: {symbol} gate 台账含 DEX UUID 且 "
                        f"slot {opens} 有 open lot——拒绝重建，请人工处理",
                        file=sys.stderr, flush=True,
                    )
                    return bm
                ts = time.strftime("%Y%m%d%H%M%S")
                src = bm.path
                bak = src.with_name(f"{src.name}.dex-uuid.bak.{ts}")
                os.replace(src, bak)
                print(
                    f"[DIAG] td_live: {symbol} gate 台账为 DEX UUID 残留"
                    f"（全部 available），快照 {bak.name} 后重建",
                    file=sys.stderr, flush=True,
                )
                bm = None  # 走下方按通道创建流程
            else:
                # S3a 场景池子变更检测：场景 sub_accounts 与台账 slot 账号
                # 不一致时（无 open lot）快照后按新池子重建；有 open lot
                # fail-closed（不可丢台账，与 UUID 自愈同模式）。
                cur_ids = [
                    str(s.get("account_id") or "") for s in bm.slots
                ]
                want_ids = (
                    [str(a) for a in account_ids][:td_batches]
                    if account_ids else None
                )
                if want_ids is not None and cur_ids != want_ids:
                    opens = [
                        s["slot"]
                        for s in bm.slots
                        if s.get("status") != "available"
                    ]
                    if opens:
                        print(
                            f"[DIAG] td_live: {symbol} 场景池子变更"
                            f"（{cur_ids} → {want_ids}）且 slot {opens}"
                            f" 有 open lot——拒绝重建，请人工处理",
                            file=sys.stderr, flush=True,
                        )
                        return bm
                    ts = time.strftime("%Y%m%d%H%M%S")
                    src = bm.path
                    bak = src.with_name(f"{src.name}.scene.bak.{ts}")
                    os.replace(src, bak)
                    print(
                        f"[DIAG] td_live: {symbol} 场景池子变更"
                        f"（{cur_ids} → {want_ids}，全部 available），"
                        f"快照 {bak.name} 后重建",
                        file=sys.stderr, flush=True,
                    )
                    bm = None  # 走下方按新池子创建流程
                else:
                    print(
                        f"[DIAG] td_live: batches restored ({symbol}, "
                        f"{len(bm.slots)} slots, {channel}"
                        + (f", scene={scene}" if scene else ""),
                        file=sys.stderr, flush=True,
                    )
                    return bm
        if family == "cex":
            from nanobot_quant.gate_credentials import load_slot_map

            if account_ids:
                ids = [str(a) for a in account_ids][:td_batches]
            else:
                slot_map = load_slot_map() or {}
                ids = [
                    str(slot_map.get(str(i)) or f"gate_bot{i}")
                    for i in range(1, td_batches + 1)
                ]
            if not ids:
                print(
                    "[DIAG] td_live: slot_map 为空",
                    file=sys.stderr, flush=True,
                )
                return None
        else:
            from nanobot_quant.tools.tools_wallet import wallet_accounts

            if account_ids:
                ids = [str(a) for a in account_ids][:td_batches]
            else:
                try:
                    acc = wallet_accounts()
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[DIAG] td_live: wallet_accounts failed: {exc}",
                        file=sys.stderr, flush=True,
                    )
                    return None
                if acc.get("status") != "ok":
                    print(
                        f"[DIAG] td_live: wallet_accounts error: "
                        f"{acc.get('error')}",
                        file=sys.stderr, flush=True,
                    )
                    return None
                ids = [
                    a["account_id"]
                    for a in acc.get("data", {}).get("accounts", [])[:td_batches]
                ]
                if not ids:
                    print(
                        "[DIAG] td_live: no sub-accounts in wallets.json",
                        file=sys.stderr, flush=True,
                    )
                    return None
                if len(ids) < td_batches:
                    print(
                        f"[DIAG] td_live: only {len(ids)} sub-accounts, "
                        f"requested {td_batches} — 请先在 WebUI 批次设置中创建",
                        file=sys.stderr, flush=True,
                    )
        bm = BatchManager(
            symbol=symbol, account_ids=ids, channel=channel, scene=scene
        )
        bm.save()
        print(
            f"[DIAG] td_live: batches created ({symbol}, "
            f"{len(ids)} slots, {channel}"
            + (f", scene={scene}" if scene else ""),
            file=sys.stderr, flush=True,
        )
        return bm

    # ── 生命周期 ──────────────────────────────────────────────────────
    def start(self, params: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            if self._thread is not None and self._thread.is_alive():
                # 已运行 → 返回当前状态（参数变更由 sync_from_params 先 stop）。
                # 若线程处于收尾中（stop 后未退出），等待其退出再启动。
                if not self._wait_thread_exit():
                    self._state["last_error"] = (
                        "旧 TD 循环线程未在超时内退出，拒绝启动新循环"
                        "（避免双循环重复下单）"
                    )
                    self._state["running"] = False
                    print(
                        "[DIAG] td_live: old thread did not exit within "
                        "timeout — refusing to start new loop",
                        file=sys.stderr, flush=True,
                    )
                    return self.status()
                # 等待成功后继续向下启动新循环
            # 新循环启动前清空 CEX 黑名单——用户处理完下架/无交易对币后
            # 重启 TD 循环即重新探测（blacklist 见 gate_cex_data.py）
            try:
                from nanobot_quant.gate_cex_data import clear_blacklist
                clear_blacklist()
            except Exception:  # pragma: no cover
                pass
            try:
                executor = self._build_executor(params)
            except Exception as exc:  # pragma: no cover — lumibot 真包异常
                self._state["last_error"] = str(exc)
                self._state["running"] = False
                print(
                    f"[DIAG] td_live: build executor failed: {exc}",
                    file=sys.stderr, flush=True,
                )
                return self.status()
            self._executor = executor
            # 新循环启动前复位停止标志（延迟停止方案：stop 时置位，
            # 防主循环重建 scheduler 的孤儿 job 空跑；新循环必须清除）
            try:
                from nanobot_quant import td_live_state

                td_live_state.stop_requested.clear()
            except Exception:  # pragma: no cover
                pass
            t = threading.Thread(target=self._run, daemon=True, name="td-live")
            self._thread = t
            t.start()
            self._state.update(
                running=True,
                started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                last_error=None,
                symbols=params["td_symbols"],
                sleeptime=params["td_sleeptime"],
                quantity_mode=params["quantity_mode"],
                scenes_fp=_scenes_fingerprint(params),
            )
            print(
                f"[DIAG] td_live: StrategyExecutor started "
                f"(scenes={sorted(k for k, v in (params.get('scenes') or {}).items() if isinstance(v, dict) and v.get('enabled'))}, "
                f"mode={params['quantity_mode']})",
                file=sys.stderr, flush=True,
            )
            return self.status()

    def _run(self) -> None:
        try:
            # lumibot 运行时日志输出到 stdout 会污染 MCP stdio JSON-RPC 通道，
            # 全程重定向 stdout → stderr（P1 已验证方案）。
            _saved_stdout = sys.stdout
            sys.stdout = sys.stderr
            try:
                # 启动对账：链上天然持仓导入各标的台账（min_hold 扣减）
                self._reconcile_all()
                try:
                    from nanobot_quant import td_live_state
                    td_live_state.set_loop(True)
                except Exception:  # noqa: BLE001
                    pass
                self._executor.run()  # Thread.run → StrategyExecutor 主循环
            finally:
                sys.stdout = _saved_stdout
        except Exception as exc:  # pragma: no cover — lumibot 真包异常
            self._state["last_error"] = str(exc)
            self._state["running"] = False
            print(
                f"[DIAG] td_live: executor stopped with error: {exc}",
                file=sys.stderr, flush=True,
            )
        finally:
            try:
                from nanobot_quant import td_live_state
                td_live_state.set_loop(False)
            except Exception:  # noqa: BLE001
                pass

    def _reconcile_all(self) -> None:
        """对全部注入 batch_manager 的标的做启动对账（天然持仓导入）。

        Step 1（2026-08-17）：CEX 通道跳过——子账号持仓对账导入在
        Step 2 实现（对标 DEX _reconcile_import，按子账号余额导入）。
        S3a（2026-08-20）：遍历所有启用场景的 batch_managers（场景隔离）。
        """
        try:
            strategy = getattr(self, "_strategy", None)
            if strategy is None:
                return
            managers: dict[str, Any] = {}
            runtimes = getattr(strategy, "_scene_runtimes", None)
            if runtimes:
                for name, rt in runtimes.items():
                    if not rt.get("enabled"):
                        continue
                    managers.update(rt.get("batch_managers") or {})
            else:
                managers = getattr(strategy, "batch_managers", None) or {}
            tokens_json = getattr(strategy, "tokens_json", None)
            if (strategy.parameters or {}).get("channel_family") == "cex":
                importer = self._reconcile_import_cex
            else:
                importer = self._reconcile_import
            for sym, bm in managers.items():
                try:
                    importer(bm, sym, tokens_json)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[DIAG] td_live 对账: {sym} 失败: {exc}",
                        file=sys.stderr, flush=True,
                    )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[DIAG] td_live 对账: 未执行（{exc}）",
                file=sys.stderr, flush=True,
            )

    def stop(self) -> dict[str, Any]:
        executor = None
        with _lock:
            executor = self._executor
            self._state["running"] = False
        if executor is not None:
            self._stop_executor_graceful(executor)
        print(
            "[DIAG] td_live: 停止已请求（当前轮自然结束后生效，页面不受影响）",
            file=sys.stderr, flush=True,
        )
        return self.status()

    def _stop_executor_graceful(self, executor: Any) -> None:
        """延迟停止（2026-08-21 方案，用户拍板）。

        用户原则：不在业务进行中强行中断——业务轮可能正涉及订单处理，
        强行中断风险大。停止 = 等当前轮业务自然结束后再停。

        旧实现（_stop_executor_safe）调 executor.stop() → gracefully_exit
        → APScheduler shutdown(wait=True) 在 job 卡于网络调用时无限等待，
        曾致线程残留（thread_alive=True）与空间卡死。新实现：
          ① 轮询策略 _iteration_active == False（当前轮跑完，90s 兜底）
          ② 设 td_live_state.stop_requested + executor.stop_event
             ——主循环 _should_continue_trading_loop 感知 stop_event →
             break → 线程退出；stop_requested 供策略孤儿 job 守卫
          ③ 清理 scheduler（remove_all_jobs + shutdown(wait=False) +
             scheduler=None）——防主循环 break 前重建的 scheduler 在
             线程退出后继续调度孤儿 job
        不调 executor.stop()/gracefully_exit（避开 shutdown(wait=True)）。
        """
        def _do() -> None:
            from nanobot_quant import td_live_state

            strategy = getattr(self, "_strategy", None)
            # ① 等当前轮业务自然结束
            deadline = time.monotonic() + 90.0
            while time.monotonic() < deadline:
                if strategy is None or not getattr(
                    strategy, "_iteration_active", False
                ):
                    break
                time.sleep(0.2)
            # ② 停止信号（主循环 1s 内 break；策略孤儿 job 守卫）
            try:
                td_live_state.stop_requested.set()
                executor.stop_event.set()
            except Exception as exc:  # pragma: no cover
                print(
                    f"[DIAG] td_live: 停止信号设置异常: {exc}",
                    file=sys.stderr, flush=True,
                )
            # ③ 清理 scheduler（防孤儿 job；wait=False 秒回）
            try:
                sched = getattr(executor, "scheduler", None)
                if sched is not None:
                    sched.remove_all_jobs()
                    sched.shutdown(wait=False)
                executor.scheduler = None
            except Exception as exc:  # pragma: no cover
                print(
                    f"[DIAG] td_live: scheduler 清理异常: {exc}",
                    file=sys.stderr, flush=True,
                )
            print(
                "[DIAG] td_live: 优雅停止完成（当前轮已自然结束，线程将退出）",
                file=sys.stderr, flush=True,
            )

        threading.Thread(
            target=_do, daemon=True, name="td-executor-graceful-stop"
        ).start()

    def _wait_thread_exit(self, timeout: float = 10.0) -> bool:
        """等待旧循环线程退出（stop 后 lumibot 收尾可能 >0.3s）。

        stop() 只设 stop_event，线程跑完 on_abrupt_closing / scheduler
        清理才真正退出；start() 的 is_alive 守卫若在收尾期间被调用会
        静默拒绝启动，导致参数变更后循环停在 running=False。
        轮询等待线程退出（默认 10s），超时返回 False（调用方 fail-closed，
        不启动新循环以避免双循环重复下单）。
        """
        t = self._thread
        if t is None or not t.is_alive():
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not t.is_alive():
                return True
            time.sleep(0.1)
        return not t.is_alive()

    def status(self) -> dict[str, Any]:
        alive = self._thread is not None and self._thread.is_alive()
        return dict(self._state, thread_alive=alive)

    # ── 参数同步 ──────────────────────────────────────────────────────
    def sync_from_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """按 exec_params 同步启停（WebUI 保存 / execute_signal 时调用）。

        td_enabled=True：未运行 → 启动；运行中且参数未变 → 保持；
        运行中但标的/周期/模式/风控变化 → 重启（stop → start）。
        td_enabled=False：运行中 → 停止。
        """
        if params.get("td_enabled"):
            running = (
                self._state.get("running")
                or (self._thread is not None and self._thread.is_alive())
            )
            if running:
                scenes_fp = _scenes_fingerprint(params)
                changed = any(
                    self._state.get(k) != params.get(pk)
                    for k, pk in (
                        ("symbols", "td_symbols"),
                        ("sleeptime", "td_sleeptime"),
                        ("quantity_mode", "quantity_mode"),
                    )
                ) or self._state.get("scenes_fp") != scenes_fp
                if not changed:
                    return self.status()
                # 参数变化 → 重启循环（先停旧的，避免双循环）；
                # 等旧线程真正退出再 start，避免 is_alive 守卫挡回。
                self.stop()
                if not self._wait_thread_exit():
                    self._state["last_error"] = (
                        "旧 TD 循环线程未在超时内退出，拒绝启动新循环"
                        "（避免双循环重复下单）"
                    )
                    self._state["running"] = False
                    print(
                        "[DIAG] td_live: old thread did not exit within "
                        "timeout — refusing to start new loop",
                        file=sys.stderr, flush=True,
                    )
                    return self.status()
            return self.start(params)
        if self._state.get("running") or (
            self._thread is not None and self._thread.is_alive()
        ):
            return self.stop()
        return self.status()


def _scenes_fingerprint(params: dict[str, Any]) -> str:
    """S3a：场景配置指纹（含字段变化，供 sync_from_params 判定重启）。"""
    import json

    scenes = params.get("scenes") or {}
    return json.dumps(scenes, sort_keys=True, ensure_ascii=False, default=str)


_SLEEPTIME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900,
    "1H": 3600, "4H": 14400, "1D": 86400, "1W": 604800,
}


def _parse_sleeptime_seconds(value: str) -> int:
    """场景周期字符串 → 秒（1m/5m/15m/1H/4H/1D/1W）。

    S3a 起 td_live 自行定义（不从策略模块导入，避免测试 fake 模块拦截
    _build_executor 延迟 import 时缺属性）。
    """
    return _SLEEPTIME_SECONDS.get(str(value).strip(), 60)


def get_runner() -> _TdLiveRunner:
    global _runner
    with _lock:
        if _runner is None:
            _runner = _TdLiveRunner()
        return _runner


def sync_from_params(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """模块级入口：按 exec_params 同步 TD live 循环（幂等，可反复调用）。"""
    if params is None:
        from nanobot_quant.exec_params import load_exec_params

        params = load_exec_params()
    return get_runner().sync_from_params(params)


def status() -> dict[str, Any]:
    return get_runner().status()
