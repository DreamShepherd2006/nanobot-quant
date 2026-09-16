"""通用 live runner 基类（模板方法）—— E 期期权接线时从 okx_options_live 抽出。

背景（docs/quant-system.md §33.22）：期权策略线要与 TD live **尽可能同构**，
避免两套写法。TD live 的机制层（``_TdLiveRunner``）与期权线的机制层（原先在
``okx_options_live`` 里独立写了一份）本质相同，因此这里把通用骨架抽成基类：

- daemon 线程 + stop_event 生命周期
- **优雅停止**：绝不强行中断正在跑的业务轮，等当前轮自然结束再退出
- ``sync()`` 幂等启停/重启（配置变化 → 重启线程）
- 进程内 LIVE_STATE（``status()``）
- append-only 事件文件（跨重启保留）
- 诊断日志走 stderr（两条历史教训见下）

**与 td_live 的关系（2026-09-16 用户拍板）**：本基类先立起来给期权线用，
``td_live`` 本次一个字不动；将来单独一个 PR 把 ``_TdLiveRunner`` 迁移过来，
避免现货线承担任何回归风险。

子类需提供：

===========================  ==================================================
``LOG_PREFIX``               诊断日志前缀（如 ``[OPT-LIVE]``）
``EVENTS_NAME``              事件文件名（落在子类的 ``storage_dir()`` 下）
``load_config()``            读本线配置（含 ``enabled`` / ``interval_s``）
``config_changed(o, n)``     配置是否变化（变化 → 重启线程）
``do_round()``               单轮业务；返回 dict 写进 ``last_result``
``storage_dir()``            持久化目录
===========================  ==================================================

两条历史教训（务必保留）：

1. **gatekeeper 进程没有配置 logging handler** —— ``logging.INFO`` 及以下会被
   Python 的 lastResort handler 静默丢弃（只有 WARNING+ 进 stderr）。诊断输出
   一律 ``print(..., file=sys.stderr, flush=True)``。
2. **launch.sh 会捕获 Python 子进程的 stdout 并 eval 执行** —— 任何 stdout
   输出都会被当成 shell 命令，绝不可 ``print()`` 到 stdout。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# 优雅停止：等待当前业务轮结束的最长时间（秒）。超时不强杀，只放弃等待。
STOP_WAIT_TIMEOUT = 90.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LiveRunnerBase:
    """daemon runner 骨架。子类只需实现 ``do_round()`` 等少数钩子。"""

    LOG_PREFIX = "[LIVE]"
    EVENTS_NAME = "live_events.jsonl"
    DEFAULT_INTERVAL_S = 60
    INTERVAL_RANGE = (10, 3600)

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # 优雅停止用：业务轮执行期间为 True（finally 复位）
        self._round_active = False
        self._cfg: dict = {}
        self._state: dict[str, Any] = {
            "running": False,
            "started_at": None,
            "last_run": None,
            "last_error": None,
            "last_result": None,
            "total_rounds": 0,
            "totals": {},
        }

    # ══════════════════════ 子类钩子 ══════════════════════
    def load_config(self) -> dict:
        """读本线配置（含 enabled / interval_s）。"""
        raise NotImplementedError

    def config_changed(self, old: dict, new: dict) -> bool:
        """配置是否变化——变化时 sync() 会重启线程以套用新配置。"""
        raise NotImplementedError

    def do_round(self) -> dict:
        """单轮业务。异常由基类兜住（记 last_error，下轮继续）。"""
        raise NotImplementedError

    def storage_dir(self) -> Path:
        """持久化目录；默认当前工作目录（子类应覆盖）。"""
        return Path.cwd()

    # ══════════════════════ 配置便捷读取 ══════════════════════
    def _enabled(self, cfg: dict) -> bool:
        return bool((cfg or {}).get("enabled"))

    def _interval_s(self, cfg: dict) -> int:
        lo, hi = self.INTERVAL_RANGE
        try:
            v = int((cfg or {}).get("interval_s") or self.DEFAULT_INTERVAL_S)
        except (TypeError, ValueError):
            v = self.DEFAULT_INTERVAL_S
        return max(lo, min(hi, v))

    # ══════════════════════ 日志 / 事件 ══════════════════════
    def _log(self, msg: str) -> None:
        """诊断日志 —— **必须走 stderr**（见模块 docstring 两条教训）。"""
        print(f"{self.LOG_PREFIX} {msg}", file=sys.stderr, flush=True)

    def events_path(self) -> Path:
        return self.storage_dir() / self.EVENTS_NAME

    def _append_event(self, event: dict) -> None:
        """append-only 落盘；失败静默（事件是 UX 信息，不阻塞业务）。"""
        try:
            p = self.events_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            row = dict(event or {})
            row.setdefault("ts", _utc_now())
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def load_events(self, limit: int = 50) -> list[dict]:
        """读最近 ``limit`` 条事件（原始行，不做 enrich；子类可覆盖）。"""
        try:
            p = self.events_path()
            if not p.exists():
                return []
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()
            out: list[dict] = []
            for line in lines[-max(1, int(limit)):]:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
            return out
        except Exception:
            return []

    # ══════════════════════ 生命周期 ══════════════════════
    def start(self, cfg: Optional[dict] = None) -> dict:
        """起线程（已在跑则返回现状，幂等）。"""
        with self._lock:
            cfg = dict(cfg if cfg is not None else self.load_config() or {})
            self._cfg = cfg
            if self._thread and self._thread.is_alive():
                return {"ok": True, "started": False, "reason": "已在运行"}
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name=f"{type(self).__name__}-loop", daemon=True)
            self._thread.start()
            self._state.update({"running": True, "started_at": _utc_now(),
                                "last_error": None})
            self._log(f"循环已启动（interval={self._interval_s(cfg)}s）")
            return {"ok": True, "started": True}

    def stop(self) -> dict:
        """优雅停止：设停止位后**等当前业务轮自然结束**（不强杀）。

        与 td_live 同规则（2026-08-21 用户定稿）：绝不强行中断正在执行的业务轮
        —— 那涉及订单处理，风险大；两轮之间自然退出即可。
        """
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                self._state["running"] = False
                return {"ok": True, "stopped": False, "reason": "未在运行"}
            self._stop_event.set()
            thread = self._thread
        # 等当前轮结束（在锁外等待，避免阻塞 status()）
        deadline = time.time() + STOP_WAIT_TIMEOUT
        while time.time() < deadline and getattr(self, "_round_active", False):
            time.sleep(0.2)
        if thread.is_alive():
            thread.join(timeout=5.0)
        alive = thread.is_alive()
        with self._lock:
            if not alive:
                self._thread = None
            self._state["running"] = alive
        self._log(f"循环已停止（thread_alive={alive}）")
        return {"ok": True, "stopped": not alive, "thread_alive": alive}

    def sync(self, cfg: Optional[dict] = None) -> dict:
        """幂等启停/重启：配置驱动的唯一入口（保存参数 / 页面操作后调用）。

        - 应运行且未运行 → 启动
        - 运行中且配置变化 → 重启（先优雅停，再启）
        - 运行中且应停用 → 停止
        """
        cfg = dict(cfg if cfg is not None else self.load_config() or {})
        should = self._enabled(cfg)
        running = bool(self._state.get("running"))
        alive = bool(self._thread and self._thread.is_alive())
        with self._lock:
            old = dict(self._cfg)
        changed = bool(old) and self.config_changed(old, cfg)

        if should and (not alive or changed):
            if alive:
                self._log("配置变化 → 重启循环")
                self.stop()
            return self.start(cfg)
        if not should and (alive or running):
            self._log("配置停用 → 停止循环")
            return self.stop()
        if should and alive:
            with self._lock:
                self._cfg = cfg
        return {"ok": True, "started": False, "stopped": False, "unchanged": True}

    # ══════════════════════ 主循环 ══════════════════════
    def run_forever(self) -> None:
        """主循环。默认实现 = interval 心跳 + :meth:`_tick`。

        子类可覆盖以换调度形态（如把节拍交给 lumibot engine 的长驻模式）。
        覆盖时**必须响应 ``self._stop_event``**，否则 :meth:`stop` 只能等超时。
        """
        while not self._stop_event.is_set():
            self._tick()
            if self._stop_event.wait(timeout=self._interval_s(self._cfg)):
                break

    def _tick(self) -> None:
        """单轮：标 _round_active（优雅停止依赖它）→ do_round() → 记录状态。"""
        self._round_active = True
        try:
            result = self.do_round()
            with self._lock:
                self._state["last_run"] = _utc_now()
                self._state["last_result"] = result
                self._state["total_rounds"] = int(
                    self._state.get("total_rounds") or 0) + 1
        except Exception as e:  # 单轮异常不外抛：记 last_error，下轮继续
            with self._lock:
                self._state["last_error"] = f"{type(e).__name__}: {e}"
            self._log(f"单轮异常（下轮继续）：{type(e).__name__}: {e}")
        finally:
            self._round_active = False

    def _loop(self) -> None:
        try:
            self.run_forever()
        except Exception as e:  # 主循环异常：记账并退出（不静默）
            with self._lock:
                self._state["last_error"] = f"{type(e).__name__}: {e}"
            self._log(f"主循环异常退出：{type(e).__name__}: {e}")
        with self._lock:
            self._state["running"] = False
        self._log("循环线程退出")

    # ══════════════════════ 状态 ══════════════════════
    def _bump(self, key: str, n: int = 1) -> None:
        """通用计数（子类 do_round 里随手累计）。"""
        with self._lock:
            totals = self._state.setdefault("totals", {})
            totals[key] = int(totals.get(key) or 0) + int(n)

    def status(self) -> dict:
        with self._lock:
            st = dict(self._state)
            st["totals"] = dict(self._state.get("totals") or {})
            st["interval_s"] = self._interval_s(self._cfg)
            st["thread_alive"] = bool(self._thread and self._thread.is_alive())
        return st


__all__ = ["LiveRunnerBase", "STOP_WAIT_TIMEOUT"]
