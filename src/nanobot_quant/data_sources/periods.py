"""统一 K 线周期命名与秒数（数据源无关的内部规范名）。

各交易所 API 的 interval 字符串不同（Gate "4h" / OKX "4H" / lumibot "4hour"），
上层（TD 周期下拉 / td-table / 回测 / 调度）一律使用这里的统一名，
由各数据源 spec 的 ``interval_map`` 映射到该所 API 字符串（2026-08-24 方案 C）。

实测依据：Gate API 服务端支持 18 个 interval（2026-08-24 实测：
1s/10s/1m/3m/5m/15m/30m/1h/2h/4h/6h/8h/12h/1d/3d/1w/7d/30d）。
秒级（1s/10s）对 TD 无意义（用户拍板不做），不纳入 PERIODS；
1W 与 7D 均为 Gate 真实粒度（自然周 vs 7 天），分开保留。
"""

# 统一周期名（上层唯一合法值）
PERIODS: tuple[str, ...] = (
    "1m", "3m", "5m", "15m", "30m",
    "1H", "2H", "4H", "6H", "8H", "12H",
    "1D", "3D", "1W", "7D", "30D",
)

# 周期 → 秒（TD 调度 / 回测时间推进）。30D 官方语义为日历月，取 30 天近似。
INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "2H": 7200, "4H": 14400, "6H": 21600, "8H": 28800, "12H": 43200,
    "1D": 86400, "3D": 259200, "1W": 604800, "7D": 604800, "30D": 2592000,
}

# UI 显示名（1W「1周」与 7D「7天」区分开，避免混淆）
DISPLAY_NAMES: dict[str, str] = {
    "1m": "1分", "3m": "3分", "5m": "5分", "15m": "15分", "30m": "30分",
    "1H": "1小时", "2H": "2小时", "4H": "4小时", "6H": "6小时",
    "8H": "8小时", "12H": "12小时",
    "1D": "1日", "3D": "3日", "1W": "1周", "7D": "7天", "30D": "30天（约1月）",
    # 合成周期（见 COMPOSITE_PERIODS）：显示名后缀标注来源，避免与原生档混淆
    "2m": "2分（1m×2）", "4m": "4分（1m×4）", "6m": "6分（3m×2）",
    "10m": "10分（5m×2）", "20m": "20分（5m×4）", "45m": "45分（15m×3）",
}


# ── 合成周期（2026-10-22 用户拍板）────────────────────────────────────────
# 官方 API 不提供、由更粗的原生周期**精确聚合**得到的档位。只作为回测的
# **可选档位**存在，默认不勾选、不影响任何现有默认行为；td-table 与实盘
# 调度仍只认 ``PERIODS``（见 ``backtest_periods()``）。
#
# 准入规则（三条，写入 docs/quant-system.md 数据源章节）：
#   ① 目标周期必须是源周期的**整数倍**。不整除时窗口边界无法对齐，
#      「用 3m + 1m 拼 4m」只在 1/3 的窗口成立、其余窗口必须退回最细源
#      ⇒ 深度退化、无数据收益，故一律不做。
#   ② 源必须**原生提供**；同一目标只允许一个源（多个候选时取最粗的）。
#   ③ 可回溯深度 ≈ 10000 × **源周期秒**（服务端深度闸门按源周期计），
#      必须能覆盖 TD 窗口 + 预热 + 目标回测区间。
COMPOSITE_PERIODS: dict[str, tuple[str, int]] = {
    "2m": ("1m", 2),
    "4m": ("1m", 4),
    "6m": ("3m", 2),
    "10m": ("5m", 2),
    "20m": ("5m", 4),
    "45m": ("15m", 3),
}


# 合成周期的秒数与原生周期同表（调度 / 回测推进 / 缺口判定共用一份）
INTERVAL_SECONDS.update({
    "2m": 120, "4m": 240, "6m": 360,
    "10m": 600, "20m": 1200, "45m": 2700,
})


def is_composite_period(period: object) -> bool:
    """该周期是否为合成周期（不在原生 PERIODS 里、由更粗周期聚合）。"""
    return str(period) in COMPOSITE_PERIODS


def composite_source(period: object) -> str | None:
    """合成周期的源周期名；原生周期返回 None。"""
    hit = COMPOSITE_PERIODS.get(str(period))
    return hit[0] if hit else None


def composite_factor(period: object) -> int | None:
    """合成周期的聚合因子（目标 = 源 × 因子）；原生周期返回 None。"""
    hit = COMPOSITE_PERIODS.get(str(period))
    return hit[1] if hit else None


def backtest_periods() -> tuple[str, ...]:
    """回测可选周期 = 原生 16 档 + 合成 6 档（顺序：原生在前）。"""
    return PERIODS + tuple(COMPOSITE_PERIODS)


def period_label(period: object) -> str:
    """展示用标签：合成周期补注来源与因子（如 ``4m（1m×4 合成）``）。"""
    p = str(period)
    src = composite_source(p)
    if src is None:
        return p
    return f"{p}（{src}×{composite_factor(p)} 合成）"


def lumibot_bar_map(spec_name: str = "gate_cex") -> dict:
    """数据源 timestep → 统一周期名 的完整解析映射。

    lumibot 风格键（minute/5min/hour/day…）为回测/旧调用兼容；注册表
    spec.bars（16 周期）原样/小写双键覆盖新周期（3m/2H/6H/8H/12H/
    3D/7D/30D），调用处 ``.lower().removeprefix("bar:")`` 会把 "1H" 归一
    成 "1h"，故大小写键都放。ReplayDataSource / CexDataSource 共用，
    新增交易所周期只需改 spec.bars 声明，无需再改硬编码（方案 C）。
    """
    m = {
        "minute": "1m", "5min": "5m", "15min": "15m", "30min": "30m",
        "hour": "1H", "4hour": "4H", "day": "1D", "week": "1W",
    }
    try:
        from nanobot_quant.data_sources import get_data_source

        bars = getattr(get_data_source(spec_name), "bars", None) or ()
    except Exception:  # 注册表未就绪时仅 lumibot 风格键可用
        bars = ()
    for p in bars:
        m.setdefault(p, p)
        m.setdefault(p.lower(), p)
    return m
