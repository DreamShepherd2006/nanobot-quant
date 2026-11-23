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
