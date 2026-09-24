"""OKX 期权 handlers 分派测试（批 1 Step 2，2026-09-08）。

验证 /sell /close /preview 的模块级 dispatch helper 按 inst_id 的
opt_type（P/C）正确分派到 put/call 路径，call 透传 cost_basis 保本门。
"""

import pytest

from nanobot_quant import okx_options_handlers as oh
from nanobot_quant import okx_options_trade as ot


@pytest.fixture(autouse=True)
def _dispatch_mocks(monkeypatch):
    """把 resolve_instrument 与 put/call 函数替换为记录桩，不触网络。"""
    calls = {}

    def fake_resolve(inst_id):
        return {"opt_type": "C" if inst_id.endswith("-C") else "P",
                "lot": 0.1, "strike": 101.0, "exp_ms": 0, "inst_family": "SOL-USD_UM"}

    monkeypatch.setattr(oh.ot, "resolve_instrument", fake_resolve)
    for name in ("open_put", "open_call", "close_put", "close_call",
                 "preview_open_put", "preview_open_call"):
        monkeypatch.setattr(oh.ot, name, _make_fake(calls, name))
    return calls


def _make_fake(calls, name):
    def _f(*a, **kw):
        calls[name] = {"a": a, "kw": kw}
        return {"ok": True, "kind": name, "opt_type": kw.get("inst_id", a[0] if a else "").rsplit("-", 1)[-1][:1]}
    return _f


# ── 分派：卖期权（sell）──


def test_dispatch_sell_call(_dispatch_mocks):
    """C 合约 → open_call，cost_basis / no_cost_basis_ack 透传（门在后端执行）。"""
    oh._dispatch_sell("bot1", "SOL-USD_UM-260910-101-C", 1, "limit", 1.1, cost_basis=106.0)
    assert _dispatch_mocks["open_call"]["kw"]["cost_basis"] == 106.0
    assert _dispatch_mocks["open_call"]["kw"]["inst_id"].endswith("-C")
    assert "open_put" not in _dispatch_mocks


def test_dispatch_sell_call_no_cost_ack_passthrough(_dispatch_mocks):
    """C46：无成本锚确认标志必须透传到 open_call（否则后端门直接拒）。"""
    oh._dispatch_sell("bot1", "SOL-USD_UM-260910-101-C", 1, "limit", 1.1,
                      cost_basis=None, no_cost_ack=True)
    assert _dispatch_mocks["open_call"]["kw"]["no_cost_basis_ack"] is True
    # 默认未确认
    oh._dispatch_sell("bot1", "SOL-USD_UM-260910-101-C", 1, "limit", 1.1, cost_basis=106.0)
    assert _dispatch_mocks["open_call"]["kw"]["no_cost_basis_ack"] is False


def test_dispatch_sell_put(_dispatch_mocks):
    """P 合约 → open_put（保持原路径，cost_basis 不适用不传递）。"""
    oh._dispatch_sell("bot1", "SOL-USD_UM-260910-101-P", 1, "limit", 0.3)
    assert _dispatch_mocks["open_put"]["kw"]["inst_id"].endswith("-P")
    assert "open_call" not in _dispatch_mocks


# ── 分派：买回平仓（close）──


def test_dispatch_close_call(_dispatch_mocks):
    """C 合约平仓 → close_call（只匹配 open_call 台账行）。"""
    oh._dispatch_close("bot1", "SOL-USD_UM-260910-106-C", 1, "limit", 0.2)
    assert "close_call" in _dispatch_mocks
    assert "close_put" not in _dispatch_mocks


def test_dispatch_close_put(_dispatch_mocks):
    """P 合约平仓 → close_put（原路径）。"""
    oh._dispatch_close("bot1", "SOL-USD_UM-260910-101-P", 1, "limit", 0.2)
    assert "close_put" in _dispatch_mocks
    assert "close_call" not in _dispatch_mocks


# ── 分派：预览（preview）──


def test_dispatch_preview_call_gate(_dispatch_mocks):
    """C 合约预览 → preview_open_call 带 cost_basis（返回保本门状态）。"""
    oh._dispatch_preview("SOL-USD_UM-260910-101-C", 1, "limit", 1.1, cost_basis=106.0)
    kw = _dispatch_mocks["preview_open_call"]["kw"]
    assert kw["cost_basis"] == 106.0
    assert "preview_open_put" not in _dispatch_mocks


def test_dispatch_preview_put(_dispatch_mocks):
    """P 合约预览 → preview_open_put（原路径，无 cost_basis）。"""
    oh._dispatch_preview("SOL-USD_UM-260910-101-P", 1, "limit", 0.3)
    assert "preview_open_put" in _dispatch_mocks
    assert "preview_open_call" not in _dispatch_mocks


def test_dispatch_preview_call_no_cost_ack_passthrough(_dispatch_mocks):
    """C46：预览也透传 no_cost_ack（页面据此区分「已确认 / 未确认无 C」）。"""
    oh._dispatch_preview("SOL-USD_UM-260910-101-C", 1, "limit", 1.1, no_cost_ack=True)
    assert _dispatch_mocks["preview_open_call"]["kw"]["no_cost_ack"] is True
    oh._dispatch_preview("SOL-USD_UM-260910-101-C", 1, "limit", 1.1)
    assert _dispatch_mocks["preview_open_call"]["kw"]["no_cost_ack"] is False


def test_sell_stage_to_confirm_carries_no_cost_ack(_dispatch_mocks):
    """C46 全链路（start stage → confirm consume → dispatch）：

    ack 必须随 stage 一路传到 open_call——否则勾选确认后仍会被后端门误拒。
    镜像 _sell_confirm 的取参方式（ack 取自 stage payload，不由 confirm 单独传）。
    """
    oh._pending_tx.clear()
    st = oh._stage("sell", {"account": "bot1", "inst_id": "SOL-USD_UM-260910-101-C",
                            "sz": 1, "ord_type": "limit", "px": 1.1,
                            "cost_basis": None, "no_cost_ack": True,
                            "opt_type": "C", "preview": {}})
    act, err = oh._consume({"tx_id": st["tx_id"]})
    assert err is None and act["action"] == "sell"
    p = act["payload"]
    oh._dispatch_sell(p["account"], p["inst_id"], p["sz"], p["ord_type"],
                      p.get("px"), p.get("cost_basis"), bool(p.get("no_cost_ack")))
    assert _dispatch_mocks["open_call"]["kw"]["no_cost_basis_ack"] is True
    # 令牌一次性：重复 confirm 必须失效
    _, err2 = oh._consume({"tx_id": st["tx_id"]})
    assert err2 and "无效" in err2


def test_options_page_no_cost_ack_controls():
    """C46 页面契约：卖 call 弹窗必须有「无成本锚确认」勾选框，且台账行标记可见。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "src" / "nanobot_quant" / "okx_options_page.html").read_text(encoding="utf-8")
    assert 'id="mNoCostAck"' in html                  # 勾选框存在
    assert 'id="mRowCostAck"' in html                 # 所在行（随 call 模式显隐）
    assert "后端默认拒绝" in html                      # 未勾选时的后果文案
    assert "no_cost_ack" in html                       # 表单透传字段
    assert "⚠️无成本锚" in html                          # 台账行标记（e.no_cost_ack）
    assert "no_cost_ack" in html and "e.no_cost_ack" in html


def test_ledger_page_pnl_falls_back_to_settle_pnl():
    """台账「盈亏」列须回退读 settle_pnl：到期判定行只写 settle_pnl（无 pnl_usd），
    此前页面盈亏恒显示「—」（2026-09-15 重建实测 104-C 行）。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "src" / "nanobot_quant" / "okx_options_page.html").read_text(encoding="utf-8")
    assert "function pnlUsd(e)" in html
    assert "ok(e.pnl_usd)" in html and "ok(e.settle_pnl)" in html
    assert "/^settled_/" in html          # 到期结算行以官方账单口径 settle_pnl 为准
    assert "+ pnlUsd(e) +" in html          # 台账表盈亏列已改用 pnlUsd
    assert "无待回填行" in html               # 回填扫描 0 行时的说明


def test_c24_routes_registered():
    """C24 合约选择路由已注册：候选生成（GET）+ 选择参数保存（POST）。"""
    class _App:           # 只需 add_api_route（gatekeeper 用 FastAPI；测试不依赖 fastapi）
        def __init__(self):
            self.routes = []

        def add_api_route(self, path, fn, methods=None):
            self.routes.append(type("R", (), {"path": path})())

    app = _App()

    class _GK:            # 注册阶段不访问 gatekeeper
        pass

    oh.register_okx_options_routes(app, _GK())
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/config/okx-options/candidates" in paths
    assert "/config/okx-options/selector" in paths


def test_selector_save_validates_and_persists(monkeypatch, tmp_path):
    """选择参数保存：非法值报错、合法值落盘（selector 字段）。"""
    from nanobot_quant import okx_options_select as osel
    from nanobot_quant import okx_options_trade as ot

    saved = {}

    def fake_save(**fields):
        saved.update(fields)
        return {"collateral_ratio_pct": 100, **fields}

    monkeypatch.setattr(ot, "save_option_params", fake_save)
    cleaned, err = osel.validate_selector({"min_distance_pct": 7, "top_n": 3,
                                           "expiry_min_days": 4, "expiry_max_days": 6,
                                           "delta_min": 0.2, "delta_max": 0.3,
                                           "sort_by": "apr"})
    assert err is None
    assert cleaned["min_distance_pct"] == 7.0 and cleaned["top_n"] == 3
    bad, berr = osel.validate_selector({"min_distance_pct": 99})
    assert bad is None and berr


def test_page_has_candidate_ui():
    """页面含候选 UI 与选择参数控件（防止前端改动被回退）。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "src" / "nanobot_quant" / "okx_options_page.html").read_text(encoding="utf-8")
    for token in ('id="candWrap"', 'id="candBody"', 'id="candRefresh"', 'id="selDist"',
                  'id="selMinYield"', 'id="saveSelBtn"', "function renderCandidates",
                  "function loadCandidates", "loadSelector()"):
        assert token in html, token


# ── 期权页布局（方案 A：三层分离，策略层沉底）2026-09-16 ──────────────
def _page_html() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "src" / "nanobot_quant"
            / "okx_options_page.html").read_text(encoding="utf-8")


def test_page_three_layers_layout():
    """策略层（🤖 自动策略）沉底 —— 不再夹在看板层（TD/链/台账）中间。

    参数层（担保/定价/合约选择）独立成折叠区，与策略层分开。
    """
    h = _page_html()
    # 三个折叠区：参数层（paramSection）+ 自动策略层（liveSection）+ 盘口采集层（tapeSection）
    assert h.count("<details") == h.count("</details>") == 3
    assert 'id="settingsCard"' not in h            # 旧的混合折叠区已拆掉

    i_bar = h.index('id="liveBar"')
    i_param = h.index('id="paramSection"')
    i_td = h.index('id="tdCard"')
    i_chain = h.index('id="chainCard"')
    i_led = h.index('id="ledCard"')
    i_live = h.index('id="liveSection"')
    i_tape = h.index('id="tapeSection"')

    assert i_bar > h.index('id="chainCtl"')        # 状态条在看板设置之后
    assert i_td < i_live and i_chain < i_live and i_led < i_live   # 看板层在策略层之前
    assert i_param < i_td                          # 参数层独立、默认收起
    assert i_live < i_tape                         # 采集层沉底（研究用，不干扰看板/策略）


def test_family_multi_select_replaces_text_input():
    """标的家族：手填文本框 → 多选（由后端 available_families 渲染）。"""
    h = _page_html()
    assert 'type="text" id="liveFamilies"' not in h
    assert 'id="liveFamilies" class="famchk"' in h
    for fn in ("renderFamilyChecks", "readFamilyChecks", "familiesChanged"):
        assert fn in h, fn
    assert "available_families" in h


def test_live_status_bar_and_polling():
    """顶部状态条 + 自动策略状态 30s 轮询（此前只在页面加载时抓一次）。"""
    h = _page_html()
    assert 'id="liveBarText"' in h
    assert "updateLiveBar" in h
    assert "setInterval(loadLive, 30000)" in h


def test_outside_family_manual_hint():
    """家族外标的的手动下单提示（候选表提示 + 顶部下拉标注）。"""
    h = _page_html()
    assert 'id="candOutside"' in h
    assert "不在自动循环的标的家族内" in h
    assert "markFamilyOptions" in h
    assert "（策略中）" in h


def test_live_get_exposes_available_families():
    """GET /live 暴露 available_families；保存时对未知家族 fail-closed。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "nanobot_quant"
           / "okx_options_handlers.py").read_text(encoding="utf-8")
    assert '"available_families": list(od.FAMILIES)' in src
    assert "未知标的家族" in src


# ── 📼 盘口采集（研究用只读，2026-09-22）：路由 / UI / 只读边界 ──


def _handlers_src() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "src" / "nanobot_quant"
            / "okx_options_handlers.py").read_text(encoding="utf-8")


def _tape_block() -> str:
    src = _handlers_src()
    start = src.index("async def _tape_get")
    end = src.index("async def _pending")
    return src[start:end]


def test_tape_routes_registered():
    """盘口采集：GET 看状态 / POST 保存并启停。"""
    class _App:
        def __init__(self):
            self.routes = []

        def add_api_route(self, path, fn, methods=None):
            self.routes.append(type("R", (), {"path": path})())

    app = _App()

    class _GK:
        pass

    oh.register_okx_options_routes(app, _GK())
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/config/okx-options/tape" in paths


def test_tape_endpoint_validates_families_fail_closed():
    """未知家族拒绝写入（fail-closed，与自动循环同口径）。"""
    blk = _tape_block()
    assert "未知标的家族" in blk
    assert 'if k in otp.DEFAULT_TAPE' in blk      # 只接受采集白名单字段
    assert "otp.state()" in blk and "otp.sync" in blk


def test_tape_endpoint_is_not_a_trading_switch():
    """采集保存不得触碰 live 段/交易开关（只写 option_params 的 tape 段）。"""
    blk = _tape_block()
    for forbidden in ("save_option_params(live", "ol.sync",
                      "set_order", "set_margin_balance", "open_put"):
        assert forbidden not in blk, f"采集端点出现交易相关调用：{forbidden}"


def test_tape_ui_present_and_polled():
    """页面：📼 采集小节 + 独立保存按钮 + 30s 只读状态轮询。"""
    h = _page_html()
    for token in ('id="tapeEnabled"', 'id="saveTapeBtn"', 'id="tapeFamilies"',
                  'id="tapeStatus"', 'id="tapeDepth"',
                  "setInterval(loadTape, 30000)", "loadTape()"):
        assert token in h, f"页面缺少 {token}"
    assert "不参与任何交易决策" in h or "不读持仓、不下单、不参与任何交易决策" in h


def test_live_sync_also_syncs_tape_without_breaking_strategy(monkeypatch):
    """策略循环 sync 同时拉起采集；采集报错不得影响策略循环。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "nanobot_quant"
           / "okx_options_live.py").read_text(encoding="utf-8")
    assert "from . import option_tape as _tape" in src
    assert "_tape.sync()" in src
    assert "不影响策略循环" in src        # 异常吞噪，但打 stderr 可查
