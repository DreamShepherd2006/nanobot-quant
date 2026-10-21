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


def test_bars_csv_export_present():
    """逐 bar 表 CSV 导出（2026-10-20）：按钮、路由、导出全部行。"""
    html = _html()
    assert "/config/backtest/bars.csv?run_id=" in html
    assert "导出 CSV" in html
    # 导出全部行，不受「只看有动作/成交」筛选影响
    assert "title=\"导出全部行（不受筛选影响）\"" in html
    # runId + 当前周期必须从 renderResult 传进 barsBox，否则链接无从拼 run_id/period
    assert "barsBox(res, runId, period)" in html


def test_multi_period_ui_present():
    """多周期对比（2026-10-21）：多选框、对比表、切换、口径警告。"""
    html = _html()
    assert 'id="periods"' in html and "multiple" in html
    # 表头分组（2026-10-21）：对比表与逐 bar 表的 TD / EMA 段都要分组表头
    assert html.count('class="grp td"') >= 2
    assert html.count('class="grp ema"') >= 2
    assert "候选买入" in html and "通过率" in html
    assert "selectedPeriods()" in html          # 提交时带上 periods
    assert "function compareBox" in html        # 周期对比表
    assert "function switchPeriod" in html      # 点行切换详情
    assert "不得求平均或加总" in html            # 口径警告写死在页面
    assert "&period=" in html                   # CSV 按当前档导出
    assert "周期 ${prog.period}" in html         # 运行中显示第 i/n 档
    # Markdown 按钮必须挂在顶层（各档子结果没有 markdown 字段，放 _periodView 会被空守卫吞掉）
    assert "mdButton(res, runId) + compareBox(res, runId)" in html


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


def test_multi_period_merged_bars_view_markers():
    """多周期对齐视图（2026-10-21 方案 A）：以最细周期为行基准并排三档。

    页面必须包含：合并渲染函数、行基准挑选、切换按钮、合并 CSV 导出参数。
    """
    html = _html()
    assert "_renderMergedBars" in html
    assert "_mergedRows" in html
    assert "_cacheBars" in html
    assert "多周期对齐" in html and "单周期" in html
    assert "&merge=1" in html              # 合并视图的 CSV 导出走 merge=1
    assert "行基准" in html                 # 页面显式标注行基准口径
