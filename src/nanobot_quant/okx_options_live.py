"""期权自动循环 daemon（C23 S3 + E 期接线，2026-09-07 / 2026-09-16）。

**一轮 = ① 到期判定 + ② 策略轮次**，与 TD live 同构（一个 runner · 一个开关 ·
一个 LIVE_STATE · 一轮内做完）——机制层改为继承 :class:`LiveRunnerBase`
（daemon 线程 / 优雅停止 / ``sync()`` 幂等 / 进程内 LIVE_STATE / append-only
事件文件），本模块只保留期权特有逻辑。``td_live.py`` 本次未动，将来单独迁移。

- ① 到期判定：``ot.settle_expired_puts()``（OTM 自动关账 / ITM 记赔付 / 矛盾挂
  settled_review fail-closed / 账单未出保持 open 下轮重试）。
- ② 策略轮次：TD 衰竭信号 → IV 闸门 → 合约选择 → 卖 put；权利金回落止盈买回。
  ``dry_run=True``（默认）时**只记录决策不下单** —— 「AI 不能自行授权实盘」在
  闭环里的结构性落实。

启停：``option_params.json`` 的 ``live`` 字段
``{"enabled": bool, "interval_s": int, "strategy": {...}}`` —— 期权页手动开关
（AI 不自行开启，与现货 td_enabled 同规则）。心跳默认 60s、范围 10–3600；
interval / strategy 变更经 ``sync()`` 重启线程生效。

诊断输出一律走 stderr（前缀 ``[OPT-LIVE]``）——gatekeeper 进程未配 logging
handler（logger.info 被 lastResort 静默丢弃）、launch.sh 会 eval stdout。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import okx_options_trade as ot
from .live_runner_base import LiveRunnerBase

_LIVE_EVENTS_NAME = "okx_options_live_events.jsonl"

DEFAULT_INTERVAL_S = 60
MIN_INTERVAL_S = 10
MAX_INTERVAL_S = 3600

# 策略默认参数（option_params.json 的 live.strategy 段；用户可在页面改）
# 取向（2026-09-15 拍板）：先跑通闭环，参数用真实样本迭代——所以
#   - dry_run 默认 True：循环跑起来也只「说它想做什么」，不下单；
#   - iv_min_percentile 默认 0（关闭）：历史 IV 分位需样本积累，不假装有依据。
DEFAULT_STRATEGY: dict = {
    "account": "",                      # 期权子账号（空 = 默认）
    "families": ["SOL-USD_UM"],         # 标的家族
    "td_period": "5m",                  # 信号周期（5m 为实证主力周期）
    "td_bars": 120,                      # K 线窗口
    "entry_setup": 9,                    # 买 9 阈值
    "entry_countdown": 13,               # countdown 13 阈值
    "iv_min_percentile": 0,              # IV 环境闸门（0 = 关）
    "take_profit_pct": 50,               # 权利金回落止盈线（%）
    "max_contracts_per_family": 1,
    "max_contracts_total": 3,
    "dry_run": True,
}


# ── 事件文件（append-only JSONL，与台账同目录持久化）───────

def _storage_dir() -> Path:
    from .credential_registry import _get_storage_dir
    return Path(_get_storage_dir())


def events_path() -> Path:
    return _storage_dir() / _LIVE_EVENTS_NAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_event(ev: dict) -> None:
    p = events_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def load_events(limit: int = 50) -> list[dict]:
    """从事件文件尾部读取最近 N 条 settle 判定事件（倒序返回，最新在前）。"""
    p = events_path()
    if not p.exists():
        return []
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            back = min(size, 65536)
            f.seek(size - back)
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if back < size:
        lines = lines[1:]  # 块首行可能被截断
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    # 到期事件 enrich：台账存在同现货对（期权基础币 -USD/-USDC）的 filled 行
    #   put  → filled spot_cover ⇒ 已现货补买（covered=True，前端不再显示「补买」入口）
    #   call → filled spot_exit  ⇒ 已现货出货（exited=True，前端不再显示「出货」入口）
    covers: set[str] = set()
    exits: set[str] = set()
    try:
        for r in ot.load_ledger():
            if r.get("status") != "filled":
                continue
            if r.get("kind") == "spot_cover":
                covers.add(str(r.get("inst_id") or ""))
            elif r.get("kind") == "spot_exit":
                exits.add(str(r.get("inst_id") or ""))
    except Exception:
        covers, exits = set(), set()
    for ev in out:
        if ev.get("status") == ot.STATUS_SETTLED_ITM:
            base = str(ev.get("inst_id") or "").split("-")[0]
            pairs = (f"{base}-USD", f"{base}-USDC")
            is_call = (str(ev.get("opt_type") or "").upper() == "C"
                       or str(ev.get("inst_id") or "").endswith("-C"))
            if is_call:
                ev["exited"] = any(c in exits for c in pairs)
            else:
                ev["covered"] = any(c in covers for c in pairs)
    return out


# ── 配置（option_params.json 的 live 字段）─────────────────

def live_config() -> dict:
    d = (ot.load_option_params().get("live") or {})
    try:
        enabled = bool(d.get("enabled", False))
    except (TypeError, ValueError):
        enabled = False
    try:
        interval_s = int(d.get("interval_s", DEFAULT_INTERVAL_S))
    except (TypeError, ValueError):
        interval_s = DEFAULT_INTERVAL_S
    interval_s = min(max(interval_s, MIN_INTERVAL_S), MAX_INTERVAL_S)
    strat = dict(DEFAULT_STRATEGY)
    raw = d.get("strategy")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in DEFAULT_STRATEGY or k == "selector":
                strat[k] = v
    return {"enabled": enabled, "interval_s": interval_s, "strategy": strat}


def save_live_config(enabled: bool | None = None,
                     interval_s: int | None = None,
                     strategy: dict | None = None) -> dict:
    cur = live_config()
    if enabled is not None:
        cur["enabled"] = bool(enabled)
    if interval_s is not None:
        try:
            cur["interval_s"] = min(max(int(interval_s), MIN_INTERVAL_S),
                                    MAX_INTERVAL_S)
        except (TypeError, ValueError):
            pass
    if isinstance(strategy, dict):
        strat = dict(cur["strategy"])
        for k, v in strategy.items():
            if k in DEFAULT_STRATEGY or k == "selector":
                strat[k] = v
        cur["strategy"] = strat
    ot.save_option_params(live=cur)
    return cur


# ── 策略轮次（卖 put + 权利金回落止盈）──────────────────────

def _log(msg: str) -> None:
    """诊断输出统一走 stderr（前缀 [OPT-LIVE]，便于在 Runtime logs 里 grep）。

    两处历史教训：① gatekeeper 进程未配 logging handler，logger.info 被 Python
    lastResort 静默丢弃；② launch.sh 会捕获 stdout 并 eval，print 到 stdout 会被
    当 shell 命令执行。因此统一 print(..., file=sys.stderr, flush=True)。
    """
    print(f"[OPT-LIVE] {msg}", file=sys.stderr, flush=True)


def _strategy_params(cfg: dict) -> dict:
    """live_config()['strategy'] → evaluate_entry 需要的 params 形状。"""
    return dict(cfg.get("strategy") or {})


# ── Runner（机制层继承 LiveRunnerBase，与 TD live 同构）──────

class _OkxOptionsRunner(LiveRunnerBase):
    """期权线 runner（方案 B，与 td_live 同构）。

    **节拍交给 lumibot**：覆盖 :meth:`run_forever`，内部构造 ``StrategyExecutor``
    并 ``executor.run()`` 长驻；不再自己掐 interval 心跳（到期判定已挪进策略的
    ``on_trading_iteration``，每轮与策略一起跑）。

    ``do_round()`` 保留为「单轮」入口（测试/调试）——直接调策略的一轮，
    与引擎路径共用同一份决策实现。
    """

    LOG_PREFIX = "[OPT-LIVE]"
    EVENTS_NAME = _LIVE_EVENTS_NAME
    DEFAULT_INTERVAL_S = DEFAULT_INTERVAL_S
    INTERVAL_RANGE = (MIN_INTERVAL_S, MAX_INTERVAL_S)

    def __init__(self) -> None:
        super().__init__()
        self._executor = None
        self._strategy = None

    # ── 基类钩子 ──
    def load_config(self) -> dict:
        return live_config()

    def config_changed(self, old: dict, new: dict) -> bool:
        # 与接线前的 `last_cfg == cfg` 全等比较语义一致：
        # interval / enabled / strategy（页面保存的整份 config）任一变化即重启。
        return dict(old or {}) != dict(new or {})

    def storage_dir(self) -> Path:
        # 经模块级 events_path() 取目录（而非直接 _storage_dir()），
        # 便于测试 monkeypatch 隔离事件文件——两条路径同源，行为一致。
        return events_path().parent

    # ── 调度：交给 lumibot（方案 B）──
    def run_forever(self) -> None:
        """长驻：构造 executor 并阻塞在 ``executor.run()``。

        ``_round_active`` 全程为 True —— :meth:`LiveRunnerBase.stop` 因此会等
        executor 自然返回（策略在下一轮开头看到 ``stop_requested`` 后退出），
        不会强行中断正在执行的一轮。
        """
        self._round_active = True
        try:
            self._build_executor()
            self._executor.run()
        finally:
            self._round_active = False

    def stop(self) -> dict:
        """优雅停止：先请策略在下一轮退出，再等线程结束。

        **不调** ``executor.stop()`` —— lumibot 内部 ``shutdown(wait=True)`` 会等
        业务轮收尾，遇网络卡死即永久挂住（TD live 已踩过）。
        """
        if self._strategy is not None:
            try:
                self._strategy.parameters["stop_requested"] = True
                self._log("已请求策略停止（下一轮开头退出）")
            except Exception:  # noqa: BLE001
                pass
        return super().stop()

    def do_round(self) -> dict:
        """单轮（测试/调试）：直接调策略的一轮，与引擎路径共用决策实现。

        若策略已存在，先把 config 里的 strategy 参数合并进去 —— 与
        `_build_executor` 的注入语义一致（否则测试里 `save_live_config` 后
        参数不会生效，因为单轮路径不重建 executor）。
        """
        if self._strategy is None:
            self._build_executor()
        else:
            strat_cfg = dict(self.load_config().get("strategy") or {})
            if strat_cfg:
                self._strategy.parameters = {
                    **dict(self._strategy.parameters), **strat_cfg,
                }
        self._strategy.on_trading_iteration()
        with self._lock:
            self._state["last_run"] = _utc_now()
        return {"ok": True}

    # ── executor 构造 ──
    def _build_executor(self):
        """造 lumibot executor（惰性 import，避免启动即拉 lumibot 污染 stdout）。"""
        from lumibot.strategies.strategy_executor import StrategyExecutor

        from .brokers.okx_options_broker import OkxOptionsBroker
        from .data.okx_options_data_source import OkxOptionsDataSource
        from .strategies.okx_options_put_strategy import OkxOptionsPutStrategy

        cfg = self.load_config()
        strat_cfg = dict(cfg.get("strategy") or {})
        account = str(strat_cfg.get("account") or "")
        sleeptime = _to_sleeptime(self._interval_s(cfg))

        data_source = OkxOptionsDataSource()
        broker = OkxOptionsBroker(account=account, data_source=data_source)
        strategy = OkxOptionsPutStrategy(
            broker=broker, data_source=data_source, sleeptime=sleeptime,
        )
        strategy.parameters = {
            **dict(OkxOptionsPutStrategy.parameters),
            **strat_cfg,
            "live_mode": True,
            "stop_requested": False,
        }
        print(
            f"[DIAG] 期权 runner: account={account or '(default)'} "
            f"sleeptime={sleeptime} families={strat_cfg.get('families')} "
            f"dry_run={strat_cfg.get('dry_run')} "
            f"entry=setup{strat_cfg.get('entry_setup')}/cd{strat_cfg.get('entry_countdown')} "
            f"tp={strat_cfg.get('take_profit_pct')}%",
            file=sys.stderr, flush=True,
        )
        executor = StrategyExecutor(strategy)
        executor.daemon = True
        self._strategy = strategy
        self._executor = executor
        return executor


def _to_sleeptime(seconds: int) -> str:
    """秒 → lumibot sleeptime 字符串。

    lumibot 的 ``calculate_strategy_trigger`` 用 ``int(sleeptime[:-1])`` 解析，
    即必须是「数字 + 单字母」形式（1m / 5m / 1H / 1D）；写 ``"minute"`` 会
    在 ``[:-1]`` 后剩下 ``"minut"`` 而抛 ValueError。
    单位与 TD live 的 td_sleeptime 写法保持一致（m=分钟、H=小时、D=天）。
    """
    try:
        secs = max(60, int(seconds))
    except (TypeError, ValueError):
        secs = 60
    if secs % 86400 == 0:
        return f"{secs // 86400}D"
    if secs % 3600 == 0:
        return f"{secs // 3600}H"
    if secs % 60 == 0:
        return f"{secs // 60}m"
    return "1m"


_RUNNER: _OkxOptionsRunner | None = None


def _runner() -> _OkxOptionsRunner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = _OkxOptionsRunner()
    return _RUNNER


# 兼容层：历史上 _state 是模块级字典（可能有外部直接读取），现指向 runner 的
# 同一对象——读写双向可见，且 do_round 仍写入原有字段名。
_state: dict = _runner()._state


# ── 模块级 API（保持接线前的签名与返回，内部委托 runner 单例）──────

def run_once() -> dict:
    """跑一轮（线程与测试共用）。"""
    return _runner().do_round()


def live_state() -> dict:
    """期权页轮询的完整状态（字段与接线前完全一致）。

    方案 B 后状态有两个来源：策略线程写 ``okx_options_live_state``（轮次结果），
    runner 写自己那份 ``_state``（线程/错误）。此处合并，页面字段不变。
    """
    cfg = live_config()
    r = _runner()
    with r._lock:
        st = dict(r._state)
    try:
        from . import okx_options_live_state as _lst
        snap = _lst.snapshot()
    except Exception:  # noqa: BLE001
        snap = {}
    totals = snap.get("totals") or st.get("totals") or {}
    return {
        "config": cfg,
        "running": bool(r._thread is not None and r._thread.is_alive()),
        "last_run": st.get("last_run") or snap.get("round_ts"),
        "last_settled": snap.get("settled") or st.get("last_settled") or [],
        "last_strategy": snap.get("strategy") or st.get("last_strategy"),
        "last_error": st.get("last_error") or snap.get("error") or "",
        "total_settled": int(totals.get("settled") or 0),
        "total_entries": int(totals.get("entries") or 0),
        "total_exits": int(totals.get("exits") or 0),
    }


def stop() -> dict:
    """停止循环（幂等）。不打断正在执行的一轮——等它自然结束。"""
    return _runner().stop()


def sync() -> dict:
    """按 option_params live 配置启/停/重启（幂等）。

    运行中且配置未变 → 不动；运行中配置变（interval / strategy / enabled）→ 重启；
    enabled=false → 停。
    """
    _runner().sync()
    return live_state()


__all__ = [
    "DEFAULT_STRATEGY", "DEFAULT_INTERVAL_S", "MIN_INTERVAL_S", "MAX_INTERVAL_S",
    "LIVE_EVENTS_NAME",
    "live_config", "save_live_config",
    "run_once", "live_state", "sync", "stop",
    "events_path", "load_events",
]

# 旧名（接线前为模块级常量，可能有外部引用）
LIVE_EVENTS_NAME = _LIVE_EVENTS_NAME
