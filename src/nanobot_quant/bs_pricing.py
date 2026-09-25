"""Black-Scholes 定价 / IV 反解 / delta（OKX U 本位期权口径）。

**为什么需要它**：期权回测要与实盘「同一份决策代码」。实盘选档过滤链里有一道
delta 带（`okx_options_select`，默认 0.05–0.35），而 delta 来自 OKX ticker；
历史时刻的 delta 不存在 —— 只能从 mark 反解 IV 再算 delta。

OKX 的 `markVol` 本身就是「标准 BS 反解（欧式 / 无股息 / r≈0）」，所以本模块与
实盘是**同一口径**，不是近似替代。（`MarkPrice.py` 里的 `askVol`/`bidVol`/`markVol` 
可直接与本模块输出交叉校验。）

约定
----
* 欧式看涨/看跌，无股息，连续复利；``r`` 默认 0（OKX 短周期实际 r≈0）
* ``T`` 年化（365 天）
* ``sigma`` 年化小数（0.6 = 60%）
* **无效输入一律返回 ``None``**（fail-closed），不抛异常、不静默猜测 ——
  调用方负责计数与留痕（静默降级是明确禁止的）
"""

from __future__ import annotations

import math
from typing import Optional

__all__ = [
    "norm_cdf", "intrinsic", "bs_price", "bs_delta", "implied_vol",
    "years_to_expiry", "quote_pair_from_iv", "SECONDS_PER_YEAR",
]

SECONDS_PER_YEAR = 365.0 * 86400.0
_MS_PER_SECOND = 1000.0

# IV 反解搜索区间（年化）：1bp ~ 500%。OKX 极薄盘偶见 >300% 的噪声报价，
# hi 留到 5.0 以覆盖，但调用方应对极端值保持怀疑（不设上限反而会掩盖异常）。
_IV_LO = 1e-4
_IV_HI = 5.0


def norm_cdf(x: float) -> float:
    """标准正态分布 CDF（用标准库 erf，避免引入 scipy）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def intrinsic(spot: float, strike: float, right: str = "P") -> float:
    """内在价值（每 1 名义币的 USD 金额）。"""
    if right == "P":
        return max(strike - spot, 0.0)
    return max(spot - strike, 0.0)


def _d1_d2(spot: float, strike: float, t: float, sigma: float,
           r: float = 0.0):
    vol_t = sigma * math.sqrt(t)
    if vol_t <= 0:
        return None, None
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / vol_t
    return d1, d1 - vol_t


def bs_price(spot: float, strike: float, t: float, sigma: float,
             r: float = 0.0, right: str = "P") -> Optional[float]:
    """欧式期权理论价（每 1 名义币，USD）。无效输入 → None。"""
    if not (spot > 0 and strike > 0) or t is None or t <= 0 or sigma is None:
        return None
    if sigma < 0:
        return None
    if sigma == 0:
        # 零波动极限：贴现后的内在价值
        return math.exp(-r * t) * intrinsic(spot, strike, right)
    d1, d2 = _d1_d2(spot, strike, t, sigma, r)
    if d1 is None:
        return None
    disc = math.exp(-r * t)
    if right == "P":
        return strike * disc * norm_cdf(-d2) - spot * norm_cdf(-d1)
    return spot * norm_cdf(d1) - strike * disc * norm_cdf(d2)


def bs_delta(spot: float, strike: float, t: float, sigma: float,
             r: float = 0.0, right: str = "P") -> Optional[float]:
    """BS delta（put 为负，call 为正）。无效输入 → None。

    注意：``okx_options_select`` 的 delta 带用**绝对值**比较（``abs(delta)``），
    OKX ticker 的 delta 对 put 也给负值，两边口径一致。
    """
    if not (spot > 0 and strike > 0) or t is None or t <= 0 or sigma is None:
        return None
    if sigma < 0:
        return None
    if sigma == 0:
        itm = (spot < strike) if right == "P" else (spot > strike)
        return (-1.0 if itm else 0.0) if right == "P" else (1.0 if itm else 0.0)
    d1, _ = _d1_d2(spot, strike, t, sigma, r)
    if d1 is None:
        return None
    disc = math.exp(-r * t)
    return (norm_cdf(d1) - 1.0) * disc if right == "P" else norm_cdf(d1) * disc


def quote_pair_from_iv(spot: float, strike: float, t: float, iv: float,
                       right: str = "P", dsigma: float = 0.0,
                       tick: float = 0.0, extra_slip: float = 0.0,
                       r: float = 0.0):
    """中价 IV → 可成交 ``(bid, ask)``（每 1 名义币，USD）。

    ``σ_bid = iv − Δσ/2``、``σ_ask = iv + Δσ/2``（``dsigma`` 为 IV 小数，
    0.122 = 12.2 点）—— 做市商报的是 IV 双边，价差在**波动率维度**上稳定，
    在价格维度上看普会随虚值程度被 vega 几何放大（假象）。

    ``tick`` > 0 时复现真实报价量化：价差不足 1 tick 按 1 tick 展开，并按 tick
    网格取整（bid 向下、ask 向上）—— 最薄档（权利金 ≲ 1 tick）的 bid 会被
    取整到 0，调用方按 ``bid > 0`` 自然剔除（与 OKX 返回 null bidPx 同效）。

    ``extra_slip`` 是在价差之上的对称价格滑点（默认 0，仅用于压力测试）。
    无效输入（spot/strike/t/iv 非法）→ ``(None, None)``（fail-closed）。
    """
    if not (spot > 0 and strike > 0) or t is None or t <= 0 \
            or iv is None or iv <= 0:
        return None, None
    half = max(0.0, float(dsigma or 0.0)) / 2.0
    bid = bs_price(spot, strike, t, max(1e-6, float(iv) - half), r, right)
    ask = bs_price(spot, strike, t, float(iv) + half, r, right)
    if bid is None or ask is None:
        return None, None
    sl = max(0.0, float(extra_slip or 0.0))
    if sl:
        bid *= 1.0 - sl
        ask *= 1.0 + sl
    tk = max(0.0, float(tick or 0.0))
    if tk > 0:
        if ask - bid < tk:                       # 地板：至少一个 tick
            mid = (ask + bid) / 2.0
            bid, ask = mid - tk / 2.0, mid + tk / 2.0
        bid = math.floor(bid / tk + 1e-9) * tk
        ask = math.ceil(ask / tk - 1e-9) * tk
    if bid < 0:
        bid = 0.0
    return bid, ask


def years_to_expiry(ts_ms: int, exp_ms: int) -> Optional[float]:
    """剩余年化期限。已到期（≤0）→ None。"""
    try:
        secs = (int(exp_ms) - int(ts_ms)) / _MS_PER_SECOND
    except (TypeError, ValueError):
        return None
    if secs <= 0:
        return None
    return secs / SECONDS_PER_YEAR


def implied_vol(mark_px: float, spot: float, strike: float, t: float,
                right: str = "P", r: float = 0.0,
                lo: float = _IV_LO, hi: float = _IV_HI,
                tol: float = 1e-7, max_iter: int = 120) -> Optional[float]:
    """从 mark 价反解隐含波动率（二分法，BS 价格对 sigma 单调）。

    返回年化小数；无解（残值 <0、报价异常、区间内无根）→ ``None``。

    二分而非 Newton：vega 在深度虚值 / 临近到期时会塌到 0，
    Newton 容易发散或跳到无意义根；二分区间固定，行为可预期。
    """
    if mark_px is None or spot is None or strike is None or t is None:
        return None
    if not (spot > 0 and strike > 0) or t <= 0:
        return None
    if not (mark_px > 0):
        return None
    intr = math.exp(-r * t) * intrinsic(spot, strike, right)
    # 容差取相对/绝对较大者：深 ITM 的 intrinsic 可能很大（如 30），
    # 纯绝对 1e-12 会把浮点误差当成「报价低于内在」而误杀；
    # 反之小 intrinsic 时相对量又不该被放大。
    eps = max(1e-12, abs(intr) * 1e-10)
    if mark_px < intr - eps:
        return None                      # 报价低于内在（不可能，报价数据异常）
    if mark_px <= intr + eps:
        return 0.0                       # 恰好只剩内在价值 → 时间价值 0

    def _f(sigma: float) -> Optional[float]:
        px = bs_price(spot, strike, t, sigma, r, right)
        return None if px is None else px - mark_px

    f_lo, f_hi = _f(lo), _f(hi)
    if f_lo is None or f_hi is None or f_lo > 0 or f_hi < 0:
        return None                      # 区间内无根

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = _f(mid)
        if f_mid is None:
            return None
        if abs(f_mid) < tol or (hi - lo) < tol:
            return 0.5 * (lo + hi)
        if f_mid > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)
