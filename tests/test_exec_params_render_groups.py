"""/config/exec 分组渲染的防回归测试（2026-10-05）。

背景：`gate_enabled` / `gate_red_min`（贝叶斯闸门）于 2026-09-21（commit ceb383a）
从 group="td" 迁到 group="f1gate"，但渲染侧当时维护着第二份硬编码分组列表
("risk", "exec", "td") ⇒ 该组从那天起在 /config/exec 页面上**静默消失**。

修复：渲染改为遍历 GROUP_TITLES（分组元数据的唯一来源）再扣除 RENDER_SKIP_GROUPS。
本测试锁住三点，防止同类「两处定义」缺陷再次静默发生：
  ① PARAM_META 里声明的每个 group 都必须在 GROUP_TITLES 中有标题；
  ② 除专属渲染组（scene → 场景卡）外，所有分组都会进入渲染列表；
  ③ f1gate 能真正渲染出卡片（2026-10-05 的回归点）。
"""
from nanobot_quant import exec_params_handlers as h
from nanobot_quant.exec_params import (GROUP_TITLES, PARAM_META,
                                       RENDER_SKIP_GROUPS)


def test_declared_groups_all_have_titles():
    declared = {m.get("group") for m in PARAM_META.values()}
    missing = declared - set(GROUP_TITLES)
    assert not missing, f"PARAM_META 声明了未定义标题的分组: {missing}"


def test_rendered_groups_cover_all_non_skip():
    rendered = [g for g in GROUP_TITLES if g not in RENDER_SKIP_GROUPS]
    assert rendered == [g for g in GROUP_TITLES if g != "scene"]
    assert "f1gate" in rendered, "贝叶斯闸门分组必须可见（2026-10-05 回归点）"


def test_f1gate_card_renders():
    html = h._group_html("f1gate", {"gate_enabled": False, "gate_red_min": 0.45},
                         {}, "cex")
    assert html, "f1gate 分组应渲染出卡片（此前为静默消失）"
    assert "贝叶斯" in html and "gate_enabled" in html
