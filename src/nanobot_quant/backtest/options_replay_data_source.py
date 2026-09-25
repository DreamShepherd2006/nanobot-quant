"""Options replay data source —— 期权回测数据层（E 期步 3）。

与现货 ``backtest/replay_data_source.py``（``ReplayDataSource``）**同构**：
预拉全量历史 → ``seek(ts)`` 显式定位 → 每次 ``get_historical_prices`` 返回
``[ts−length+1, ts]`` 最近窗口。区别在于期权线除了标的 K 线，还要承载：

  ① 标的 K 线（OKX 现货成交价 —— 与期权链/策略/td_kline 同源，见「期权线一律取 OKX」）
  ② 归档逐笔成交（含已到期合约 —— 实时端点对它们全是 ``51001``）
  ③ 成交价反解 IV → 按到期建微笑 → BS 重定价任意档（``iv_surface``）

驱动在每个 bar 上向数据源问三件事：

    price_of(family)        → 标的当前价（TD 信号 + 选档用）
    chain_at(ts)            → 该时刻「在售」合约及理论价（选档用）
    premium_of(inst_id)     → 某合约当前理论价（撮合 / 止盈判断用）

为什么不能像现货那样直接拉历史价
--------------------------------
已到期 OKX 期权合约的一切实时端点都返回 ``51001``（``candles`` /
``mark-price-candles`` / ``ticker`` / ``history-trades`` 逐个实测），而回测的合约
全是已到期合约。唯一可用来源是官方每日归档，且归档里**只有成交** —— 所以历史价
只能反解 IV 后重定价，见 ``nanobot_quant.iv_surface``。

设计原则（对齐现货）：**历史数据全离线、零网络轮询、确定性**——网络只发生在
``prefetch()``；驱动重放阶段纯内存。

时间语义：所有 ts 为 **UTC 秒**（mark 记录为毫秒）；K 线索引 tz-aware UTC。
"""

from __future__ import annotations

import bisect
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from nanobot_quant.bs_pricing import bs_delta, quote_pair_from_iv, years_to_expiry
from nanobot_quant.iv_surface import (
    DEFAULT_IV_STALENESS_MS,
    IVSurface,
    iv_points_from_trades,
)
from nanobot_quant.okx_options_data import (
    FAMILIES,
    _OKX_BAR_MAP,
    _OKX_BAR_UNAVAILABLE,
    _ref_inst,
    parse_inst_id,
)
from nanobot_quant.options_history import contract_meta

_DEFAULT_BAR = "15m"

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
        opt_types: 合约类型（默认只看 put）。
        iv_mode: IV 口径（``hybrid`` 默认 / ``trade`` / ``smile``）。
        max_iv_staleness_ms: IV 观测保鲜期（默认 7 天 —— 远月档成交稀疏，窗口
            太短会让 3–7 天档的微笑为空）。
        fetcher: 标的 K 线注入点 ``(inst_id, bar, start_ts, end_ts) -> DataFrame``。
        archive_fetcher: 归档成交注入点 ``(start_ms, end_ms) -> list[dict]``。

    ``fetcher`` / ``archive_fetcher`` 为单测注入点（不打网络），
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
        opt_types: tuple[str, ...] = ("P",),
        iv_mode: str = "hybrid",
        max_iv_staleness_ms: int = DEFAULT_IV_STALENESS_MS,
        fetcher: Optional[Callable[[str, str, int, int], pd.DataFrame]] = None,
        archive_fetcher: Optional[Callable[[int, int], list[dict]]] = None,
    ):
        if family not in FAMILIES:
            raise ValueError(f"未知标的家族 {family}，可选 {FAMILIES}")
        self._family = family
        self._timestep = str(timestep)
        self._bar = _resolve_bar(timestep)
        self._user_start_ts = _to_ts(start_ts)
        self._end_ts = _to_ts(end_ts)
        self._length = max(1, int(length))
        self._opt_types = tuple(t.upper() for t in opt_types) or ("P",)
        self._iv_mode = str(iv_mode or "hybrid")
        self._max_iv_staleness_ms = int(max_iv_staleness_ms)

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
        self._archive_fetcher = archive_fetcher or self._default_archive_fetch

        self._underlying: Optional[pd.DataFrame] = None
        self._bar_times: list = []
        self._current_ts = None
        self._contracts: dict[str, dict] = {}
        self._surface: Optional[IVSurface] = None
        self._spot_ts: list[int] = []
        self._spot_px: list[float] = []
        self.notes: list[str] = []

    # ── 标的价索引 ────────────────────────────────────────

    def _index_spot(self) -> None:
        """标的收盘价索引（毫秒升序 + 平行价格数组）—— IV 反解按 ts 二分取价。"""
        idx = self._underlying.index
        self._spot_ts = [int(t.timestamp() * 1000) for t in idx]
        self._spot_px = [float(v) for v in self._underlying["close"].astype(float)]

    def _spot_at_ms(self, ts_ms: int) -> Optional[float]:
        """该毫秒时刻的标的收盘价（取 ≤ ts 的最近 bar）；无数据 → None。

        反解 IV 必须用「成交那一刻」的标的价 —— 用当前 bar 的价会直接污染 IV。
        """
        if not self._spot_ts:
            return None
        j = bisect.bisect_right(self._spot_ts, int(ts_ms)) - 1
        return self._spot_px[j] if j >= 0 else None

    def window_spot_stats(self) -> Optional[dict]:
        """评估区间的标的价统计：首 / 尾 / 最高 / 最低。

        回测报告里只写 ``SOL-USDT`` 这种标的代码，读的人无从判断那些 put
        到底虚值多少 —— 价格水平必须随报告一起给出。
        """
        u = self._underlying
        if u is None or u.empty or not self._bar_times:
            return None
        try:
            start, end = self._bar_times[self.start_idx], self._bar_times[-1]
        except (AttributeError, IndexError):
            return None
        w = u.loc[start:end]
        if w.empty:
            return None
        out = {"first": float(w["close"].iloc[0]),
               "last": float(w["close"].iloc[-1])}
        if "high" in w.columns:
            out["high"] = float(w["high"].max())
        if "low" in w.columns:
            out["low"] = float(w["low"].min())
        return out

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
        self._index_spot()

        # ② 归档成交 → IV 曲面（已到期合约的唯一可用来源）
        self._load_archive_iv(start_ts, end_ts)

    def _family_contracts(self) -> dict[str, dict]:
        """家族全部合约的元数据（在售 ∪ 已到期，含从未成交的档位）。"""
        from nanobot_quant import okx_options_data as od

        try:
            inst_ids = od.family_contracts(self._family)
        except Exception as exc:  # noqa: BLE001 —— 失败必须留痕
            self.notes.append(f"合约表拉取失败: {type(exc).__name__}: {exc}")
            return {}
        out: dict[str, dict] = {}
        for inst in inst_ids:
            meta = contract_meta(inst, family=self._family)
            if meta is None:
                continue
            out[meta["inst_id"].upper()] = {**meta, "inst_id": meta["inst_id"].upper()}
        self.notes.append(f"合约表：{len(out)} 个档位（在售 ∪ 已到期）")
        return out

    def _default_archive_fetch(self, start_ms: int, end_ms: int) -> list[dict]:
        """默认归档取数：``options_history``（本地缓存 + 流式解析）。"""
        from nanobot_quant import options_history as oh

        paths = oh.fetch_range(start_ms, end_ms, progress=self.notes.append)
        return oh.load_trades(paths, family=self._family)

    def _load_archive_iv(self, start_ts: int, end_ts: int) -> None:
        """归档逐笔成交 → IV 曲面（取代旧的「枚举 instId + 逐合约拉 mark」）。

        旧路径两头落空：自己推算出来的合约**大多不存在或已下架**（已到期合约的
        实时端点一律 51001），而真正有成交的合约又可能被 400 个枚举上限截断 ——
        实测枚举 400 个、只有 14 个拿到 mark。新路径反过来：**合约列表来自真实
        成交**，IV 由成交价反解，价格由 IV 重算。
        """
        lo_ms = int(start_ts or 0) * 1000
        hi_ms = int(end_ts or time.time()) * 1000

        try:
            trades = self._archive_fetcher(lo_ms, hi_ms)
        except Exception as exc:  # noqa: BLE001 —— 失败原因必须留痕，不静默降级
            self.notes.append(f"归档成交拉取失败: {type(exc).__name__}: {exc}")
            return
        if not trades:
            self.notes.append(
                "归档成交为空：区间内该家族无成交（检查区间/家族）")

        # ① 合约元数据：官方合约表（在售 ∪ 已到期），**不是**「有成交的合约」
        #    —— 归档成交太稀疏（SOL 日均 ~80 笔，摊到几十个档位后，策略要的
        #    「剩 3–7 天 + ≤ −5% OTM」交集常为空：实测 09-14 那一刻链里只有
        #    5 个档，全被到期窗口/距离门刷掉）。IV 仍只来自真实成交，未成交的
        #    档由同到期微笑插值补上（hybrid 口径）。
        contracts = self._family_contracts()
        if not contracts:
            self.notes.append("合约表为空：家族既无在售合约、也无历史到期记录")
            return
        if not trades:
            self.notes.append(
                f"归档成交为空：IV 全部缺失（合约表仍有 {len(contracts)} 个档）")
            self._contracts = contracts
            return

        # ② 反解 IV（需要成交那一刻的标的价）
        rejected: Counter = Counter()
        pts = iv_points_from_trades(
            trades, spot_at=self._spot_at_ms, contracts=contracts,
            on_reject=lambda why, _inst: rejected.update([why]))
        if not pts:
            # 合约仍然保留 —— 否则上层看不到「有链但一只都算不出 IV」，
            # 会被误读成「区间内无合约」（静默降级）。
            self._contracts = contracts
            self.notes.append(
                f"IV 反解全失败（{len(trades)} 笔 / {len(contracts)} 合约）："
                f"剔除统计 {dict(rejected)}")
            return

        # ③ 上市时间不做推断：``list_ms`` 保持 0（= 历史全程在售），链的有效区间由
        #    「``t < exp_ms`` 且该时刻能算出价格」共同界定。不构成前视 ——
        #    IVSurface 的 ``_latest_iv`` / ``smile_at`` 都只取 ts ≤ 当前时刻的观测点。

        self._contracts = contracts
        self._surface = IVSurface(self._contracts,
                                  max_iv_staleness_ms=self._max_iv_staleness_ms)
        self._surface.add_points(pts)
        msg = (f"IV 曲面：成交 {len(trades)} 笔 → 合约 {len(contracts)} 个 "
               f"（{len({p.exp_ms for p in pts})} 个到期）→ 反解成功 {len(pts)}")
        if rejected:
            msg += f"｜剔除 {dict(rejected)}"
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
        """某合约在 ``ts``（缺省=当前重放时间）的理论价；无 IV → None。

        对外语义与旧实现一致（一次价格查询），但数据来源已换：旧版查「逐合约
        mark 历史」，新版走「成交反解 IV → 微笑 → BS 重定价」—— 因为已到期
        合约的 mark 历史根本不存在（实时端点逐个实测均为 51001）。
        """
        if self._surface is None:
            return None
        t = ts or self._current_ts
        if t is None:
            return None
        t_ms = int(t.timestamp() * 1000) if isinstance(t, datetime) else int(t)
        spot = self._spot_at_ms(t_ms)
        if not spot:
            return None
        return self._surface.price_at(str(inst_id).upper(), t_ms, spot)

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
        dsigma_pts: Optional[float] = None,
        tick: Optional[float] = None,
    ) -> dict:
        """把该时刻的链快照转成 ``okx_options_select.select_puts`` 认的 chain dict。

        这是**回测与实盘共用同一份选档代码的唯一桥接层** —— 过滤/排序逻辑一行
        都不重写，回测侧只负责把历史中价 IV 还原成「链」。

        字段映射（盘口价差由**家族 Δσ + tick 地板**模型给出，C43-① 方案 A）：

        ==========  ==================================================
        ``bid``     ``BS(iv − Δσ/2)`` —— 模拟「买一价」（卖方实收）。
                    选档与成交共用它，避免「选档看 mark、成交吃 bid」的口径分裂。
        ``ask``     ``BS(iv + Δσ/2)`` —— 买回价（与 bid 同一份代码）
        ``iv``      直接取 IV 曲面值（含插值），不反向从 mark 反解
        ``delta``   BS 算（代入曲面 IV）；反解失败 → None
        ``days``    ``(exp_ms − ts) / 86400000``
        ==========  ==================================================

        Δσ 取自 ``okx_options_trade.FAMILY_DSIGMA_PTS``（生产 tape 实测，单位
        IV 点），tick 取自 ``FAMILY_TICK``；两者均可用 ``dsigma_pts``/``tick``
        参数临时覆盖（测试/敏感性分析用）。``slippage`` 是**在价差之上**的
        额外对称价格滑点（小数，0.005 = 0.5%），默认 0。
        缺 spot / 已到期 / 无 IV / 算不出价的合约一律剔除并计入 ``stats``
        —— 静默降级不可接受，调用方必须看得到剔了多少、为什么。
        """
        t = ts or self._current_ts
        lot = self._lot_coin()
        empty = {"spot": 0.0, "lot_coin": lot, "groups": [],
                 "stats": {"total": 0, "kept": 0, "expired": 0,
                           "no_spot": 1, "no_iv": 0, "no_px": 0}}
        if t is None:
            return dict(empty)
        t_ms = int(t.timestamp() * 1000) if isinstance(t, datetime) else int(t)
        # spot 必须与「链」取同一时刻。不能走 ``price_of()`` —— 它读的是
        # ``_current_ts``，传入 ts 但未 seek 时会出现「链是 ts 的、spot 却是上次
        # seek 的」错配（测试里就撞到了：groups 直接空）。
        spot = float(self._spot_at_ms(t_ms) or 0.0)
        stats = {"total": 0, "kept": 0, "expired": 0, "no_spot": 0, "no_iv": 0,
                 "no_px": 0}
        if spot <= 0:
            stats["no_spot"] = 1
            return {"spot": 0.0, "lot_coin": lot, "groups": [], "stats": stats}

        sl = max(0.0, float(slippage or 0.0))   # 价差之上的额外对称价格滑点
        right = (opt_type or "P").upper()[:1]
        dsigma_override = None if dsigma_pts is None else max(0.0, float(dsigma_pts))
        tick_override = None if tick is None else max(0.0, float(tick))
        from nanobot_quant.okx_options_trade import family_dsigma_pts, family_tick
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
            # IV 直接取曲面值（含插值）—— 不再从 mark 反解：mark 本身就是用它算出来
            # 的，反解一圈只会引入数值误差，且两者必须一致（口径自洽）。
            iv = None
            if tv and self._surface is not None:
                iv = self._surface.iv_for(str(c.get("inst_id") or ""), t_ms)
            if iv is None:
                stats["no_iv"] += 1
                continue
            delta = bs_delta(spot, strike, tv, iv, 0.0, right)
            # 盘口价差：家族 Δσ + tick 地板（C43-① 方案 A，单一实现）
            inst_id = c.get("inst_id")
            dsigma = (dsigma_override if dsigma_override is not None
                      else family_dsigma_pts(inst_id)) / 100.0
            tick_sz = (tick_override if tick_override is not None
                       else family_tick(inst_id))
            bid, ask = quote_pair_from_iv(spot, strike, tv, iv, right=right,
                                          dsigma=dsigma, tick=tick_sz,
                                          extra_slip=sl)
            if bid is None:
                stats["no_px"] += 1
                continue
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
                    "inst_id": inst_id,
                    "bid": bid,
                    "ask": ask,
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

    def ask_at(self, inst_id: str, ts=None, right: Optional[str] = None,
               extra_slip: float = 0.0, dsigma_pts: Optional[float] = None,
               tick: Optional[float] = None) -> Optional[float]:
        """该合约在 ``ts`` 的**买回价（ask）**——与链快照同一套 Δσ + tick 口径。

        出场买回（止盈）用它与入场（``chain_dict_at`` 的 ``bid``）对称：
        卖收 bid、买付 ask，不能用同一个中价。（无 IV / 已到期 / 无 spot
        → ``None``，调用方自行回退并留痕，不得静默降价。）
        """
        t = ts or self._current_ts
        if t is None or not inst_id or self._surface is None:
            return None
        t_ms = int(t.timestamp() * 1000) if isinstance(t, datetime) else int(t)
        meta = self._contracts.get(str(inst_id))
        if not meta:
            return None
        exp_ms = int(meta.get("exp_ms") or 0)
        strike = float(meta.get("strike") or 0)
        rt = (right or meta.get("opt_type") or "")[:1].upper()
        if not rt:
            try:
                from nanobot_quant.okx_options_assets import right_of_inst
                rt = right_of_inst(inst_id)
            except Exception:  # noqa: BLE001
                return None
        if exp_ms <= t_ms or strike <= 0:
            return None
        spot = float(self._spot_at_ms(t_ms) or 0.0)
        if spot <= 0:
            return None
        tv = years_to_expiry(t_ms, exp_ms)
        iv = self._surface.iv_for(str(inst_id), t_ms)
        if iv is None:
            return None
        from nanobot_quant.okx_options_trade import family_dsigma_pts, family_tick
        _bid, ask = quote_pair_from_iv(
            spot, strike, tv, iv, right=rt,
            dsigma=((dsigma_pts if dsigma_pts is not None
                     else family_dsigma_pts(inst_id)) / 100.0),
            tick=(tick if tick is not None else family_tick(inst_id)),
            extra_slip=max(0.0, float(extra_slip or 0.0)))
        return ask

    def contracts(self) -> list[dict]:
        """全部有 IV 覆盖的合约（附 IV 观测点数）。"""
        if self._surface is None:
            return []
        cnt = Counter(p.inst_id for p in self._surface.points)
        return [{**meta, "iv_points": cnt.get(inst_id, 0)}
                for inst_id, meta in self._contracts.items()
                if cnt.get(inst_id, 0) > 0]

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
    end_ts: Optional[int] = None,
) -> dict:
    """期权回测数据层一次性诊断（**真实拉数**，非模拟）。

    回答两个问题：
      ① OKX ``history-candles`` 分页能否按区间拉到标的 K 线
      ② 归档成交能否覆盖到合约、并从成交价反解出 IV（看反解成功率）

    **2026-09-17 修订**：原先的 ② 是「每日到期 + 整数 strike 的 instId 推算是否
    成立」—— 该路线已被实测证伪（枚举 400 个只有 14 个拿到 mark，因为已到期合约
    的实时端点一律 51001）。现改走官方每日归档：合约列表来自真实成交，不再推算。
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
            length=length,
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

    # ② 归档 → 合约 + IV
    iv_n = len(ds._surface.points) if ds._surface is not None else 0
    ok_n = len({p.inst_id for p in ds._surface.points}) if ds._surface else 0
    out["contracts"] = {
        "in_archive": len(ds._contracts),
        "with_iv": ok_n,
        "iv_points": iv_n,
        "hit_rate": round(ok_n / len(ds._contracts), 4) if ds._contracts else 0.0,
        "samples": list(ds._contracts)[:3],
    }
    out["hypotheses"]["archive_iv_recovered"] = iv_n > 0

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
    """**同一到期内** put delta 是否随 strike 单调。

    put delta **恒为负**，且随 strike 上升而**递减**
    （实测 −0.37@100 → −0.74@106）。首版按 call 的递增方向写，
    在真实数据上直接报 false —— 抽成纯函数 + 单测锁死方向。

    注意：只能喂**同一到期**的档位。不同到期的 delta 水平本就不同
    （同样一张 85-P，剩 3 天与剩 6 天的 delta 差一倍），跨到期混在一起
    再判单调必然为 false。
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
            length=length,
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
    out["contracts"] = {
        "in_archive": len(ds._contracts),
        "with_iv": len({p.inst_id for p in ds._surface.points}) if ds._surface else 0,
    }
    if not bt:
        out["error"] = "没有标的 K 线，无法选时刻"
        return out

    ts = bt[len(bt) // 2]
    ds.seek(ts)
    ch = ds.chain_dict_at(expiry_min_days=3, expiry_max_days=7)

    rows: list[dict] = []
    ivs: list[float] = []
    group_deltas: list[list[tuple[float, float]]] = []
    for g in ch["groups"]:
        deltas: list[tuple[float, float]] = []
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
        group_deltas.append(deltas)
    # 逐到期组判定：不同到期的 delta 水平不同，跨组混排必然不单调
    per_group = [m for m in (
        _deltas_monotonic(sorted(gd)) for gd in group_deltas) if m is not None]
    mono = all(per_group) if per_group else None
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
