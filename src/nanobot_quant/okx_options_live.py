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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import okx_options_trade as ot
from .live_runner_base import STOP_WAIT_TIMEOUT, LiveRunnerBase

_LIVE_EVENTS_NAME = "okx_options_live_events.jsonl"

# 停止收尾（与 td_live 同构，2026-09-25 实测定稿）：
#   STOP_WAIT_TIMEOUT（基类常量 90s）= 等当前业务轮自然结束的上限；
#   STOP_JOIN_WAIT_S = start() 前等旧线程退出的上限（等不到即 fail-closed）。
STOP_JOIN_WAIT_S = 15.0

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
    "take_profit_pct": 50,               # put 线权利金回落止盈线（%）
    "max_contracts_per_family": 1,
    "max_contracts_total": 3,
    # ── 卖 call（covered call）支线（§33.40）—— 与 put 参数互不影响 ──
    "put_enabled": True,                 # put 线总开关（便于只跑 call 线验证）
    "call_enabled": False,               # call 线总开关（默认关，用户在页面手动开）
    "take_profit_pct_call": 30,          # call 线止盈线（上行无界、快落袋）
    "max_calls_per_family": 1,           # 单家族在仓 call 张数上限
    "max_calls_total": 2,                # 全局在仓 call 张数上限
    "allow_no_cost_basis": False,        # 无成本锚 C 时是否放行（台账标「无成本锚」）
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
        self._stopping = False   # 优雅停止线程是否在跑（防重复 stop 起多个）

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

        **不在整段生命周期置 ``_round_active=True``**（2026-09-25 实测修正）：
        基类 ``stop()`` 会据此等满超时再 join，实际永远等不到 → 「循环已停止
        (thread_alive=True)」、且 ``start()`` 见 alive=True 直接「已在运行」
        ⇒ 停一次就再也起不来，只能重启空间。
        改为逐轮由策略侧 ``_iteration_active`` 表达「当前轮是否在跑」，
        :meth:`stop` 据此等当前轮自然结束（绝不强行中断业务轮）。
        """
        self._build_executor()
        self._executor.run()

    def stop(self) -> dict:
        """优雅停止（幂等、立即返回，页面不受影响）。

        ① 后台等当前业务轮自然结束（策略 ``_iteration_active``，90s 兜底，
           **绝不强行中断正在执行的一轮**）
        ② 置 ``parameters["stop_requested"]`` + **``executor.stop_event``** ——
           lumibot 主循环 ``_should_continue_trading_loop`` 感知 stop_event 后
           break → ``executor.run()`` 返回 → 线程退出
        ③ 清 scheduler（remove_all_jobs + ``shutdown(wait=False)`` + None），
           防主循环收尾前重建的 scheduler 继续调度孤儿 job

        **不调** ``executor.stop()`` —— lumibot 内部 ``shutdown(wait=True)`` 会等
        业务轮收尾，遇网络卡死即永久挂住（TD live 已踩过）。

        历史教训：上一版靠策略 ``raise SystemExit`` 退出 —— 异常被 APScheduler
        的 job 层捕获记日志、主循环照跑（2026-09-25 实测复现「循环已停止
        (thread_alive=True)」+ 每 60s 空转抛一次 traceback）。
        """
        with self._lock:
            self._stop_event.set()
            self._state["running"] = False
            executor, strategy = self._executor, self._strategy
            alive = bool(self._thread is not None and self._thread.is_alive())
            if not alive:
                self._stopping = False
            elif self._stopping:
                return {"ok": True, "stopping": True, "thread_alive": True}
            else:
                self._stopping = True
        if not alive:
            self._teardown_executor(executor, strategy)
            self._log("循环未在运行（无需停止）")
            return {"ok": True, "stopping": False, "thread_alive": False}
        threading.Thread(
            target=self._graceful_stop, args=(executor, strategy),
            daemon=True, name="opt-live-stop",
        ).start()
        self._log("已请求停止（等当前轮自然结束后退出；页面不受影响）")
        return {"ok": True, "stopping": True, "thread_alive": True}

    def _teardown_executor(self, executor, strategy) -> None:
        """停止信号 + scheduler 清理（幂等、可重入）。"""
        try:
            if strategy is not None:
                strategy.parameters["stop_requested"] = True
        except Exception as e:  # noqa: BLE001
            self._log(f"⚠️ 停止位设置失败：{type(e).__name__}: {e}")
        try:
            ev = getattr(executor, "stop_event", None)
            if ev is not None:
                ev.set()
        except Exception as e:  # noqa: BLE001
            self._log(f"⚠️ stop_event 设置失败：{type(e).__name__}: {e}")
        try:
            sched = getattr(executor, "scheduler", None)
            if sched is not None:
                sched.remove_all_jobs()
                sched.shutdown(wait=False)
            if executor is not None:
                executor.scheduler = None
        except Exception as e:  # noqa: BLE001
            self._log(f"⚠️ scheduler 清理失败：{type(e).__name__}: {e}")

    def _graceful_stop(self, executor, strategy) -> None:
        """后台停止：等当前轮自然结束 → 发停止信号 → 收尾（不阻塞页面）。"""
        deadline = time.monotonic() + STOP_WAIT_TIMEOUT
        waited = False
        while time.monotonic() < deadline:
            if strategy is None or not getattr(strategy, "_iteration_active", False):
                break
            waited = True
            time.sleep(0.2)
        if waited and time.monotonic() >= deadline:
            self._log(f"⚠️ 等当前轮结束超时（{STOP_WAIT_TIMEOUT:.0f}s）"
                      "——仍发停止信号（本轮跑完才退出）")
        self._teardown_executor(executor, strategy)
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5.0)
        alive = bool(t is not None and t.is_alive())
        with self._lock:
            if not alive:
                self._thread = None
            self._state["running"] = False
            self._stopping = False
        self._log(f"循环已停止（thread_alive={alive}）")

    def start(self, cfg: Optional[dict] = None) -> dict:
        """起线程前先等旧循环退出（stop 后 lumibot 收尾需数百毫秒~数秒）。

        基类 ``start`` 的 ``is_alive`` 守卫会把新配置静默吞掉（页面只显示
        「已在运行」、参数不生效）——这里等不到就 fail-closed 明确报错。
        """
        if not self._wait_thread_exit(STOP_JOIN_WAIT_S):
            msg = f"上一轮循环尚未退出（等 {STOP_JOIN_WAIT_S:.0f}s 超时），请稍后重试"
            self._log(f"⚠️ {msg}")
            with self._lock:
                self._state["last_error"] = msg
            return {"ok": False, "started": False, "reason": msg}
        return super().start(cfg)

    def sync(self, cfg: Optional[dict] = None) -> dict:
        """比基类多一层：正在退出中的循环视为「未运行」，等它退完再起。"""
        cfg = dict(cfg if cfg is not None else (self.load_config() or {}))
        if self._enabled(cfg) and self._stop_event.is_set():
            self._wait_thread_exit(STOP_JOIN_WAIT_S)
        return super().sync(cfg)

    def _wait_thread_exit(self, timeout: float) -> bool:
        """轮询等旧线程退出；返回是否已退出。"""
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            t = self._thread
            if t is None or not t.is_alive():
                return True
            time.sleep(0.2)
        t = self._thread
        return t is None or not t.is_alive()

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
        params = {
            **dict(OkxOptionsPutStrategy.parameters),
            **strat_cfg,
            "live_mode": True,
            "stop_requested": False,
        }
        strategy.parameters = params
        print(
            f"[DIAG] 期权 runner: account={account or '(default)'} "
            f"sleeptime={sleeptime} families={params.get('families')} "
            f"dry_run={params.get('dry_run')} "
            f"put={'on' if params.get('put_enabled', True) else 'off'} "
            f"call={'on' if params.get('call_enabled') else 'off'} "
            f"entry=setup{params.get('entry_setup')}/cd{params.get('entry_countdown')} "
            f"tp={params.get('take_profit_pct')}%/tp_call={params.get('take_profit_pct_call')}%",
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
        # 「循环在运行」= 状态位未清 且 线程活着：stop() 会立即清状态位 → 页面
        # 随即显示「已停止」，而线程仍在后台把当前轮跑完再退出（不打断业务轮）。
        "running": bool(st.get("running") and r._thread is not None
                        and r._thread.is_alive()),
        "last_run": st.get("last_run") or snap.get("round_ts"),
        "last_settled": snap.get("settled") or st.get("last_settled") or [],
        "last_strategy": snap.get("strategy") or st.get("last_strategy"),
        "last_error": st.get("last_error") or snap.get("error") or "",
        "total_settled": int(totals.get("settled") or 0),
        "total_entries": int(totals.get("entries") or 0),
        "total_exits": int(totals.get("exits") or 0),
        "total_call_entries": int(totals.get("call_entries") or 0),
        "total_call_exits": int(totals.get("call_exits") or 0),
    }


def stop() -> dict:
    """停止循环（幂等）。不打断正在执行的一轮——等它自然结束。"""
    return _runner().stop()


def sync() -> dict:
    """按 option_params live 配置启/停/重启（幂等）。

    运行中且配置未变 → 不动；运行中配置变（interval / strategy / enabled）→ 重启；
    enabled=false → 停。

    同时同步**盘口采集器**（研究用只读采集，见 option_tape）：两者共用一个启停入口
    （期权页保存），但各自独立线程、各自 enabled 门控 —— 采集失败不影响策略循环。
    """
    _runner().sync()
    try:
        from . import option_tape as _tape
        _tape.sync()
    except Exception as e:  # noqa: BLE001 —— 采集器不得拖垮策略循环
        print(f"[OPT-LIVE] 盘口采集同步失败（不影响策略循环）："
              f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
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
