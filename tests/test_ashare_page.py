"""A 股采集页：结构性 marker 断言（防「静默误导」类回归）。

背景（2026-11-23）：
- 「刷新合约清单」按钮发过 GET，而后端只注册了 POST ⇒ 请求落到 SPA 兜底返回 HTML 200，
  页面显示「已刷新：undefined 个合约」。页面上看不出失败是最难查的一种。
- 页面状态区没有轮询 ⇒ 采集器在后台跑，页面一直显示打开时刻的快照，容易被误判成卡死。
"""

from __future__ import annotations

from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "src" / "nanobot_quant" / "ashare_page.html"


def _html() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_refresh_button_sends_post_body():
    """必须传 `{}` 才走 POST（api() 靠 body === undefined 区分 GET/POST）。"""
    assert "api('/config/ashare/contracts/refresh', {})" in _html()
    assert "api('/config/ashare/contracts/refresh')" not in _html()


def test_status_polling_wired():
    html = _html()
    assert "async function pollStatus" in html
    assert "setInterval(pollStatus, 30000)" in html
    # 轮询只重渲染状态区，不重写表单控件（否则打断正在填写的用户）
    assert "function renderStatus(" in html


def test_contracts_row_shows_cache_age():
    assert "ageText(cs.age_s)" in _html()


def test_poll_failure_is_visible():
    """轮询失败不得静默：状态行要显式提示「显示的是上次状态」。"""
    assert "状态刷新失败" in _html()


# ── 期权链 · IV 分位（5c）──────────────────────────────────

def test_option_chain_section_wired():
    html = _html()
    # 区块与控件
    assert "📈 期权链 · IV 分位" in html
    for el in ("oc-symbol", "oc-expiry", "oc-band", "oc-refresh", "oc-kpi",
               "oc-chain", "oc-iv", "oc-meta"):
        assert f'id="{el}"' in html, el
    # 两个只读端点必被调用（路径写错会落到 SPA 兜底返回 HTML）
    assert "/config/ashare/chain?symbol=" in html
    assert "/config/ashare/iv?symbol=" in html
    # 现役月默认取第一个（最近到期）
    assert "else if ((list || []).length) sel.value = list[0].expiry" in html


def test_option_chain_section_states_iv_caveats():
    """口径必须写在页面上：不用新浪 IV 字段、分位按交易日计、不外推。"""
    html = _html()
    assert "不用" in html and "新浪 IV" in html
    assert "按交易日计" in html
    assert "不外推" in html


# ── 脚本 boot 顺序（2026-11-26 真机回归）────────────────────

def test_modal_markup_precedes_script():
    """弹窗节点必须在 <script> 之前——否则 $('md-close') 为 null 抛 TypeError，
    后续 loadChain/loadIv 永不执行，期权链与 IV 分位永远停在「加载中…」。"""
    html = _html()
    assert html.index('id="md-mask"') < html.index("<script")


def test_optional_nodes_wired_through_guard():
    """拿不准是否在场的节点一律走 on() 守卫，不得直接 $('x').addEventListener。"""
    html = _html()
    assert "const on = (id, ev, fn) =>" in html
    for el in ("md-close", "md-all", "md-copy", "md-mask", "oc-symbol", "oc-expiry", "oc-refresh"):
        assert f"{el}').addEventListener" not in html, el
    # 启动段整体包 try/catch，失败要显式可见（不能静默停在「加载中…」）
    assert "页面初始化失败" in html and "loadChain(false);" in html


def test_chain_workbench_section_wired():
    """🎯 卖出候选：容器、渲染函数、接线三处都要在（缺一处页面就静默不显示）。"""
    html = _html()
    assert '<div id="oc-cand"' in html
    assert "function renderCand(j)" in html
    assert "renderCand(j);" in html          # loadChain 成功分支里真的调了
    # 候选口径必须在页面上写明，不能让人把「净收益率」当成往返后的数字
    assert "往返后" in html and "打平回落" in html


def test_arm_labels_have_no_letter_prefix():
    """臂名列已经是 A/B，标签再带「A · 」就重复（页面与 markdown 同源）。"""
    from nanobot_quant.ashare import paper_strategy as ps
    for arm in (ps.ARM_A, ps.ARM_B):
        assert not ps.ARM_LABELS[arm].startswith(f"{arm} ·")


def test_script_block_is_closed_exactly_once_and_last():
    """内联 JS 里不得出现 `</script>` 字面量（注释/字符串都不行）——浏览器会就地结块，
    其余 JS 变成页面正文；同时闭合标签必须真的存在且只有一个（曾漏掉 → 整页脚本文本化）。"""
    html = _html()
    assert html.count("</script") == 1, html.count("</script")
    assert html.count("</body>") == 1 and html.rstrip().endswith("</html>")
    # 唯一的闭合标签必须在启动段之后（在它之前出现 = 脚本被提前截断）
    assert html.index("</script") > html.index("setInterval(() => loadChain(false), 30000);")
