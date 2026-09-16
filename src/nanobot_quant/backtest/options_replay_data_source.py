"""Options replay data source —— 期权回测数据层（E 期步 3）。

与现货 ``backtest/replay_data_source.py``（``ReplayDataSource``）**同构**：
预拉全量历史 → ``seek(ts)`` 显式定位 → 每次 ``get_historical_prices`` 返回
``[ts−length+1, ts]`` 最近窗口。区别在于期权线除了标的 K 线，还要承载：

  ① 标的 K 线（OKX 现货成交价 —— 与期权链/策略/td_kline 同源，见「期权线一律取 OKX」）
  ② 候选合约枚举（按到期日 × strike 带，instId 规整可推算）
  ③ 每合约 mark 价全生命周期（复用 ``okx_options_data.fetch_lifecycle``）

驱动在每个 bar 上向数据源问三件事：

    price_of(family)        → 标的当前价（TD 信号 + 选档用）
    chain_at(ts)            → 该时刻「在售」合约及 mark 价（选档用）
    premium_of(inst_id)     → 某合约当前 mark（撮合 / 止盈判断用）

设计原则（对齐现货）：**历史数据全离线、零网络轮询、确定性**——网络只发生在
``prefetch()``；驱动重放阶段纯内存。

时间语义：所有 ts 为 **UTC 秒**（mark 记录为毫秒）；K 线索引 tz-aware UTC。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from nanobot_quant.bs_pricing import bs_delta, implied_vol, years_to_expiry
from nanobot_quant.okx_options_data import (
    FAMILIES,
    _OKX_BAR_MAP,
    _OKX_BAR_UNAVAILABLE,
    _ref_inst,
    fetch_lifecycle,
)

_DEFAULT_BAR = "15m"

# 合约枚举默认参数：strike 带 ±20%、按 1 美元步进（SOL 档位实测为整数档）
_DEFAULT_STRIKE_PCT = 0.20
_DEFAULT_STRIKE_STEP = 1.0

# 枚举上限：防止 strike 带 × 到期日 组合爆炸（超限截断并记 note）
_MAX_CONTRACTS = 400

# 到期日枚举尾窗（天）：区间末尾持有的 put 往往在 end_ts 之后才到期
# （选档要求剩 3–7 天），若只枚举到 end_ts，回测尾部将无链可卖。
_ENUM_TAIL_DAYS = 7

# 合约并发拉取数（网络密集；单合约失败不阻塞）
_PREFETCH_WORKERS = 6

_OKX_BARS = ("1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H",
             "6H", "12H", "1D", "3D", "1W", "1M")

# lumibot timestep → OKX bar（与现货数据源口径一致）
_LUMIBOT_BAR = {
    "minute": "1m", "1min": "1m",
    "3min": "3m", "5min": "5m", "15min": "15m", "30min": "30m",
    "hour": "1H", "2hour": "2H", "4hour": "4H", "6hour": "6H", "12hour": "12H",
    "day": "1D", "3day": "3D", "week": "1W", "month": "1M",
}


def _to_ts(value) -> Optional[int]:
    """区间时间戳归一化：None / unix 秒 / datetime / 'YYYY-MM-DD[ HH:MM]' → int 秒（UTC）。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    if isinstance(value, str):
        s = value.strip()
        fmt = "%Y-%m-%d" if len(s) <= 10 else "%Y-%m-%d %H:%M"
        return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
    raise TypeError(f"无法解析回测时间戳: {value!r}")


def _resolve_bar(timestep: str) -> str:
    """timestep（lumibot 名 / 统一周期名 / 'bar:' 前缀）→ OKX bar 名（fail-closed）。

    注意大小写：lumibot 名全小写（``"hour"``/``"15min"``），
    OKX bar 名带大写单位（``"1H"``/``"1D"``）——先按小写查 lumibot 表，
    未命中时保留原始大小写再查 OKX 表。
    """
    raw = str(timestep or "").strip().removeprefix("bar:")
    if not raw:
        return _DEFAULT_BAR
    bar = _LUMIBOT_BAR.get(raw.lower(), raw)
    if bar in _OKX_BAR_UNAVAILABLE:
        raise ValueError(_OKX_BAR_UNAVAILABLE[bar])
    if bar not in _OKX_BARS:
        raise ValueError(f"不支持的回测周期: {timestep!r}")
    return _OKX_BAR_MAP.get(bar, bar)


def _fmt_strike(strike: float) -> str:
    """strike 格式化：整数不带小数（101 而非 101.0）。"""
    return str(int(strike)) if float(strike).is_integer() else str(strike)


def _bar_seconds(bar: str) -> int:
    from nanobot_quant.data_sources.periods import INTERVAL_SECONDS
    return int(INTERVAL_SECONDS.get(bar, 60))


class OptionsReplayDataSource:
    """Deterministic historical replay data source for options backtests.

    Parameters:
        family: 期权家族（如 ``"SOL-USD_UM"``）。
        timestep: 周期（``"5m"`` / ``"15min"`` / ``"1H"`` …，映射 OKX bar）。
        start_ts / end_ts: 回测区间（unix 秒；缺省 = 标的可用历史全量）。
        length: 策略窗口长度（默认 120 = min_history）。
        strike_pct: strike 枚举带（现价 ±pct，默认 0.20）。
        strike_step: strike 步进（默认 1.0）。
        opt_types: 合约类型（默认只枚举 put）。
        fetcher: 标的 K 线注入点 ``(inst_id, bar, start_ts, end_ts) -> DataFrame``。
        lifecycle_fetcher: 合约生命周期注入点 ``(inst_id, bar) -> dict``。

    ``fetcher`` / ``lifecycle_fetcher`` 为单测注入点（不打网络），
    与现货 ``ReplayDataSource.fetcher`` 同款设计。
    """

    SOURCE = "backtest-options"

    # 历史数据全为已收盘 bar —— 无「进行中 bar」概念（对齐现货回测契约）
    drops_in_progress_bars = False

    def __init__(
        self,
        family: str,
        timestep: str = _DEFAULT_BAR,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        length: int = 120,
        strike_pct: float = _DEFAULT_STRIKE_PCT,
        strike_step: float = _DEFAULT_STRIKE_STEP,
        opt_types: tuple[str, ...] = ("P",),
        fetcher: Optional[Callable[[str, str, int, int], pd.DataFrame]] = None,
        lifecycle_fetcher: Optional[Callable[[str, str], dict]] = None,
    ):
        if family not in FAMILIES:
            raise ValueError(f"未知标的家族 {family}，可选 {FAMILIES}")
        self._family = family
        self._timestep = str(timestep)
        self._bar = _resolve_bar(timestep)
        self._user_start_ts = _to_ts(start_ts)
        self._end_ts = _to_ts(end_ts)
        self._length = max(1, int(length))
        self._strike_pct = max(0.0, float(strike_pct))
        self._strike_step = max(0.0, float(strike_step))
        self._opt_types = tuple(t.upper() for t in opt_types) or ("P",)

        self._ref_inst, self._ref_kind = _ref_inst(family)
        if not self._ref_inst:
            raise ValueError(f"家族 {family} 无参考标的，无法回测")

        # prefetch 起点前移 (length−1) 根：TD 计数窗口覆盖更早的重置点
        # （与现货回测 2026-08-28 的预热对齐增强同款）
        self._prefetch_start_ts = self._user_start_ts
        if self._user_start_ts is not None:
            self._prefetch_start_ts = (
                self._user_start_ts - (self._length - 1) * _bar_seconds(self._bar)
            )

        self._fetcher = fetcher or self._default_fetch
        self._lifecycle_fetcher = lifecycle_fetcher or (
            lambda inst_id, bar: fetch_lifecycle(inst_id, bar)
        )

        self._underlying: Optional[pd.DataFrame] = None
        self._bar_times: list = []
        self._current_ts = None
        self._contracts: dict[str, dict] = {}
        self._premiums: dict[str, dict[int, float]] = {}
        self.notes: list[str] = []

    # ── 数据预拉 ──────────────────────────────────────────────

    def _default_fetch(self, inst_id: str, bar: str, start_ts: int,
                       end_ts: int) -> pd.DataFrame:
        """标的 K 线区间拉取（OKX history-candles 分页，after = 往更早翻）。"""
        from nanobot_quant import okx_sdk

        out: dict[int, list] = {}
        after = ""
        for _ in range(60):                       # 300 根/页 × 60 页上限
            rows = okx_sdk.check(
                okx_sdk.market().get_history_candles(
                    instId=inst_id, bar=bar, after=after, limit="300"
                )
            ) or []
            if not rows:
                break
            for r in rows:
                out.setdefault(int(r[0]), r)
            oldest = min(out)
            if start_ts and oldest <= int(start_ts) * 1000:
                break
            after = str(oldest)
        if not out:
            return pd.DataFrame()
        rows_sorted = [out[k] for k in sorted(out)]
        return pd.DataFrame(
            {
                "open": [float(r[1]) for r in rows_sorted],
                "high": [float(r[2]) for r in rows_sorted],
                "low": [float(r[3]) for r in rows_sorted],
                "close": [float(r[4]) for r in rows_sorted],
                "volume": [float(r[5]) for r in rows_sorted],
            },
            index=pd.to_datetime([int(r[0]) for r in rows_sorted], unit="ms", utc=True),
        )

    def prefetch(self) -> None:
        """拉全量历史：① 标的 K 线 → ② 合约枚举 → ③ 每合约 mark。"""
        end_ts = self._end_ts or int(time.time())
        start_ts = self._prefetch_start_ts or 0

        # ① 标的 K 线
        try:
            df = self._fetcher(self._ref_inst, self._bar, start_ts, end_ts)
        except Exception as exc:  # noqa: BLE001
            self.notes.append(f"标的 K 线拉取失败: {type(exc).__name__}: {exc}")
            df = pd.DataFrame()
        if df is None or df.empty:
            self.notes.append("标的 K 线为空：区间内无可用历史（检查周期/区间/标的）")
            df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        else:
            # 本地裁剪到 [prefetch_start, end]——分页按页边界会多拉
            lo = pd.Timestamp(self._prefetch_start_ts or 0, unit="s", tz="UTC")
            hi = pd.Timestamp(int(self._end_ts or time.time()), unit="s", tz="UTC")
            before = len(df)
            df = df.sort_index().loc[lo:hi]
            if len(df) != before:
                self.notes.append(
                    f"标的 K 线裁剪：{before} → {len(df)} 根（区间外丢弃 {before - len(df)}）")
        self._underlying = df.rename(columns=str.lower).sort_index()
        self._bar_times = list(self._underlying.index)
        if not self._bar_times:
            return

        # ② 合约枚举（strike 带 = 区间内标的价 ±pct）
        self._enumerate_contracts(start_ts, end_ts)

        # ③ 每合约 mark（并发；单合约失败只记录）
        if self._contracts:
            self._prefetch_premiums()

    def _enumerate_contracts(self, start_ts: int, end_ts: int) -> None:
        """按 instId 规则枚举回测区间内的候选合约。

        OKX 期权 **每日到期**（08:00 UTC），instId 形如
        ``SOL-USD_UM-260906-101-P``（yymmdd / strike / P|C），故可推算。
        strike 带 = 区间内标的价 [min×(1−pct), max×(1+pct)]，按 step 取整。
        """
        base = self._family.split("-")[0]
        closes = self._underlying["close"].astype(float)
        if closes.empty:
            return
        lo_px = float(closes.min()) * (1.0 - self._strike_pct)
        hi_px = float(closes.max()) * (1.0 + self._strike_pct)

        step = self._strike_step or 1.0
        strikes: list[float] = []
        k = int(lo_px / step)
        while k * step <= hi_px:
            if k > 0:
                strikes.append(round(k * step, 6))
            k += 1

        # 到期日：区间内每天 08:00 UTC。上限延伸 _ENUM_TAIL_DAYS 天 —— 区间末尾
        # 持有的 put 常在 end_ts 之后才到期，只枚举到 end_ts 会让回测尾部无链可卖。
        day = 86400
        t = ((start_ts or 0) // day) * day
        expiries: list[int] = []
        limit_ts = (self._end_ts or int(time.time())) + _ENUM_TAIL_DAYS * day
        while t <= limit_ts:
            exp_ms = (t + 8 * 3600) * 1000
            if exp_ms > (start_ts or 0) * 1000:
                expiries.append(exp_ms)
            t += day

        truncated = False
        for exp_ms in expiries:
            dt = datetime.fromtimestamp(exp_ms // 1000 - 8 * 3600, tz=timezone.utc)
            yymmdd = dt.strftime("%y%m%d")
            for strike in strikes:
                for opt in self._opt_types:
                    if len(self._contracts) >= _MAX_CONTRACTS:
                        truncated = True
                        break
                    inst = f"{base}-USD_UM-{yymmdd}-{_fmt_strike(strike)}-{opt}"
                    self._contracts[inst] = {
                        "inst_id": inst,
                        "exp_ms": exp_ms,
                        "strike": strike,
                        "opt_type": opt,
                        "family": self._family,
                    }
                if truncated:
                    break
            if truncated:
                break
        if truncated:
            self.notes.append(
                f"合约枚举达上限 {_MAX_CONTRACTS}（截断）：建议缩小区间或 strike 带"
            )

    def _prefetch_premiums(self) -> None:
        """并发拉取各合约 mark 生命周期，本地裁剪到回测区间。"""
        lo_ms = int(self._prefetch_start_ts or 0) * 1000
        hi_ms = int(self._end_ts or time.time()) * 1000
        ok_cnt = fail = 0
        errors: list[str] = []

        def _one(inst_id: str):
            life = self._lifecycle_fetcher(inst_id, self._bar)
            marks = {
                int(r["ts"]): float(r["mark_px"])
                for r in life.get("rows", [])
                if r.get("mark_px") is not None
            }
            return marks, life

        with ThreadPoolExecutor(max_workers=_PREFETCH_WORKERS) as pool:
            futs = {pool.submit(_one, i): i for i in self._contracts}
            for fut in as_completed(futs):
                inst_id = futs[fut]
                try:
                    marks, life = fut.result()
                except Exception as exc:  # noqa: BLE001 —— 失败原因分类留痕
                    fail += 1
                    if len(errors) < 3:
                        errors.append(f"{inst_id} → {type(exc).__name__}: {exc}")
                    continue
                win = {ts: px for ts, px in marks.items() if lo_ms <= ts <= hi_ms}
                if not win:
                    fail += 1
                    if len(errors) < 3:
                        errors.append(
                            f"{inst_id} → 区间内无 mark（全量 {len(marks)} 根）")
                    continue
                self._premiums[inst_id] = win
                self._contracts[inst_id]["lot_coin"] = life.get("lot_coin")
                self._contracts[inst_id]["list_ms"] = life.get("list_ms")
                ok_cnt += 1
        msg = f"合约 mark 预拉：成功 {ok_cnt} / 失败 {fail}（枚举 {len(self._contracts)}）"
        if errors:
            msg += "｜样本 " + " ｜ ".join(errors)
        self.notes.append(msg)

    # ── 驱动辅助 ─────────────────────────────────────────────

    @property
    def family(self) -> str:
        return self._family

    @property
    def ref_inst(self) -> str:
        return self._ref_inst

    @property
    def bar(self) -> str:
        return self._bar

    @property
    def bar_times(self) -> list:
        """标的完整时间轴（tz-aware UTC 升序）——驱动逐根推进。"""
        return self._bar_times

    @property
    def start_idx(self) -> int:
        """评估起点（预热窗口已前移；无 start 时退回 length−1）。"""
        if not self._bar_times:
            return 0
        if self._user_start_ts is None:
            return max(0, self._length - 1)
        for i, ts in enumerate(self._bar_times):
            if ts.timestamp() >= self._user_start_ts:
                return i
        return len(self._bar_times) - 1

    def seek(self, ts) -> None:
        """定位当前重放时间——标的窗口尾与合约权利金查询均对齐到 ``ts``。"""
        self._current_ts = ts

    def price_of(self, family: Optional[str] = None) -> float:
        """当前重放时间的标的收盘价。无数据 → 0.0（fail-closed）。"""
        df = self._underlying
        if df is None or df.empty or self._current_ts is None:
            return 0.0
        tail = df.loc[: self._current_ts]
        if tail.empty:
            return 0.0
        try:
            return float(tail["close"].iloc[-1])
        except (KeyError, IndexError, ValueError, TypeError):
            return 0.0

    def premium_of(self, inst_id: str, ts=None) -> Optional[float]:
        """某合约在 ``ts``（缺省=当前重放时间）的 mark 价；无数据 → None。

        mark 可能因粒度差异缺某个 bar → 回退到不超过 ts 的最近一笔。
        """
        marks = self._premiums.get(str(inst_id).upper())
        if not marks:
            return None
        t = ts or self._current_ts
        if t is None:
            return None
        t_ms = int(t.timestamp() * 1000) if isinstance(t, datetime) else int(t)
        if t_ms in marks:
            return marks[t_ms]
        prior = [k for k in marks if k <= t_ms]
        return marks[max(prior)] if prior else None

    def chain_at(self, ts=None, opt_type: Optional[str] = None) -> list[dict]:
        """该时刻「在售」合约及其 mark 价（选档输入）。

        在售 = ``list_ms ≤ ts < exp_ms`` 且该时刻有 mark 记录。按 (到期, strike) 升序。
        """
        t = ts or self._current_ts
        if t is None:
            return []
        t_ms = int(t.timestamp() * 1000) if isinstance(t, datetime) else int(t)
        want = opt_type.upper() if opt_type else None
        out: list[dict] = []
        for inst_id, meta in self._contracts.items():
            if want and meta.get("opt_type") != want:
                continue
            if not (meta.get("list_ms", 0) <= t_ms < meta.get("exp_ms", 0)):
                continue
            px = self.premium_of(inst_id, t)
            if px is None:
                continue
            out.append({**meta, "mark_px": px})
        out.sort(key=lambda r: (r["exp_ms"], r["strike"]))
        return out

    # ── 选档桥接（回测 ↔ 实盘共用 select_puts）────────────────

    def _lot_coin(self) -> float:
        """每张合约面值（币数）。

        取 ``FAMILY_LOT`` 家族常量（SOL=0.1、BTC/ETH/XAU=0.01）。取不到返回 0.0
        —— 调用方（select_puts）会记为 ``no_lot`` 并剔除，不静默当 1 张。
        """
        base = (self._family or "").split("-")[0].upper()
        if not base:
            return 0.0
        try:
            from nanobot_quant.okx_options_trade import FAMILY_LOT
            v = FAMILY_LOT.get(base)
            if v:
                return float(v)
        except Exception:  # pragma: no cover - 常量缺失时退化
            pass
        return 0.0

    def chain_dict_at(
        self,
        ts=None,
        opt_type: str = "P",
        expiry_min_days: Optional[float] = None,
        expiry_max_days: Optional[float] = None,
        slippage: float = 0.0,
    ) -> dict:
        """把该时刻的链快照转成 ``okx_options_select.select_puts`` 认的 chain dict。

        这是**回测与实盘共用同一份选档代码的唯一桥接层** —— 过滤/排序逻辑一行
        都不重写，回测侧只负责把历史 mark 还原成「链」。

        字段映射（回测没有盘口，一律以 mark 代理）：

        ==========  ==================================================
        ``bid``     ``mark × (1 − slippage)`` —— 模拟「买一价」。
                    选档与成交共用它，避免「选档看 mark、成交吃 bid」的口径分裂。
        ``ask``     ``mark × (1 + slippage)``
        ``iv``      从 mark 反解（与 OKX ``markVol`` 同口径：欧式/无股息/r≈0）
        ``delta``   BS 算（代入反解出的 IV）；反解失败 → None
        ``days``    ``(exp_ms − ts) / 86400000``
        ==========  ==================================================

        ``slippage`` 为小数（0.005 = 0.5%），默认 0 = 直接用 mark。
        缺 spot / 已到期 / 反解失败的合约一律剔除并计入 ``stats``
        —— 静默降级不可接受，调用方必须看得到剔了多少、为什么。
        """
        t = ts or self._current_ts
        lot = self._lot_coin()
        empty = {"spot": 0.0, "lot_coin": lot, "groups": [],
                 "stats": {"total": 0, "kept": 0, "expired": 0,
                           "no_spot": 1, "no_iv": 0}}
        if t is None:
            return dict(empty)
        t_ms = int(t.timestamp() * 1000) if isinstance(t, datetime) else int(t)
        spot = self.price_of()
        stats = {"total": 0, "kept": 0, "expired": 0, "no_spot": 0, "no_iv": 0}
        if spot <= 0:
            stats["no_spot"] = 1
            return {"spot": 0.0, "lot_coin": lot, "groups": [], "stats": stats}

        sl = max(0.0, float(slippage or 0.0))
        right = (opt_type or "P").upper()[:1]
        groups: dict[int, dict] = {}
        for c in self.chain_at(t, opt_type):
            stats["total"] += 1
            exp_ms = int(c.get("exp_ms") or 0)
            strike = float(c.get("strike") or 0)
            mark = float(c.get("mark_px") or 0)
            if exp_ms <= t_ms or strike <= 0 or mark <= 0:
                stats["expired"] += 1
                continue
            tv = years_to_expiry(t_ms, exp_ms)
            iv = implied_vol(mark, spot, strike, tv, right) if tv else None
            if iv is None:
                stats["no_iv"] += 1
                continue
            delta = bs_delta(spot, strike, tv, iv, 0.0, right)
            g = groups.get(exp_ms)
            if g is None:
                g = groups[exp_ms] = {
                    "days": (exp_ms - t_ms) / 86_400_000.0,
                    "exp_ms": exp_ms,
                    "date": datetime.fromtimestamp(
                        exp_ms // 1000, tz=timezone.utc).strftime("%y%m%d"),
                    "rows": [],
                }
            g["rows"].append({
                "strike": _fmt_strike(strike),
                right: {
                    "inst_id": c.get("inst_id"),
                    "bid": mark * (1.0 - sl),
                    "ask": mark * (1.0 + sl),
                    "iv": iv,
                    "delta": delta,
                    "mark_px": mark,
                },
            })
            stats["kept"] += 1

        out_groups = []
        for exp_ms in sorted(groups):
            g = groups[exp_ms]
            if expiry_min_days is not None and g["days"] < expiry_min_days:
                continue
            if expiry_max_days is not None and g["days"] > expiry_max_days:
                continue
            g["rows"].sort(key=lambda r: float(r["strike"]))
            out_groups.append(g)
        return {"spot": spot, "lot_coin": lot, "groups": out_groups,
                "stats": stats}

    def contracts(self) -> list[dict]:
        """全部预拉成功的合约（附 mark 覆盖 bar 数）。"""
        return [
            {**meta, "mark_bars": len(self._premiums.get(inst_id, {}))}
            for inst_id, meta in self._contracts.items()
            if inst_id in self._premiums
        ]

    # ── lumibot DataSource 接口 ──────────────────────────────

    def get_historical_prices(
        self,
        asset,
        length,
        timestep: str = "",
        timeshift=None,
        exchange=None,
        include_after_hours: bool = True,
        quote=None,
        return_polars: bool = False,
    ):
        """窗口 = [current_ts − length + 1, current_ts]（标的最近 N 根 bar）。"""
        from lumibot.entities import Bars

        df = self._underlying
        if df is None or df.empty or self._current_ts is None:
            empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            return Bars(empty, self.SOURCE, asset)
        return Bars(df.loc[: self._current_ts].tail(max(1, int(length))),
                    self.SOURCE, asset)

    def get_last_price(self, asset, quote=None, exchange=None):
        return self.price_of()

    def get_datetime(self, adjust_for_delay: bool = True):
        """当前重放时间（tz-aware UTC）。离线重放无延迟概念，忽略该参数。"""
        if self._current_ts is not None:
            return self._current_ts
        if self._bar_times:
            return self._bar_times[-1]
        return datetime.now(timezone.utc)

    def get_timestamp(self):
        return time.time()

    def get_timestep(self):
        return self._timestep


# ── 自检探针（真实拉数，用于验证数据层假设）─────────────────────────

def probe(
    family: str,
    timestep: str = _DEFAULT_BAR,
    days: int = 3,
    length: int = 120,
    strike_pct: float = _DEFAULT_STRIKE_PCT,
    end_ts: Optional[int] = None,
) -> dict:
    """期权回测数据层一次性诊断（**真实拉数**，非模拟）。

    回答两个未经实测的假设：
      ① OKX ``history-candles`` 分页能否按区间拉到标的 K 线
      ② 期权「每日到期 + 整数 strike」的 instId 推算是否成立（看 mark 命中率）

    命中率为 0 说明枚举规则需修正（例如实际按周到期 / strike 非整数档）。
    ``end_ts`` 缺省为当前时间；显式传入可诊断历史区间。
    """
    t0 = time.time()
    end = int(end_ts or t0)
    start = end - max(1, int(days)) * 86400
    out: dict = {
        "family": family,
        "timestep": timestep,
        "days": max(1, int(days)),
        "window": {"start": start, "end": end},
        "hypotheses": {},
    }

    try:
        ds = OptionsReplayDataSource(
            family=family, timestep=timestep,
            start_ts=start, end_ts=end,
            length=length, strike_pct=strike_pct,
        )
    except Exception as exc:  # noqa: BLE001 —— 探针不抛，错误进结果
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["bar"] = ds.bar
    out["ref_inst"] = ds.ref_inst

    try:
        ds.prefetch()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"prefetch 异常 {type(exc).__name__}: {exc}"
        out["notes"] = list(ds.notes)
        return out

    # ① 标的 K 线
    bt = ds.bar_times
    und: dict = {"bars": len(bt)}
    if bt and ds._underlying is not None and not ds._underlying.empty:
        closes = ds._underlying["close"].astype(float)
        und.update({
            "first": bt[0].isoformat(),
            "last": bt[-1].isoformat(),
            "first_close": round(float(closes.iloc[0]), 6),
            "last_close": round(float(closes.iloc[-1]), 6),
        })
    out["underlying"] = und
    out["hypotheses"]["okx_history_candles"] = bool(bt)

    # ② 合约枚举命中率
    enum_n = len(ds._contracts)
    ok_n = len(ds._premiums)
    out["contracts"] = {
        "enumerated": enum_n,
        "with_mark": ok_n,
        "hit_rate": round(ok_n / enum_n, 4) if enum_n else 0.0,
        "samples": list(ds._contracts)[:3],
    }
    out["hypotheses"]["daily_expiry_integer_strike"] = ok_n > 0

    # 区间中点时刻的「在售链」快照
    if bt:
        mid = bt[len(bt) // 2]
        ds.seek(mid)
        chain = ds.chain_at()
        out["chain_sample"] = {
            "ts": mid.isoformat(),
            "spot": ds.price_of(),
            "count": len(chain),
            "rows": [
                {"inst": c["inst_id"], "strike": c["strike"],
                 "mark": c["mark_px"], "exp_ms": c["exp_ms"],
                 "lot_coin": c.get("lot_coin")}
                for c in chain[:5]
            ],
        }

    out["notes"] = list(ds.notes)
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out


def _deltas_monotonic(pairs: list[tuple[float, float]]) -> Optional[bool]:
    """put delta 是否随 strike 单调。

    put delta **恒为负**，且随 strike 上升而**递减**
    （实测 −0.37@100 → −0.74@106）。首版按 call 的递增方向写，
    在真实数据上直接报 false —— 抽成纯函数 + 单测锁死方向。
    """
    if len(pairs) < 2:
        return None
    pairs = sorted(pairs)
    return all(pairs[i][1] >= pairs[i + 1][1] - 1e-9
               for i in range(len(pairs) - 1))


def probe_chain_dict(
    family: str,
    timestep: str = _DEFAULT_BAR,
    days: int = 3,
    length: int = 120,
    strike_pct: float = _DEFAULT_STRIKE_PCT,
    end_ts: Optional[int] = None,
) -> dict:
    """``chain_dict_at`` 真实性校验（**真实拉数**，只读）。

    单测里的 mark 是用 BS 自己生成的（σ 已知），只能证明「反解器自洽」，
    **证不了真实 mark 反解出来的 IV/delta 是否合乎期权市场**。

    这里用真实链跑一遍，回答：
      * 反解出的 IV 是否落在期权市场合理区间（SOL 实务约 0.3–1.5）
      * put delta 是否 ∈ (−1, 0)、且随 strike 单调
      * 选档窗口（剩 3–7 天）内是否真的有候选
    """
    t0 = time.time()
    end = int(end_ts or t0)
    start = end - max(1, int(days)) * 86400
    out: dict = {"family": family, "timestep": timestep,
                 "window": {"start": start, "end": end}}
    try:
        ds = OptionsReplayDataSource(
            family=family, timestep=timestep,
            start_ts=start, end_ts=end,
            length=length, strike_pct=strike_pct,
        )
    except Exception as exc:  # noqa: BLE001 —— 探针不抛
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["bar"] = ds.bar
    out["ref_inst"] = ds.ref_inst
    try:
        ds.prefetch()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"prefetch 异常 {type(exc).__name__}: {exc}"
        out["notes"] = list(ds.notes)
        return out

    bt = ds.bar_times
    out["bars"] = len(bt)
    out["contracts"] = {"enumerated": len(ds._contracts),
                        "with_mark": len(ds._premiums)}
    if not bt:
        out["error"] = "没有标的 K 线，无法选时刻"
        return out

    ts = bt[len(bt) // 2]
    ds.seek(ts)
    ch = ds.chain_dict_at(expiry_min_days=3, expiry_max_days=7)

    rows: list[dict] = []
    ivs: list[float] = []
    deltas: list[tuple[float, float]] = []
    for g in ch["groups"]:
        for r in g["rows"]:
            c = r["P"]
            if c["iv"] is not None:
                ivs.append(float(c["iv"]))
            if c["delta"] is not None:
                deltas.append((float(r["strike"]), float(c["delta"])))
            if len(rows) < 8:
                rows.append({
                    "inst": c["inst_id"], "strike": r["strike"],
                    "days": round(float(g["days"]), 3),
                    "mark": round(float(c["mark_px"]), 6),
                    "bid": round(float(c["bid"]), 6),
                    "iv": None if c["iv"] is None else round(float(c["iv"]), 4),
                    "delta": None if c["delta"] is None else round(float(c["delta"]), 4),
                })
    deltas.sort()
    mono = _deltas_monotonic(deltas)
    sane_iv = bool(ivs) and all(0.05 <= v <= 3.0 for v in ivs)
    out["chain_dict"] = {
        "ts": ts.isoformat(),
        "spot": ch["spot"],
        "lot_coin": ch["lot_coin"],
        "groups": len(ch["groups"]),
        "stats": ch["stats"],
        "iv_min": round(min(ivs), 4) if ivs else None,
        "iv_max": round(max(ivs), 4) if ivs else None,
        "iv_within_market_range": sane_iv,
        "delta_monotonic_in_strike": mono,
        "sample": rows,
    }
    out["ok"] = bool(ch["stats"]["kept"]) and sane_iv
    out["notes"] = list(ds.notes)
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out
