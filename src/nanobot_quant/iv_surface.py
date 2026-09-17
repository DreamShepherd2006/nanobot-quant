"""期权 IV 曲面 —— 成交价 → IV 点 → 微笑插值 → 任意档 BS 重定价。

为什么需要它（2026-09-17 实测）
--------------------------------
OKX 期权归档是**成交级**数据（0.22MB/天），而已到期合约的一切实时端点都返回
``51001``（``candles`` / ``mark-price-candles`` / ``ticker`` / ``history-trades``
逐个实测）—— 拿不到历史标记价。但期权价格本就是 ``(S, K, T, σ)`` 的确定函数：
只要有成交价把 σ 解出来，任意 ``(strike, 时刻)`` 的价格都能重算。

定价链条::

    tick 成交（price / ts / inst_id）
      → 反解 IV（需要当时的标的价 S）        :func:`iv_points_from_trades`
      → 按到期聚成微笑                      :class:`Smile` / :func:`fit_smile`
      → 任意档 BS 重定价 → bid/ask          :class:`IVSurface`

为什么「必须插值」而不是「只用有成交的档」
------------------------------------------
实测 09-15：链上 22 档 put，**只有 4 档有真实成交**。不插值的话链会被
``no_iv`` 剔到只剩 4 档，而那 4 档常常不在选档带（距离 ≥5%）里 —— 回测直接空转
（这正是旧路径「288 根有信号却零成交」的根因）。

口径
----
* 插值在 **log-moneyness**（``ln(K/F)``）上做线性 —— 期权微笑的标准口径；
  按 strike 直接线性插值在深度虚值端会失真。
* **范围外不外推**：取最近点的 IV 作常数延拓。线性外推会在深虚端算出负 IV，
  而这恰恰是卖 put 最关心的区域。
* 同一到期同一时刻的 call/put 理论上 IV 相等，因此**两侧的点共用一条微笑**
  （``Smile`` 不区分 right）；这是套利约束，不是简化。
* 无效输入一律返回 ``None``（fail-closed），由调用方计数留痕 ——
  与 :mod:`nanobot_quant.bs_pricing` 同一约定，禁止静默降级。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from nanobot_quant.bs_pricing import bs_delta, bs_price, implied_vol, years_to_expiry

__all__ = [
    "IVPoint", "Smile", "IVSurface",
    "iv_points_from_trades", "fit_smile", "DEFAULT_IV_STALENESS_MS",
]

# IV 观测保鲜期。实测 SOL-USD_UM 一天约 52 笔成交、散在 ~10 个合约上 ——
# 单合约平均间隔 ~5 小时，6 小时会把一半观测判失效。期权 IV 日内变化本就
# 远慢于标的价（它衡量的是波动率预期，不是价格）。
#
# 但 24h 对**远月**档太短：策略要的「剩 3–7 天」恰好落在成交最稀疏的档位上，
# 实测一个到期日常常整天无成交 —— 24h 窗口内一个点都没有，微笑为空，链里就
# 只剩近月那几组（实测每个 bar 只链出 1–4 个到期组，3–7 天窗口恒为空，
# 回测因此 0 成交）。放宽到 7 天："pts" 仍只取 ts 之前的点，不构成前视。
_DEFAULT_IV_STALENESS_DAYS = 7
DEFAULT_IV_STALENESS_MS = _DEFAULT_IV_STALENESS_DAYS * 24 * 3600 * 1000


# ──────────────────────────── IV 点 ────────────────────────────

@dataclass
class IVPoint:
    """一条成交反解出来的 IV 观测。"""
    ts_ms: int
    inst_id: str
    strike: float
    exp_ms: int
    right: str
    iv: float
    price: float
    spot: float


def iv_points_from_trades(
    trades: Iterable[dict],
    *,
    spot_at: Callable[[int], Optional[float]],
    contracts: dict[str, dict],
    r: float = 0.0,
    on_reject: Optional[Callable[[str, str], None]] = None,
) -> list[IVPoint]:
    """逐笔成交 → IV 观测点。

    ``spot_at(ts_ms)`` 负责给「成交那一刻的标的价」—— 回测里由标的 K 线提供；
    拿不到就跳过该笔（``on_reject("spot", ...)``），不猜、不用邻近值硬凑。

    ``contracts`` 是 ``instId → 元数据``（``strike`` / ``exp_ms`` / ``right``），
    由调用方从 instId 反解后传入 —— 本模块不 import ``okx_options_data``，
    保持「只依赖 math 与 bs_pricing」。
    """
    out: list[IVPoint] = []
    for tr in trades:
        inst = tr.get("instrument_name") or ""
        meta = contracts.get(inst)
        if not meta:
            if on_reject:
                on_reject("meta", inst)
            continue
        try:
            ts = int(float(tr.get("created_time")))
            px = float(tr.get("price"))
        except (TypeError, ValueError):
            if on_reject:
                on_reject("row", inst)
            continue
        spot = spot_at(ts)
        if not spot or spot <= 0:
            if on_reject:
                on_reject("spot", inst)
            continue
        t = years_to_expiry(ts, meta["exp_ms"])
        if not t:
            if on_reject:
                on_reject("expired", inst)
            continue
        iv = implied_vol(px, spot, meta["strike"], t, meta.get("right", "P"), r)
        if iv is None or iv <= 0:
            if on_reject:
                on_reject("iv", inst)
            continue
        out.append(IVPoint(ts_ms=ts, inst_id=inst, strike=float(meta["strike"]),
                           exp_ms=int(meta["exp_ms"]), right=meta.get("right", "P"),
                           iv=float(iv), price=px, spot=float(spot)))
    out.sort(key=lambda p: p.ts_ms)
    return out


# ──────────────────────────── 微笑 ────────────────────────────

@dataclass
class Smile:
    """同一到期的 IV 微笑：``(strike, iv)`` 点集 + 按 log-moneyness 插值。

    ``forward`` 只用于把 strike 转成 log-moneyness（``ln(K/F)``）；它不参与定价
    （定价用真实 spot），仅决定插值的自变量尺度。
    """
    strikes: list[float] = field(default_factory=list)
    ivs: list[float] = field(default_factory=list)
    forward: float = 0.0
    n_points: int = 0

    def __post_init__(self) -> None:
        self.n_points = len(self.strikes)

    @property
    def empty(self) -> bool:
        return not self.strikes

    def _fwd(self) -> float:
        """log-moneyness 的参考价：优先 forward，缺失时用点集中位 strike。"""
        if self.forward and self.forward > 0:
            return float(self.forward)
        if self.strikes:
            return float(self.strikes[len(self.strikes) // 2])
        return 1.0

    def iv(self, strike: float) -> Optional[float]:
        """任意 strike 的 IV。范围外取最近点（常数延拓），不线性外推。"""
        if not self.strikes or strike is None or strike <= 0:
            return None
        if len(self.strikes) == 1:
            return self.ivs[0]
        f = self._fwd()
        try:
            x = math.log(strike / f)
            xs = [math.log(k / f) for k in self.strikes]
        except (ValueError, ZeroDivisionError):
            return None
        if x <= xs[0]:
            return self.ivs[0]
        if x >= xs[-1]:
            return self.ivs[-1]
        for i in range(1, len(xs)):
            if x <= xs[i]:
                x0, x1 = xs[i - 1], xs[i]
                y0, y1 = self.ivs[i - 1], self.ivs[i]
                if x1 <= x0:
                    return y1
                w = (x - x0) / (x1 - x0)
                return y0 + w * (y1 - y0)
        return self.ivs[-1]


def fit_smile(points: Sequence[IVPoint], *, forward: float) -> Optional[Smile]:
    """同一到期的 IV 点 → 微笑。同一 strike 取**时间最近**的那个点。"""
    if not points:
        return None
    by_strike: dict[float, IVPoint] = {}
    for p in points:
        cur = by_strike.get(p.strike)
        if cur is None or p.ts_ms > cur.ts_ms:
            by_strike[p.strike] = p
    ks = sorted(by_strike)
    f = forward if forward and forward > 0 else ks[len(ks) // 2]
    return Smile(strikes=[float(k) for k in ks],
                 ivs=[by_strike[k].iv for k in ks],
                 forward=float(f))


# ──────────────────────────── 曲面 ────────────────────────────

class IVSurface:
    """按到期组织的 IV 曲面。

    与「逐合约 mark 历史」的对应关系：旧路径 ``_premiums[inst][ts]`` 是直接查表；
    这里换成「该合约截至 ts 的最近 IV → BS 定价」，两者对外都是一次价格查询，
    但本实现**不需要已到期合约的 mark 历史**（那东西根本不存在）。
    """

    def __init__(self, contracts: dict[str, dict], *,
                 lookback_ms: Optional[int] = None,
                 max_iv_staleness_ms: int = DEFAULT_IV_STALENESS_MS) -> None:
        self.contracts = contracts
        self.points: list[IVPoint] = []
        self._by_inst: dict[str, list[IVPoint]] = {}
        self._by_exp: dict[int, list[IVPoint]] = {}
        self._smile_cache: dict[tuple[int, int], Smile] = {}
        # IV 观测的保鲜期：超过这个时长的旧成交不再当作「当前 IV」
        self.max_iv_staleness_ms = int(max_iv_staleness_ms)
        self.lookback_ms = lookback_ms
        self.stats = {"points": 0, "no_point": 0, "stale": 0, "no_spot": 0,
                      "no_t": 0, "term": 0}

    # ── 装载 ──

    def add_points(self, points: Iterable[IVPoint]) -> None:
        for p in points:
            self.points.append(p)
            self._by_inst.setdefault(p.inst_id, []).append(p)
            self._by_exp.setdefault(p.exp_ms, []).append(p)
        for lst in self._by_inst.values():
            lst.sort(key=lambda p: p.ts_ms)
        for lst in self._by_exp.values():
            lst.sort(key=lambda p: p.ts_ms)
        self.points.sort(key=lambda p: p.ts_ms)
        self._smile_cache.clear()
        self.stats["points"] = len(self.points)

    # ── 查询 ──

    def _latest_iv(self, inst_id: str, ts_ms: int) -> Optional[IVPoint]:
        """该合约截至 ts 的最近 IV 点（超过保鲜期则视为失效）。"""
        lst = self._by_inst.get(inst_id)
        if not lst:
            return None
        lo, hi = 0, len(lst)
        while lo < hi:                       # 二分找最后一个 ts <= ts_ms
            mid = (lo + hi) // 2
            if lst[mid].ts_ms <= ts_ms:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return None
        p = lst[lo - 1]
        if ts_ms - p.ts_ms > self.max_iv_staleness_ms:
            self.stats["stale"] += 1
            return None
        return p

    def _fresh_points(self, exp_ms: int, ts_ms: int) -> list:
        """该到期在 ``ts_ms`` 时刻可用的观测（只取过去的点 —— 不构成前视）。"""
        pts = [p for p in self._by_exp.get(int(exp_ms), ()) if p.ts_ms <= ts_ms]
        if self.max_iv_staleness_ms > 0:
            pts = [p for p in pts if ts_ms - p.ts_ms <= self.max_iv_staleness_ms]
        return pts

    def smile_at(self, ts_ms: int, exp_ms: int, *, forward: float) -> Smile:
        """该到期在 ``ts_ms`` 时刻的微笑（按需构建并缓存）。

        用「各合约截至 ts 的最近 IV」作为点 —— 期权 IV 日内变化远慢于价格，
        稀疏成交下这是比「只用当根 bar 的成交」务实得多的口径。

        本到期一个点都没有时，退回**期限结构插值**（见 :meth:`_smile_by_term`）。
        """
        key = (int(ts_ms), int(exp_ms))
        hit = self._smile_cache.get(key)
        if hit is not None:
            return hit
        # 取该到期下「截至 ts」的全部点，同 strike 的多个点交给 fit_smile 取最近 ——
        # 不在这里做逐合约去重：_by_exp 按时间排序，跨合约交错时
        # 「上一条是否同一合约」判断不可靠，交给 fit_smile 统一裁决更稳。
        smile = fit_smile(self._fresh_points(exp_ms, ts_ms), forward=forward)
        if smile is None:
            smile = self._smile_by_term(ts_ms, int(exp_ms), forward=forward)
        if smile is None:
            smile = Smile()                             # 空微笑（缓存住，避免重复扫）
        self._smile_cache[key] = smile
        return smile

    def _smile_by_term(self, ts_ms: int, exp_ms: int, *,
                       forward: float) -> Optional[Smile]:
        """本到期无观测时的回退：借邻近到期微笑做总方差（σ²T）插值。

        为何需要：回测在 t 时刻只能看到「已成交过」的合约，而策略要的「剩 3–7 天」
        恰好是成交最稀疏的一段 —— 实测 09-08 那天，剩 0.8~2.8 天的到期都有观测，
        剩 3 天以上的一个点都没有，链里因此永远只有近月几组，3–7 天窗口恒为空、
        回测 0 成交。解法不是放宽到期窗口，而是借邻近到期的微笑。

        单侧有数据时直接用那一侧（同一 σ 平移）；两侧都有时按总方差线性内插 ——
        这是波动率曲面的标准做法（方差在时间上加性）。
        """
        t_years = years_to_expiry(ts_ms, exp_ms)
        if not t_years:
            return None
        lo = hi = None
        for exp in sorted(self._by_exp):
            if not self._fresh_points(exp, ts_ms):
                continue
            if exp < exp_ms:
                lo = exp if lo is None else max(lo, exp)
            elif exp > exp_ms:
                hi = exp if hi is None else min(hi, exp)
        if lo is None and hi is None:
            return None
        self.stats["term"] = self.stats.get("term", 0) + 1
        if lo is None or hi is None:
            base = lo if lo is not None else hi
            s = fit_smile(self._fresh_points(base, ts_ms), forward=forward)
            if s is None or not s.strikes:
                return None
            return Smile(strikes=list(s.strikes), ivs=list(s.ivs),
                         forward=forward)
        s1 = fit_smile(self._fresh_points(lo, ts_ms), forward=forward)
        s2 = fit_smile(self._fresh_points(hi, ts_ms), forward=forward)
        if s1 is None or s2 is None or not s1.strikes or not s2.strikes:
            return None
        t1 = years_to_expiry(ts_ms, lo) or 0.0
        t2 = years_to_expiry(ts_ms, hi) or 0.0
        if t2 <= t1:
            return None
        w = min(1.0, max(0.0, (t_years - t1) / (t2 - t1)))
        out_k: list[float] = []
        out_iv: list[float] = []
        for k in sorted(set(s1.strikes) | set(s2.strikes)):
            v1 = s1.iv(k)
            v2 = s2.iv(k)
            if v1 is None and v2 is None:
                continue
            v1 = v2 if v1 is None else v1
            v2 = v1 if v2 is None else v2
            var = (v1 * v1 * t1) * (1.0 - w) + (v2 * v2 * t2) * w
            out_k.append(float(k))
            out_iv.append(math.sqrt(max(var, 1e-12) / t_years))
        if not out_k:
            return None
        return Smile(strikes=out_k, ivs=out_iv, forward=forward)

    def price_at(self, inst_id: str, ts_ms: int, spot: float) -> Optional[float]:
        """该合约在 ``ts_ms``、标的价 ``spot`` 下的理论价（每 1 名义币 USD）。"""
        meta = self.contracts.get(inst_id)
        if not meta:
            self.stats["no_point"] += 1
            return None
        t = years_to_expiry(ts_ms, int(meta["exp_ms"]))
        if not t:
            self.stats["no_t"] += 1
            return None
        strike = float(meta["strike"])
        right = meta.get("right", "P")
        smile = self.smile_at(ts_ms, int(meta["exp_ms"]), forward=spot)
        iv = smile.iv(strike)
        if iv is None or iv <= 0:
            self.stats["no_point"] += 1
            return None
        return bs_price(spot, strike, t, iv, 0.0, right)

    def delta_at(self, inst_id: str, ts_ms: int, spot: float) -> Optional[float]:
        """与 :meth:`price_at` 同链路算出 delta（选档过滤链要用）。"""
        meta = self.contracts.get(inst_id)
        if not meta:
            return None
        t = years_to_expiry(ts_ms, int(meta["exp_ms"]))
        if not t:
            return None
        strike = float(meta["strike"])
        right = meta.get("right", "P")
        smile = self.smile_at(ts_ms, int(meta["exp_ms"]), forward=spot)
        iv = smile.iv(strike)
        if iv is None or iv <= 0:
            return None
        return bs_delta(spot, strike, t, iv, 0.0, right)

    def iv_for(self, inst_id: str, ts_ms: int) -> Optional[float]:
        """该合约该时刻的 IV（优先自身观测，缺失时回退同到期微笑）。"""
        p = self._latest_iv(inst_id, ts_ms)
        if p is not None:
            return p.iv
        meta = self.contracts.get(inst_id)
        if not meta:
            return None
        smile = self.smile_at(ts_ms, int(meta["exp_ms"]),
                              forward=float(meta["strike"]))
        return smile.iv(float(meta["strike"]))
