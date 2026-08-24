"""周期注册表（方案 C）— Step 4：调度层新周期映射全覆盖。

2026-08-24：Gate 16 周期统一规范（periods.PERIODS / INTERVAL_SECONDS /
lumibot_bar_map），td_live 与策略层心跳/粒度映射不再维护重复硬编码。
"""

from nanobot_quant.data_sources.periods import (
    INTERVAL_SECONDS,
    PERIODS,
    lumibot_bar_map,
)


def test_periods_contains_gate_16():
    assert len(PERIODS) == 16
    assert PERIODS[0] == "1s" or "1m" in PERIODS
    for p in ("3m", "30m", "2H", "6H", "8H", "12H", "3D", "7D", "30D"):
        assert p in PERIODS


def test_interval_seconds_covers_all_periods():
    for p in PERIODS:
        assert INTERVAL_SECONDS.get(p, 0) > 0, f"missing seconds for {p}"
    assert INTERVAL_SECONDS["1m"] == 60
    assert INTERVAL_SECONDS["3m"] == 180
    assert INTERVAL_SECONDS["30m"] == 1800
    assert INTERVAL_SECONDS["2H"] == 7200
    assert INTERVAL_SECONDS["6H"] == 21600
    assert INTERVAL_SECONDS["12H"] == 43200
    assert INTERVAL_SECONDS["3D"] == 259200
    assert INTERVAL_SECONDS["7D"] == 604800
    assert INTERVAL_SECONDS["30D"] == 2592000


def test_lumibot_bar_map_covers_gate_16():
    m = lumibot_bar_map("gate_cex")
    # 旧 lumibot 风格键保留（回测/旧调用兼容）
    assert m["minute"] == "1m"
    assert m["5min"] == "5m"
    assert m["hour"] == "1H"
    assert m["day"] == "1D"
    assert m["week"] == "1W"
    # 新周期：原样 + 小写双键（调用处 .lower().removeprefix("bar:") 归一）
    for p in ("3m", "30m", "2H", "6H", "8H", "12H", "3D", "7D", "30D"):
        assert m[p] == p
        assert m[p.lower()] == p


def test_strategy_timestep_by_sleeptime_new_periods():
    from nanobot_quant.strategies.td_sequential_strategy import (
        TdSequentialStrategy,
    )

    _TIMESTEP_BY_SLEEPTIME = TdSequentialStrategy._TIMESTEP_BY_SLEEPTIME

    # 旧周期 lumibot 风格（回测兼容）
    assert _TIMESTEP_BY_SLEEPTIME["1D"] == "day"
    assert _TIMESTEP_BY_SLEEPTIME["1H"] == "hour"
    assert _TIMESTEP_BY_SLEEPTIME["5m"] == "5min"
    # 新周期直通统一周期名（live bar: 前缀直拉 / 回测 replay 动态映射）
    for p in ("3m", "2H", "6H", "8H", "12H", "3D", "7D", "30D"):
        assert _TIMESTEP_BY_SLEEPTIME[p] == p


def test_parse_sleeptime_seconds_new_periods():
    from nanobot_quant.strategies.td_sequential_strategy import (
        _parse_sleeptime_seconds as strategy_pss,
    )
    from nanobot_quant.td_live import _parse_sleeptime_seconds as live_pss

    for fn in (strategy_pss, live_pss):
        assert fn("1m") == 60
        assert fn("30m") == 1800
        assert fn("2H") == 7200
        assert fn("3D") == 259200
        assert fn("7D") == 604800
        assert fn("30D") == 2592000
        assert fn("bogus") == 60  # 未知周期保持旧 fallback 语义
