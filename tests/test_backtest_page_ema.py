"""回测页 EMA 组与逐 bar 表的页面标记断言（纯展示层，2026-10-20）。

页面是静态 HTML + 内联 JS，后端换个版本很容易把新增区块漏掉
（历史上就出现过「Dockerfile pin 停在只有引擎的 commit，页面功能静默消失」）。
这里用标记断言锁住：只要页面丢了这些 id/函数，测试立刻红。
"""
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "src" / "nanobot_quant" / "backtest_page.html"


def _html() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_ema_param_controls_present():
    html = _html()
    for marker in (
        'id="ema_gate_enabled"',
        'id="ema_n"',
        'id="ema_k"',
        'id="ema_warmup"',
        'id="bars_mode"',
    ):
        assert marker in html, marker
    # 预热是只读推导，不允许手填过小
    assert "readonly" in html
    assert "syncEmaWarmup" in html


def test_ema_warmup_formula_matches_backend():
    """页面 JS 的预热公式必须与后端 ema_warmup_bars 同公式同取整。"""
    from nanobot_quant.environment.sensors import ema_warmup_bars

    html = _html()
    # 页面：Math.max(50, Math.floor(5 * (n + 1) / 2))
    assert "Math.max(50, Math.floor(5 * (n + 1) / 2))" in html
    for n in (5, 21, 89, 144):
        assert ema_warmup_bars(n) == max(50, (5 * (n + 1)) // 2)


def test_bars_table_markers_present():
    html = _html()
    for marker in ("barsBox", "bars-body", "_renderBars", "bars-pageinfo"):
        assert marker in html, marker
    # 动作列来自策略自己的决策流（单一实现），不是前端另算
    assert "_event_sink" not in html  # 前端不该出现后端符号
    assert "动作/原因" in html
    # slope 列头跟随 k 动态显示
    assert "slope${" in html


def test_fills_table_has_ema_columns():
    html = _html()
    assert "<th>EMA</th><th>slope" in html
    assert "f.slope" in html


def test_two_arm_compare_wired_on_page():
    """两臂对照：复选框默认勾、结果区有 ⚖️ 块、且明写「禁配对推断」。"""
    html = _html()
    assert 'id="ema_compare"' in html
    assert "ema_compare_baseline" in html
    assert "function armsBox" in html
    assert "禁配对推断" in html


def test_running_state_renders_bars_incrementally():
    """运行中也要能看逐 bar 表（不等跑完 26 分钟）。"""
    html = _html()
    idx = html.index("function renderProgress")
    tail = html[idx : idx + 2500]
    assert "bars_rows" in tail
    assert "_renderBars()" in tail
