"""无成本锚确认（ack）= 显式放弃保本门 —— 页面侧（a）+ 执行层（b），2026-10-01。

背景：保本门实为两道独立判断 ——
    ② 无成本锚门：`cost_basis is None and not ack` ⇒ 拒（缺 C 不可判定）
    ③ 保本门：`if cost_basis is not None:` ⇒ 强制 K + px ≥ C（**ack 不参与**）
页面又为 call **自动带出 C** ⇒ 勾了 ack 也永远过不去（用户实测连续失败 3 次）。

修复：
    a. 页面勾选 ack ⇒ 清空并禁用 C 输入框（取消勾选恢复）+ 发令牌前做与后端同判据的
       保本门预校验（不再「预览+令牌都给了，② 才报保本门」）+ 提示语带正解；
    b. 执行层 ack 即覆盖 —— 无论 C 是否填写都放行，台账一律记 no_cost_ack。

本文件只做页面结构断言（后端语义见 tests/test_okx_options_trade.py 的 open_call 用例）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1]
        / "src" / "nanobot_quant" / "okx_options_page.html").read_text(encoding="utf-8")


def test_ack_clears_and_disables_c_input():
    """a：勾选 ⇒ 清空 + 禁用 C；取消 ⇒ 恢复（含占位文案）。"""
    assert "function syncNoCostAckUI()" in PAGE
    body = PAGE.split("function syncNoCostAckUI()", 1)[1].split("\nasync function ", 1)[0]
    assert 'c.value = ""; c.disabled = true;' in body
    assert "c.disabled = false" in body
    assert "c.dataset.prev" in body, "取消勾选要能恢复原值"
    assert 'const NOCOST_C_PLACEHOLDER = "默认=已接货成本 C（赔付+补买支出）÷面值";' in PAGE


def test_ack_listener_and_modal_reset():
    assert '$("mNoCostAck").addEventListener("change", syncNoCostAckUI);' in PAGE
    # 弹窗每次打开都要重置 C 输入框状态（C 分支与 put 分支各一次）
    assert PAGE.count("syncNoCostAckUI();") >= 2


def test_guard_precheck_before_token():
    """a：发令牌前先按后端同判据预校验，红的时候直接拦下并给正解。"""
    body = PAGE.split("async function doStageSell()", 1)[1].split("const j = await jpost", 1)[0]
    assert "instStrike(modalInst)" in body, "需要按合约解析 K"
    assert "保本门不通过：" in body
    assert "无成本锚确认" in body, "错误提示必须带正解（勾选 ack）"


def test_preview_takes_ack_branch_first():
    """预览的保本门状态：ack 优先（与后端一致，不管 C）→ 有 C → 无 C。"""
    seg = PAGE.split("const cb = f.cost_basis;", 1)[1].split("} else {", 1)[0]
    assert seg.index("f.no_cost_ack") < seg.index("if (cb)"), "ack 分支必须排在 C 分支之前"
    assert "已确认放弃（无成本锚）" in seg
    assert "无论是否填 C" in seg


def test_hint_copy_documents_waiver():
    assert "显式放弃保本门" in PAGE
    assert "勾选时 C 输入框自动清空并禁用" in PAGE
    assert "确需放弃保本门请勾选上方「无成本锚确认」" in PAGE
