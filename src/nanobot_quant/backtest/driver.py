"""方案 B Step 3：历史事件重放驱动（backtest driver）。

复用实盘（td_live）的场景构造与策略决策代码，换回测 broker/数据源：

- 数据源：``ReplayDataSource``（gate_cex 翻页拉全量历史，seek 逐 bar 推进）
- 撮合：``BacktestBroker``（每 slot 独立实例 = slot 级资金隔离，内存账本，
  按当前 bar 收盘价 ± slippage 成交、fee_rate 从所得币扣、min_quote/资金
  不足 fail-closed——语义对齐实盘 CexBroker，docs/quant-system.md §25.3）
- 决策：与实盘同一 ``TdSequentialStrategy``（含场景激活、TD 信号、真分账
  CEX 分支、SL/TP、信号周期门控）——唯一区别是通过两个注入 hook
  （``_slot_broker_factory`` / ``_price_source_override``）把子账号 broker
  与取价替换为回测内存实现，实盘路径零改动、零网络。

驱动循环（每 bar 一次）：

    ds.seek(ts) → strategy._activate_scene(scene, rt) → 逐标的
    strategy._evaluate_symbol() → 记录各 slot 净值（mark-to-market）

回测隔离（2026-08-23 拍板）：

- 批次台账：BatchManager 用模拟账号（``bt-{scene}-s{i}``）+ 独立临时目录
  （不回写/复用实盘 batches.gate.*.json，干净重放）
- 不读/不写 gate.json、不调任何真实下单/余额 API、只读 Gate 公开 K 线
- 每 slot 初始资金纯模拟参数（默认 1000U，2026-08-26 拍板放大——
  回测专注收益率分析，资金约束不干扰信号成交；与实盘子账号完全隔离

Step 3 验证点（docs/quant-system.md §25.6）：同一历史区间，回测决策日志
（BUY / SLOT SKIP / HOLD / SELL / 止损）与实盘逻辑逐事件一致——即本驱动
产出的 [TD] 决策日志与实盘同源（同一策略代码），在 HF Space 跑本驱动
观察日志类型覆盖即可。

CLI：

    python -m nanobot_quant.backtest.driver --scene mid --symbols CRCLX \
        --start 2026-08-01 --end 2026-08-15 --initial-quote 100 --batches 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from nanobot_quant.backtest.fetchers import (
    CROSS_VENUE_NOTE,
    DEFAULT_SOURCE,
    SOURCE_LABELS,
    is_cross_venue,
    make_fetcher,
    normalize_source,
    resolve_symbol,
    source_label,
    unavailable_symbols,
)
from nanobot_quant.backtest.replay_data_source import ReplayDataSource
from nanobot_quant.environment.sensors import (
    EMA_TREND_N,
    ema_trend,
    ema_warmup_bars,
)
from nanobot_quant.batches import BatchManager
from nanobot_quant.brokers.backtest_broker import BacktestBroker
from nanobot_quant.exec_params import load_exec_params
from nanobot_quant.gate_credentials import load_tokens_json
from nanobot_quant.strategies.registry import load_selected
from nanobot_quant.td_params import load_td_params

# 回测撮合默认值（docs/quant-system.md §25.3 拍板）
DEFAULT_INITIAL_QUOTE = 1000.0  # 每 slot 初始资金（USDT，纯模拟；2026-08-26 放大，专注收益率）
DEFAULT_MIN_QUOTE = 3.0         # 对齐 Gate min_quote $3（服务端实时下发，回测固定默认）
DEFAULT_FEE_RATE = 0.001        # Gate taker 单边 0.1%（全局扁平 fee_rate 覆盖）

def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """'YYYY-MM-DD[ HH:MM[:SS]]' → UTC naive datetime（历史 K 线为 UTC）。"""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        pass
    return datetime.strptime(value.strip(), "%Y-%m-%d")


def _timestep_for(sleeptime: str) -> str:
    """场景 sleeptime（统一周期名）→ 回测 K 线粒度。

    2026-08-24 方案 C：粒度由 gate_cex spec 声明（16 个原生周期），不再各自
    硬编码映射表。2026-10-22 起额外接受**合成周期**（2m/4m/6m/10m/20m/45m，
    由更粗原生周期整除聚合，见 ``data_sources.periods.COMPOSITE_PERIODS``）——
    仅回测可用，td-table / 实盘调度仍只认原生档。
    不支持的周期抛 ValueError（fail-closed，不静默回退）。
    """
    s = str(sleeptime)
    from nanobot_quant.data_sources import get_data_source
    from nanobot_quant.data_sources.periods import COMPOSITE_PERIODS

    spec = get_data_source("gate_cex")
    if s not in spec.bars and s not in COMPOSITE_PERIODS:
        native = ", ".join(spec.bars)
        comp = ", ".join(f"{p}(合成)" for p in COMPOSITE_PERIODS)
        raise ValueError(
            f"不支持的场景周期: {sleeptime!r}（原生: {native}；合成: {comp}）"
        )
    return s


class BacktestDriver:
    """单场景历史回放驱动（Step 3：先单场景 mid 15m 跑通）。"""

    def __init__(
        self,
        scene: str = "mid",
        params: Optional[dict[str, Any]] = None,
        symbols: Optional[list[str]] = None,
        start_ts: Optional[datetime] = None,
        end_ts: Optional[datetime] = None,
        initial_quote: float = DEFAULT_INITIAL_QUOTE,
        batches: Optional[int] = None,
        slippage: Optional[float] = None,
        fixed_amount: Optional[float] = None,
        overrides: Optional[dict[str, Any]] = None,
        fetcher: Optional[Callable] = None,
        source: object = DEFAULT_SOURCE,
        ledger_dir: Optional[Path | str] = None,
        progress_path: Optional[Path | str] = None,
        progress_extra: Optional[dict[str, Any]] = None,
    ) -> None:
        # 1. 参数：默认复用 exec_params 当前配置（场景参数默认复用、可临时覆盖，
        #    覆盖不影响实盘——2026-08-23 拍板）
        self.params = params if params is not None else load_exec_params()
        # 2026-08-29：全局扁平键基准（场景留空/回测表单缺键时回退用）。
        # params 为回测表单（无 min_hold_bars 等全局键）时必须从 exec_params
        # 读全局值，否则 min_hold_bars 等恒用类默认、与实盘不一致。
        self._exec_params = load_exec_params() if params is not None else self.params
        self.scene_name = scene
        self.scenes = self.params.get("scenes") or {}
        scene_cfg = self.scenes.get(scene)
        if not scene_cfg:
            raise ValueError(
                f"场景 {scene!r} 不存在（可用: {', '.join(self.scenes)}）；"
                f"回测前请在 /config/exec 启用该场景并保存"
            )
        self.scene_cfg = dict(scene_cfg)

        # 2. 回测覆盖（不影响实盘文件）
        self.symbols = list(symbols or self.scene_cfg.get("symbols") or [])
        if not self.symbols:
            raise ValueError(f"场景 {scene} 无标的（symbols 为空）")
        self.batches = int(
            batches if batches is not None else self.scene_cfg.get("batches", 3)
        )
        if self.batches < 1:
            raise ValueError(f"batches 必须 ≥1（当前 {self.batches}）")
        self.slippage = float(
            slippage if slippage is not None else self.params.get("slippage", 0.0)
        )  # 百分比语义（1=1%），与 exec_params/BacktestBroker 一致
        self.fixed_amount = (
            float(fixed_amount) if fixed_amount is not None else None
        )
        # 回测覆盖参数（覆盖 > 场景 > 类默认）。_activate_scene 每 bar 从
        # rt.params 重读 td_fixed_amount，仅注入 strategy.parameters 会被
        # 每 bar 覆盖回场景值（2026-08-28 实测：页面填 10 成交仍 4U）。
        # 回测覆盖参数（覆盖 > 场景 > 全局/类默认）。_activate_scene 每 bar 从
        # rt.params 重读 td_fixed_amount，仅注入 strategy.parameters 会被
        # 每 bar 覆盖回场景值（2026-08-28 实测：页面填 10 成交仍 4U）。
        self._merged_params = dict(self.scene_cfg)
        self.overrides = {
            k: v for k, v in (overrides or {}).items() if v is not None
        }
        self._merged_params.update(self.overrides)
        if self.fixed_amount is not None:
            self._merged_params["td_fixed_amount"] = self.fixed_amount
        self.initial_quote = float(initial_quote)
        # 多周期回测（2026-10-21）：额外进度字段（period / period_index / period_total）
        self.progress_extra = dict(progress_extra or {})
        self.start_ts = start_ts
        self.end_ts = end_ts

        # 3. 时间粒度（场景周期 → K 线粒度；回测覆盖 sleeptime 优先——
        #    2026-09-03：仅本次回测生效，不回写实盘 exec_params）
        _period_ov = self.overrides.get("sleeptime")
        if _period_ov:
            self.scene_cfg["sleeptime"] = str(_period_ov)
        self.timestep = _timestep_for(self.scene_cfg.get("sleeptime", "15m"))

        # 4. TD 窗口（min_history）：td_bars（默认 120）——数据不足全程 SKIP
        #    与实盘一致（2026-08-23 拍板）
        self.min_history = int(self.params.get("td_bars", 120) or 120)

        # 4b. EMA 趋势变量（2026-10-20，C73/C74 研究线）
        #     **回测侧、默认关**：driver 注入 parameters，td_live 不注入 → 实盘恒 False。
        #     EMA 必须在前置预热过的全序列上算（n=144 的 5τ ≈ 362 根 ≫ TD 窗口），
        #     因此只拉长取数窗口、不改评估起点（C72）。
        _ema_on = self._merged_params.get("ema_gate_enabled")
        if _ema_on is None:
            _ema_on = self.params.get("ema_gate_enabled")
        self.ema_gate_enabled = bool(_ema_on) if _ema_on is not None else False
        self.ema_n = int(
            self._merged_params.get("ema_n")
            or self.params.get("ema_n")
            or EMA_TREND_N
        )
        self.ema_k = int(
            self._merged_params.get("ema_k") or self.params.get("ema_k") or 3
        )
        # 预热始终按 n 推算（界面只读）——即使闸门关也用于展示表头
        self.ema_warmup = ema_warmup_bars(self.ema_n)
        # 逐 bar 表模式：lean（默认，只存信号/成交/闸门翻转行）/ full（全量）
        self.bars_mode = str(
            self._merged_params.get("bars_mode")
            or self.params.get("bars_mode")
            or "lean"
        ).lower()
        if self.bars_mode not in ("lean", "full"):
            self.bars_mode = "lean"

        # 5. 数据源
        #    默认 Gate CEX（与执行同所，零行为变化）；``okx_cex`` 为跨所参考源
        #    （2026-10-20 拍板：放行 + 强制标注，不参与参数裁决）。
        tokens = load_tokens_json() or []
        self.tokens_json = tokens
        self.source = normalize_source(source)
        self.source_label = source_label(self.source)
        self.cross_venue = is_cross_venue(self.source)
        self.source_note: Optional[str] = None
        if self.cross_venue and self.start_ts is None:
            self.source_note = (
                f"未指定起始时间 → 按 {self.source_label} 单次上限取最近窗口"
            )
        self.data_source = ReplayDataSource(
            symbols=self.symbols,
            timestep=self.timestep,
            start_ts=self.start_ts,
            end_ts=self.end_ts,
            length=self.min_history,
            tokens_json=tokens,
            fetcher=fetcher if fetcher is not None else make_fetcher(self.source),
            resolver=(
                None
                if fetcher is not None
                else (lambda sym: resolve_symbol(sym, self.source, tokens))
            ),
            # EMA 长记忆变量需额外向前预热（C72：只拉长取数、不改评估起点）
            extra_warmup_bars=self.ema_warmup,
        )

        # 6. 撮合参数
        self.fee_rate = float(self.params.get("fee_rate", DEFAULT_FEE_RATE) or 0.0)
        self.min_quote = DEFAULT_MIN_QUOTE

        # 7. 台账目录：独立临时目录（干净重放，不回写实盘批次文件）
        self.ledger_dir = Path(ledger_dir) if ledger_dir else None

        # 8. 进度文件：运行期间写入 {status:running, progress:{...}}，
        #    完成后由调用方（tools_backtest worker）覆写为完整结果。
        #    仅 WebUI/MCP 轮询展示用——写失败静默、绝不阻塞回测。
        self.progress_path = Path(progress_path) if progress_path else None

    # ── 构造 ──────────────────────────────────────────────────────────

    def _build_slot_brokers(self) -> dict[int, BacktestBroker]:
        """每 slot 独立 BacktestBroker 实例（slot 级资金隔离）。"""
        return {
            i: BacktestBroker(
                initial_quote=self.initial_quote,
                fee_rate=self.fee_rate,
                slippage=self.slippage,
                min_quote_amount=self.min_quote,
                price_source=self.data_source.price_of,
                tokens_json=self.tokens_json,
            )
            for i in range(1, self.batches + 1)
        }

    def _min_hold_bars_value(self) -> int:
        """回测生效的 min_hold_bars：回测覆盖参数优先（overrides > 场景配置，
        None 视为未设置）；缺省回退表单/exec_params 全局（2026-08-29 修复：
        此前只读表单、恒默认 10，改全局对回测无效）。"""
        v = self._merged_params.get("min_hold_bars")
        if v is None:
            v = self.params.get("min_hold_bars")
        if v is None:
            v = self._exec_params.get("min_hold_bars", 10)
        return int(v or 0)

    def _build_strategy(self, main_broker: BacktestBroker) -> Any:
        """复用 td_live 的 parameters 构造语义（live_mode=False）。"""
        from nanobot_quant.strategies.td_sequential_strategy import (
            TdSequentialStrategy,
        )

        strategy_name = load_selected()
        td_params = load_td_params(strategy_name)

        strategy = TdSequentialStrategy(
            broker=main_broker, data_source=self.data_source
        )
        strategy.parameters = dict(
            TdSequentialStrategy.parameters,
            **{
                "symbols": self.symbols,
                "quantity": 10,
                "min_hold_bars": self._min_hold_bars_value(),
                "quantity_mode": self._merged_params.get("quantity_mode", "fixed"),
                "td_fixed_amount": float(
                    self._merged_params.get("td_fixed_amount", 10.0)
                ),
                "sleeptime": str(self.scene_cfg.get("sleeptime", "15m")),
                "max_position_pct": self._merged_params.get(
                    "max_position_pct", self.params["max_position_pct"]
                ),
                "max_drawdown_pct": self._merged_params.get(
                    "max_drawdown_pct", self.params["max_drawdown_pct"]
                ),
                "stop_loss_pct": self._merged_params.get(
                    "stop_loss_pct", self.params["stop_loss_pct"]
                ),
                "exit_order": self._merged_params.get("exit_order", "fifo"),
                "take_profit_pct": self._merged_params.get("take_profit_pct", 0.0),
                # 贝叶斯闸门（2026-08-31，回测侧）：WebUI 回测页覆盖参数优先，
                # 缺省回退 exec_params 平铺开关（/config/exec 持久化，默认 False=
                # 回测与原版一致）；td_live 不注入 → 实盘恒 False（实盘接入待
                # 回测裁决后讨论）。
                "gate_enabled": bool(
                    self._merged_params.get(
                        "gate_enabled", self.params.get("gate_enabled", False)
                    )
                ),
                "gate_red_min": float(
                    self._merged_params.get(
                        "gate_red_min", self.params.get("gate_red_min", 0.45)
                    )
                    or 0.45
                ),
                "td_start_slot": int(
                    self._merged_params.get("td_start_slot", 1)
                ),
                "min_account_value": float(
                    self._merged_params.get("min_account_value", 0)
                ),
                "fee_rate": self.fee_rate,
                "min_history": self.min_history,
                "tokens_json": self.tokens_json,
                "live_mode": False,  # 回测不写信号事件文件
                "strategy_variant": strategy_name,
                # EMA 趋势闸门（2026-10-20，回测侧，默认关）
                "ema_gate_enabled": self.ema_gate_enabled,
                "ema_n": self.ema_n,
                "ema_k": self.ema_k,
                "channel_family": "cex",  # Step 3 回测撮合对齐 CexBroker（USDT）
                # 与 td_live 同一桥接模式：数据源契约从 broker.data_source 读
                "drops_in_progress_bars": bool(
                    getattr(
                        getattr(main_broker, "data_source", None),
                        "drops_in_progress_bars",
                        False,
                    )
                ),
            },
            **td_params,
        )
        print(
            f"[BACKTEST] 参数: strategy={strategy_name} scene={self.scene_name} "
            f"symbols={self.symbols} timestep={self.timestep} "
            f"batches={self.batches} initial_quote={self.initial_quote} "
            f"fee_rate={self.fee_rate} slippage={self.slippage} "
            f"min_history={self.min_history} entry_setup="
            f"{strategy.parameters.get('entry_setup')} exit_setup="
            f"{strategy.parameters.get('exit_setup')}",
            file=sys.stderr, flush=True,
        )
        self.strategy = strategy
        return strategy

    def _build_batch_managers(self) -> dict[str, BatchManager]:
        """模拟账号（bt-{scene}-s{i}）+ 独立临时台账目录，干净重放。"""
        if self.ledger_dir is None:
            self.ledger_dir = Path(
                tempfile.mkdtemp(prefix=f"nanobot_quant_backtest_{self.scene_name}_")
            )
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        account_ids = [
            f"bt-{self.scene_name}-s{i}" for i in range(1, self.batches + 1)
        ]
        return {
            sym: BatchManager(
                symbol=sym,
                account_ids=account_ids,
                # 文件名对齐 batches_path 约定（channel.scene.symbol），
                # 但落在回测独立目录，不碰实盘 batches 文件
                path=self.ledger_dir
                / f"batches.backtest.{self.scene_name}.{sym}.json",
                channel="backtest",
                scene=self.scene_name,
            )
            for sym in self.symbols
        }

    # ── 驱动 ──────────────────────────────────────────────────────────

    def _capital_stats(
        self,
        fills_detail: list[dict],
        initial_total: float,
        end_ts: str | None,
    ) -> dict[str, float]:
        """资金周转率与平均利用率（2026-08-29）。

        资金流 = quantity × avg_price × (1 − fee_rate)（买卖通用，与撮合账本一致）：
          - buy  : 实际现金支出（= qty×px×(1+slippage)）
          - sell : 实际现金回笼（= qty×px×(1−slippage)×(1−fee_rate)）
        周转率（单边）= Σ买入金额 ÷ 总资金（总资金 = 初始资金×批次）；
        双边 = Σ(买入+卖出) ÷ 总资金。
        利用率 = 按持有时间加权的平均在途资金 ÷ 总资金（评估区间 [首笔成交, end_ts]）。
        """
        empty = {
            "turnover": 0.0,
            "turnover_two_side": 0.0,
            "utilization": 0.0,
            "buy_flow": 0.0,
            "sell_flow": 0.0,
            "total_funds": round(initial_total, 6),
        }
        if not fills_detail or initial_total <= 0:
            return empty

        buy_flow = sell_flow = 0.0
        events: list[tuple] = []
        for f in fills_detail:
            flow = float(f["quantity"]) * float(f["avg_price"]) * (1.0 - self.fee_rate)
            if f["side"] == "buy":
                buy_flow += flow
            else:
                sell_flow += flow
            events.append((datetime.fromisoformat(f["ts"]), flow, f["side"]))

        turnover = buy_flow / initial_total
        turnover_two = (buy_flow + sell_flow) / initial_total

        # 平均在途（时间加权）：首笔成交 → 评估区间末（end_ts 兜底最后一笔成交）
        events.sort(key=lambda e: e[0])
        start = events[0][0]
        if end_ts:
            end = datetime.fromisoformat(end_ts)
            if end <= start:
                end = events[-1][0]
        else:
            end = events[-1][0]
        deployed = 0.0
        weighted = 0.0
        prev = start
        for ts, flow, side in events:
            if ts > end:
                break
            weighted += deployed * (ts - prev).total_seconds()
            deployed += flow if side == "buy" else -flow
            prev = ts
        weighted += deployed * (end - prev).total_seconds()
        duration = (end - start).total_seconds()
        utilization = (weighted / duration) / initial_total if duration > 0 else 0.0

        return {
            "turnover": round(turnover, 6),
            "turnover_two_side": round(turnover_two, 6),
            "utilization": round(max(utilization, 0.0), 6),
            "buy_flow": round(buy_flow, 6),
            "sell_flow": round(sell_flow, 6),
            "total_funds": round(initial_total, 6),
        }

    def _write_progress(
        self,
        stage: str,
        bars_done: int | None,
        bars_total: int | None,
        ts: str | None,
        fills: int,
        fills_detail: list[dict] | None = None,
        capital_stats: dict | None = None,
        bars_rows: list[dict] | None = None,
    ) -> None:
        """运行中进度写入（WebUI/MCP 轮询展示用，失败静默不阻塞回测）。

        progress_path 与结果文件同路径（<run_id>.json）：运行期间写
        ``{status: running, progress: {...}}``，完成后由调用方
        （tools_backtest._backtest_log）覆写为 done/error 完整结果。
        """
        if not self.progress_path:
            return
        pct = (
            round(bars_done / bars_total * 100, 1)
            if bars_total and bars_done is not None
            else None
        )
        payload = {
            "status": "running",
            "progress": {
                "stage": stage,
                "bars_done": bars_done,
                "bars_total": bars_total,
                "pct": pct,
                "ts": ts,
                "fills": fills,
                "fills_detail": fills_detail or [],  # 已成交明细（运行中实时可见）
                "capital_stats": capital_stats,  # 资金周转率/利用率（运行中实时）
                # 逐 bar 行（运行中实时可见；只带最近部分，避免几百次
                # 写盘把 2万行×上百字节 重复序列化爆大）
                "bars_rows": bars_rows or [],
                # 多周期回测（2026-10-21）：当前正在跑第几档、共几档
                **(self.progress_extra or {}),
            },
        }
        try:
            self.progress_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # 进度是 UX 信息，写失败不影响回测本身

    # ── EMA 趋势变量与逐 bar 表（2026-10-20，C73/C74 研究线，只读）─────

    def _build_ema_frames(self) -> dict[str, dict[str, Any]]:
        """每标的预计算 ``ema`` / ``slope`` / ``close`` / ``chg``（全序列）。

        为什么不在策略里算：策略每次只看 ``min_history`` 根窗口（120~200），
        而 n=144 的 5τ ≈ 362 根——用窗口算 EMA 会静默失真。这里在数据源
        **预热过的全序列**上一次性算好，逐 bar 只做查表（向量化，几乎零成本）。
        """
        out: dict[str, dict[str, Any]] = {}
        frames = getattr(self.data_source, "frames", {}) or {}
        for sym in self.symbols:
            df = frames.get(sym)
            if df is None or getattr(df, "empty", True):
                out[sym] = {"ema": None, "slope": None, "close": None, "chg": None}
                continue
            trend = ema_trend(df, n=self.ema_n, k=self.ema_k)
            close = df["close"].astype(float)
            out[sym] = {
                "ema": trend["ema"],
                "slope": trend["slope"],
                "close": close,
                "chg": close.pct_change(),
            }
        return out

    @staticmethod
    def _pick(series: Any, ts: Any) -> Optional[float]:
        """按时间戳从 Series 取值；缺值/NaN → None（不用 pandas 也能判 NaN）。"""
        if series is None:
            return None
        try:
            v = series.get(ts)
        except (TypeError, KeyError):
            return None
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if f != f else f  # NaN 自不等

    def run(self) -> dict[str, Any]:
        """预取全量历史 → 逐 bar 重放 → 返回净值曲线与决策摘要。"""
        self._write_progress("prefetch", 0, None, None, 0)
        # 启动前探测：该源拿不到行情的标的 —— fail-closed，列出原因、不静默少标的
        bad = unavailable_symbols(self.symbols, self.source, self.tokens_json)
        if bad:
            detail = "；".join(f"{sym}（{why}）" for sym, why in bad)
            raise RuntimeError(
                f"数据源 {self.source_label} 取不到以下标的的行情：{detail}。"
                f"请从标的池剔除它们，或改用 Gate CEX 数据源"
            )
        # 预取（网络只发生在此处）
        self.data_source.prefetch()
        bar_times = self.data_source.bar_times
        start_idx = self.data_source.start_idx
        if len(bar_times) <= start_idx:
            need = self.min_history
            raise RuntimeError(
                f"历史数据不足回测窗口: bars={len(bar_times)} < "
                f"min_history={need}（数据源: {self.timestep}，"
                f"区间 {self.start_ts} → {self.end_ts}；"
                f"该粒度至少需要 {need} 根 K 线（如 15m 需 ≥{need * 15 // 60}h / "
                f"1m 需 ≥{need}m / 1D 需 ≥{need} 天），请扩大区间后重试）"
            )

        # 构造回测组件
        slot_brokers = self._build_slot_brokers()
        main_broker = BacktestBroker(
            initial_quote=self.initial_quote,
            fee_rate=self.fee_rate,
            slippage=self.slippage,
            min_quote_amount=self.min_quote,
            price_source=self.data_source.price_of,
            tokens_json=self.tokens_json,
            data_source=self.data_source,  # 策略取数入口（Broker.data_source）
        )
        strategy = self._build_strategy(main_broker)
        # 手动执行一次 initialize（真实 lumibot Strategy.__init__ 会调；
        # stub 环境不会——driver 自驱动需要显式调用，构造 _risk/_td_params 等）
        strategy.initialize()
        batch_managers = self._build_batch_managers()

        # 注入回测 hooks（实盘默认 None，走真实路径）
        strategy._slot_broker_factory = lambda slot: slot_brokers[
            int(slot["slot"])
        ]
        strategy._price_source_override = self.data_source.price_of

        # 场景运行时（结构同 td_live._build_executor 的 rt）
        rt = {
            "enabled": True,
            "sleeptime": str(self.scene_cfg.get("sleeptime", "15m")),
            "symbols": self.symbols,
            "params": self._merged_params,
            "broker": main_broker,
            "batch_managers": batch_managers,
            "last_run": None,
        }
        strategy._scene_runtimes = {self.scene_name: rt}

        # 逐 bar 重放
        total_bars = len(bar_times) - start_idx
        self._write_progress("replay", 0, total_bars, None, 0)
        net_values: list[dict[str, Any]] = []
        fills_detail: list[dict[str, Any]] = []
        # 逐 bar 表采集（2026-10-20）：动作列直接复用策略自己的决策流
        # （``_event_sink``），不另写一套判定——单一实现、不会与实盘漂移。
        ema_frames = self._build_ema_frames()
        bar_rows: list[dict[str, Any]] = []
        rows_capped = False
        _bar_events: list[dict[str, Any]] = []
        strategy._event_sink = lambda ev: _bar_events.append(ev)  # noqa: B023
        _entry_setup = int(strategy.parameters.get("entry_setup", 9) or 9)
        _exit_setup = int(strategy.parameters.get("exit_setup", 9) or 9)
        _entry_cd = int(strategy.parameters.get("entry_countdown", 13) or 13)
        _exit_cd = int(strategy.parameters.get("exit_countdown", 13) or 13)
        _row_cap = 30000 if self.bars_mode == "full" else 20000
        prev_gate: dict[str, str] = {}
        for i, ts in enumerate(bar_times):
            if i < start_idx:
                continue
            self.data_source.seek(ts)
            # 场景激活（参数/broker/台账就位）→ 逐标的评估（TD 信号 + 下单）
            strategy._activate_scene(self.scene_name, rt)
            # 本轮 bar 新增成交（_tracked 累计，取增量关联到当前 bar 时间）
            # 键用 (slot, oid)：每个 BacktestBroker 实例的 _order_seq 都从 0 起，
            # 跨 slot 的 oid（bt0/bt1/…）会重复——仅用 oid 会把后一笔误判为已处理
            tracked_before = {
                (slot_no, oid): b
                for slot_no, b in slot_brokers.items()
                for oid in b._tracked
            }
            _bar_rows: dict[str, dict[str, Any]] = {}
            for sym in self.symbols:
                strategy.symbol = sym
                strategy.batch_manager = batch_managers.get(sym)
                em = ema_frames.get(sym) or {}
                ema_v = self._pick(em.get("ema"), ts)
                slope_v = self._pick(em.get("slope"), ts)
                strategy._ema_gate_value = (ema_v, slope_v)
                _bar_events.clear()
                strategy._evaluate_symbol()
                sig = getattr(strategy, "_last_signal", {}) or {}
                # 闸门列 = 策略自己的判定结果（候选买入口径，2026-10-20）：
                # ""=该 bar 无候选买信号（闸门未被问到）/pass/block/na。
                # 不再由 slope 符号反推——否则「状态」会伪装成「决策」。
                gate = str(getattr(strategy, "_last_ema_gate_state", "") or "")
                # HOLD 是每根 bar 都有的心跳事件，不算「有意思」——否则精简
                # 模式退化成全量（2026-10-20 测试实测 61/61 全被保留）
                _evs = [
                    e for e in _bar_events
                    if str(e.get("event", "")).upper() != "HOLD"
                ]
                row = {
                    "ts": ts.isoformat(),
                    "symbol": sym,
                    "close": self._pick(em.get("close"), ts),
                    "chg": self._pick(em.get("chg"), ts),
                    "setup_buy": sig.get("setup_buy"),
                    "setup_sell": sig.get("setup_sell"),
                    "cd_buy": sig.get("cd_buy"),
                    "cd_sell": sig.get("cd_sell"),
                    "score": sig.get("score"),
                    "ema": ema_v,
                    "slope": slope_v,
                    "gate": gate,
                    "action": " / ".join(
                        f"{e.get('event')}:{e.get('note')}" for e in _evs
                    ),
                    "fill": "",
                }
                _bar_rows[sym] = row
                # 精简模式只存「有意思」的行：有动作 / 闸门拦 / 接近阈值 / 翻转
                if self.bars_mode == "full":
                    keep = True
                else:
                    keep = (
                        bool(row["action"])
                        or (row["setup_buy"] or 0) >= _entry_setup - 1
                        or (row["setup_sell"] or 0) >= _exit_setup - 1
                        or (row["cd_buy"] or 0) >= _entry_cd - 1
                        or (row["cd_sell"] or 0) >= _exit_cd - 1
                        or gate != (prev_gate.get(sym) or "")
                    )
                if keep:
                    if len(bar_rows) >= _row_cap:
                        rows_capped = True
                    else:
                        bar_rows.append(row)
                prev_gate[sym] = gate
            for slot_no, b in sorted(slot_brokers.items()):
                for oid, meta in b._tracked.items():
                    if (slot_no, oid) in tracked_before:
                        continue
                    _fill_sym = meta.get("symbol")
                    _row = _bar_rows.get(_fill_sym)
                    if _row is not None:
                        # 成交行必须进表（否则「有成交却没逐 bar 记录」）
                        _row["fill"] = (
                            f"{meta.get('side')} {meta.get('quantity')}"
                            f"@{meta.get('avg_price')}"
                            f" · slot{slot_no}"
                        )
                        if self.bars_mode != "full" and _row not in bar_rows:
                            if len(bar_rows) < _row_cap:
                                bar_rows.append(_row)
                            else:
                                rows_capped = True
                    fills_detail.append(
                        {
                            "ts": ts.isoformat(),
                            "slot": slot_no,
                            "scene": self.scene_name,
                            "symbol": meta.get("symbol"),
                            "pair": meta.get("pair"),
                            "side": meta.get("side"),
                            "quantity": meta.get("quantity"),
                            "strategy_price": meta.get("strategy_price"),
                            "avg_price": meta.get("avg_price"),
                            "reason": meta.get("reason"),
                            "state": "LONG" if meta.get("side") == "buy" else "EXIT",
                            # EMA 趋势变量（2026-10-20）：逐笔可核对闸门当时状态
                            "ema": (_row or {}).get("ema"),
                            "slope": (_row or {}).get("slope"),
                        }
                    )
            # 净值快照（mark-to-market，各 slot 独立账本）
            total = 0.0
            for b in slot_brokers.values():
                total += b.snapshot()["total"]
            net_values.append({"ts": ts.isoformat(), "net": round(total, 6)})

            # 进度（每 bar 写一次：平滑推进；几百次小 I/O 开销可忽略）
            fills_now = sum(
                len(b._tracked) for b in slot_brokers.values()
            )
            self._write_progress(
                "replay",
                i - start_idx + 1,
                total_bars,
                ts.isoformat(),
                fills_now,
                fills_detail=fills_detail,
                capital_stats=self._capital_stats(
                    fills_detail, self.initial_quote * self.batches, ts.isoformat()
                ),
                bars_rows=bar_rows[-5000:],
            )

        # 成交记录（_tracked 是累计列表，结束时统计一次）
        fills = sum(len(b._tracked) for b in slot_brokers.values())

        # 每标的批次状态摘要（open 槽位 = 未平仓，含数量/成本价；期末价 = 最后 bar 收盘价）
        frames = getattr(self.data_source, "_frames", {})
        last_close: dict[str, float] = {}
        for sym in self.symbols:
            df = frames.get(sym)
            if df is not None and not df.empty and "close" in df.columns:
                last_close[sym] = float(df["close"].iloc[-1])
        slots_summary: dict[str, Any] = {}
        for sym, bm in batch_managers.items():
            open_slots = [
                {
                    "slot": s["slot"],
                    "qty": (s.get("lot") or {}).get("qty"),
                    "entry_price": (s.get("lot") or {}).get("entry_price"),
                    "account_id": s.get("account_id"),
                }
                for s in bm.slots
                if s["status"] == "open"
            ]
            slots_summary[sym] = {
                "slots": len(bm.slots),
                "open": open_slots,
                "final_price": last_close.get(sym),
                "ledger": str(bm.path),
            }

        final_net = net_values[-1]["net"] if net_values else 0.0
        initial_total = self.initial_quote * self.batches
        roi = (final_net / initial_total - 1.0) if initial_total > 0 else 0.0

        result = {
            "scene": self.scene_name,
            "symbols": self.symbols,
            "timestep": self.timestep,
            # 数据源（2026-10-20）：gate_cex = 与执行同所；okx_cex = 跨所参考
            "source": self.source,
            "source_label": self.source_label,
            "cross_venue": self.cross_venue,
            "cross_venue_note": CROSS_VENUE_NOTE if self.cross_venue else None,
            "source_note": self.source_note,
            "bars": len(net_values),
            "fetched_bars": len(bar_times) if bar_times else 0,
            "start_ts": (
                bar_times[start_idx].isoformat() if len(bar_times) > start_idx else None
            ),
            "end_ts": bar_times[-1].isoformat() if bar_times else None,
            "initial_quote": self.initial_quote,
            "initial_total": round(initial_total, 6),
            "final_net": round(final_net, 6),
            "roi": round(roi, 6),
            "batches": self.batches,
            "fills": fills,
            "fills_detail": fills_detail,
            "skip_counts": dict(getattr(strategy, "_skip_counts", {}) or {}),
            # 逐 bar 表（2026-10-20）：lean = 只存信号/成交/闸门翻转行；
            # full = 每根 bar 全存（体积≈2–3 MB/次，靠 cap 封顶）
            "bars_mode": self.bars_mode,
            "bars_rows": bar_rows,
            "bars_meta": {
                "mode": self.bars_mode,
                "count": len(bar_rows),
                "capped": rows_capped,
                "cap": _row_cap,
                "td_warmup_bars": self.min_history - 1,
                "ema_warmup_bars": self.ema_warmup,
                "fetched_bars": len(bar_times) if bar_times else 0,
                "evaluated_bars": len(net_values),
            },
            # EMA 趋势闸门统计（只拦 BUY；na = 预热不足/ATR≤0 → fail-open）
            "ema_gate": {
                "enabled": self.ema_gate_enabled,
                "n": self.ema_n,
                "k": self.ema_k,
                "warmup": self.ema_warmup,
                "pass": int(getattr(strategy, "_ema_gate_pass", 0) or 0),
                "block": int(
                    (getattr(strategy, "_skip_counts", {}) or {}).get("ema_gate", 0)
                ),
                "na": int(
                    (getattr(strategy, "_skip_counts", {}) or {}).get(
                        "ema_gate_na", 0
                    )
                ),
            },
            "capital_stats": self._capital_stats(
                fills_detail, initial_total, bar_times[-1].isoformat() if bar_times else None
            ),
            "net_values": net_values,
            "slots": slots_summary,
            "ledger_dir": str(self.ledger_dir),
            "backtest_config": {
                "scene": self.scene_name,
                "symbols": self.symbols,
                "timestep": self.timestep,
                "source": self.source,
                "source_label": self.source_label,
                "cross_venue": self.cross_venue,
                "start_ts": (
                    bar_times[start_idx].isoformat()
                    if len(bar_times) > start_idx
                    else None
                ),
                "end_ts": bar_times[-1].isoformat() if bar_times else None,
                "batches": self.batches,
                "initial_quote": self.initial_quote,
                "initial_total": round(initial_total, 6),
                "td_fixed_amount": self._merged_params.get("td_fixed_amount"),
                # 数量模式实际生效值（2026-10-20）：fixed/fixed_amount/value
                # —— 报告里必须可见，否则 0 成交（position_limit 拦截）无从自查
                "quantity_mode": (
                    getattr(strategy, "quantity_mode", None)
                    or self._merged_params.get("quantity_mode")
                    or "fixed"
                ),
                "slippage": self.slippage,
                # 手续费率实际生效值（2026-10-20）：此前快照缺该键，报告回退 0
                # → 误显示「手续费率 0.00%」，而撮合其实按 0.1% 扣（成交价可反推）
                "fee_rate": self.fee_rate,
                # EMA 趋势变量（2026-10-20，C73/C74 研究线，默认关；
                # 预热根数自动按 n 推算，界面只读）
                "ema_gate_enabled": self.ema_gate_enabled,
                "ema_n": self.ema_n,
                "ema_k": self.ema_k,
                "ema_warmup": self.ema_warmup,
                "bars_mode": self.bars_mode,
                # 策略实际生效值（_activate_scene 每 bar 注入后；
                # 2026-08-29 扩展：结果区先展示回测参数，与实盘同口径）
                "entry_setup": (strategy._td_params or {}).get("entry_setup"),
                "entry_countdown": (strategy._td_params or {}).get(
                    "entry_countdown"
                ),
                "exit_setup": (strategy._td_params or {}).get("exit_setup"),
                "exit_countdown": (strategy._td_params or {}).get(
                    "exit_countdown"
                ),
                "min_hold_bars": getattr(strategy, "_min_hold_bars", None),
                "exit_order": getattr(strategy, "_exit_order", None),
                "stop_loss_pct": getattr(
                    getattr(strategy, "_risk", None), "stop_loss_pct", None
                ),
                "take_profit_pct": getattr(
                    strategy, "_take_profit_pct", None
                ),
                "sell_only_profit_high": getattr(
                    strategy, "_sell_only_profit_high", None
                ),
                "sell_only_profit_low": getattr(
                    strategy, "_sell_only_profit_low", None
                ),
                "momentum_exit": getattr(strategy, "_momentum_exit", None),
                "cd_stall_n": getattr(strategy, "_cd_stall_n", None),
                "cd_entry_setup_gap": getattr(
                    strategy, "_cd_entry_setup_gap", None
                ),
                "td_sell_all": getattr(strategy, "_td_sell_all", None),
                "cd_exit_min_profit": getattr(
                    strategy, "_cd_exit_min_profit", None
                ),
                "cd_exit_all": getattr(strategy, "_cd_exit_all", None),
                "td_start_slot": getattr(strategy, "_start_slot", None),
                "min_account_value": getattr(
                    strategy, "_min_account_value", None
                ),
            },
        }
        print(
            f"[BACKTEST] 完成 source={self.source} scene={self.scene_name} "
            f"bars={len(net_values)} fills={fills} 期末净值={final_net:.4f} "
            f"ROI={roi * 100:.2f}%"
            + (f" · {CROSS_VENUE_NOTE}" if self.cross_venue else ""),
            file=sys.stderr, flush=True,
        )
        return result


# ── CLI ───────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m nanobot_quant.backtest.driver",
        description="方案 B 回测驱动（Step 3）：事件重放，复用实盘策略决策代码",
    )
    ap.add_argument("--scene", default="mid", help="场景名（high/mid/low，默认 mid）")
    ap.add_argument("--symbols", default=None, help="逗号分隔标的列表（覆盖场景池）")
    ap.add_argument("--start", default=None, help="开始时间 YYYY-MM-DD[ HH:MM]（默认拉满）")
    ap.add_argument("--end", default=None, help="结束时间 YYYY-MM-DD[ HH:MM]（默认最新）")
    ap.add_argument("--initial-quote", type=float, default=DEFAULT_INITIAL_QUOTE,
                    help=f"每 slot 初始资金 USDT（默认 {DEFAULT_INITIAL_QUOTE}）")
    ap.add_argument("--batches", type=int, default=None,
                    help="批次/slot 数（覆盖场景配置）")
    ap.add_argument("--slippage", type=float, default=None,
                    help="滑点（小数，默认 0）")
    ap.add_argument("--gate-enabled", action="store_true", default=None,
                    help="接入贝叶斯闸门（回测 BUY 后验≥阈值禁买；默认跟随 exec_params/关闭）")
    ap.add_argument("--gate-red-min", type=float, default=None,
                    help="闸门禁买阈值（0.05-0.9，默认 0.45=red-only；0.20=yellow+red）")
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help=f"回测数据源（{', '.join(SOURCE_LABELS)}；默认 {DEFAULT_SOURCE}）")
    ap.add_argument("--output", default=None, help="结果 JSON 落盘路径（可选）")
    args = ap.parse_args(argv)

    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    overrides = {}
    if args.batches is not None:
        overrides["batches"] = args.batches
    if args.slippage is not None:
        overrides["slippage"] = args.slippage
    if args.gate_enabled is not None:
        overrides["gate_enabled"] = True
    if args.gate_red_min is not None:
        overrides["gate_red_min"] = args.gate_red_min

    driver = BacktestDriver(
        scene=args.scene,
        symbols=symbols,
        start_ts=_parse_ts(args.start),
        end_ts=_parse_ts(args.end),
        initial_quote=args.initial_quote,
        overrides=overrides or None,
        source=args.source,
    )
    result = driver.run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n结果已写入: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
