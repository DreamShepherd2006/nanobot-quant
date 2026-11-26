"""A 股页面参数快照（📋 复制参数）：标签覆盖 / 值同源 / 只读不触发计算。

约束（与 ``params_md`` 模块 docstring 一致）：
- 标签表必须覆盖全部 ``PaperParams`` 字段——新增字段漏标签会让快照静默少行；
- 数值必须现读 ``tape`` / ``paper_runner``，不在渲染层留默认值副本；
- 生成过程**不得**触发 IV 重算（``panel()`` 会起后台线程，数十秒）。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from nanobot_quant.ashare import iv_panel as ivp
from nanobot_quant.ashare import params_md as pm
from nanobot_quant.ashare import paper_runner as pr
from nanobot_quant.ashare import paper_strategy as ps
from nanobot_quant.ashare import tape

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def cfg(monkeypatch):
    """把配置与运行态全部打桩（不碰 /data）。返回 (tape_cfg, paper_cfg)。"""
    tape_cfg = dict(tape.DEFAULT_TAPE)
    tape_cfg["underlyings"] = list(tape.POOL_CODES)
    tape_cfg["expiries"] = 4  # 用户 2026-11-26 已把到期档 3→4
    paper_cfg = pr.default_config()

    monkeypatch.setattr(tape, "tape_config", lambda: dict(tape_cfg))
    monkeypatch.setattr(tape, "state", lambda: {
        "running": True, "last_run": "2026-11-26T05:00:00Z",
        "total_samples": 199, "total_rows": 37372, "in_session": True,
        "last_error": ""})
    monkeypatch.setattr(tape, "tape_dir", lambda: "/data/legion/credentials/ashare_tape")
    monkeypatch.setattr(pr, "load_config", lambda: dict(paper_cfg))
    monkeypatch.setattr(pr, "runner_state", lambda: {
        "thread_alive": False, "last_run": None, "totals": {"rounds": 7}})
    monkeypatch.setattr(ivp, "find_option_dir", lambda: None)
    monkeypatch.setattr(ivp, "cached_state", lambda u: {
        "status": "ready", "run": "run_20261126_125304_p1120",
        "asof": "2026-11-25", "error": ""})
    # 账本摘要只读 pr.state（不碰 /data）：用一根成交的最小 payload
    payload = {
        "runner": {"thread_alive": True, "last_run": None},
        "arms": {
            ps.ARM_A: {"active": True, "ledger": {
                "equity": 500100.0, "return_pct": 0.02, "realized": 100.0,
                "unrealized": 0.0, "shorts": [], "collateral_locked": 0.0,
                "available_cash": 500100.0},
                "consistency": {"ok": True},
                "fills": [{"ts": "2026-11-26T05:00:00Z", "action": "buy_close",
                           "qty": 1, "realized": 100.0}]},
            ps.ARM_B: {"active": False, "ledger": {}, "fills": []},
        },
    }
    monkeypatch.setattr(pr, "state", lambda **kw: payload)
    return tape_cfg, paper_cfg


# ── 标签覆盖（防新增参数静默漏行）────────────────────────────

def test_labels_cover_all_paper_params():
    labelled = {k for _, items in pm.PARAM_GROUPS for k, _ in items}
    fields = {f.name for f in dataclasses.fields(ps.PaperParams)}
    assert fields - labelled == set(), f"缺标签：{fields - labelled}"
    assert labelled - fields == set(), f"标签指向不存在的字段：{labelled - fields}"


def test_labels_cover_all_tape_and_paper_base_keys():
    assert set(pm.TAPE_LABELS) == set(tape.DEFAULT_TAPE)
    # ``params`` 子字典由 PARAM_GROUPS 负责，不进基础段
    assert set(pm.PAPER_BASE_LABELS) == set(pr.default_config()) - {"params"}


# ── 结构 ────────────────────────────────────────────────────

def test_build_has_all_sections(cfg):
    md = pm.build()
    for head in ("# A 股 ETF 期权 · 页面参数快照", "## 运行态（读取时刻）",
                 "## ① 盘口采集（tape）", "## ② 模拟账本（基础）",
                 "## ③ 账本规则（入场 / 张数 / 出场 / 费用）",
                 "## ④ IV 分位口径（只读）",
                 "## ⑤ 账本摘要（成交/持仓明细见「📋 复制成交」）",
                 "## ⑥ 口径注记"):
        assert head in md, head


def test_values_read_from_config_and_deviation_marked(cfg):
    md = pm.build()
    # 改过默认值的项要标出来（到期档 4 vs 默认 3）
    assert "| 到期档数 | 4 ⚠️（默认 3） |" in md
    # 等于默认值的项不加标注
    assert f"| 采样间隔（秒） | {tape.DEFAULT_TAPE['interval_s']} |" in md
    # 标的池带中文名
    assert "510050.SH（上证50ETF）" in md


def test_initial_cash_and_symbols_rendered(cfg):
    md = pm.build()
    assert "| 初始资金（元） | 500000 |" in md or "| 初始资金（元） | 500000.0 |" in md
    for code in tape.POOL_CODES:
        assert code in md


def test_times_rendered_in_cst(cfg):
    md = pm.build()
    assert "2026-11-26 13:00:00" in md          # 05:00Z → 北京 13:00
    assert "2026-11-26 13:00:00" in md and "CST" in md


def test_tape_running_key_variant(cfg):
    """`tape.state()` 用 ``running``（不是 ``thread_alive``）——读错键会把在跑渲染成未运行。"""
    md = pm.build()
    tape_line = [l for l in md.splitlines() if "盘口采集（tape）" in l][0]
    assert "🟢 运行中" in tape_line
    assert "本次进程 samples=199" in tape_line and "rows=37372" in tape_line
    assert "交易时段内" in tape_line


def test_tape_today_rows_from_disk(cfg, tmp_path, monkeypatch):
    """内存计数器重建后归零，今日样本必须从落盘文件算。"""
    f = tmp_path / "tape_20261126.jsonl"
    f.write_bytes(b'{"a":1}\n{"a":2}\n{"a":3}\n')
    monkeypatch.setattr(tape, "sample_path", lambda day=None: f)
    md = pm.build()
    line = [l for l in md.splitlines() if "盘口采集（tape）" in l][0]
    assert "今日落盘 3 条" in line


def test_paper_stopped_rendered(cfg):
    md = pm.build()
    paper_line = [l for l in md.splitlines() if "模拟账本（paper）" in l][0]
    assert "⚪ 未运行" in paper_line and "rounds=7" in paper_line


def test_symbols_deviation_marked(cfg, monkeypatch):
    paper_cfg = cfg[1]
    paper_cfg["symbols"] = ["510050.SH", "510300.SH"]  # 默认只有 510050.SH
    md = pm.build()
    assert "⚠️（默认 510050.SH（上证50ETF））" in md


def test_notes_state_key_kou_jing(cfg):
    md = pm.build()
    assert "100% 全损担保" in md
    assert "盘口优先" in md
    assert "新浪行内 IV" in md


def test_params_snapshot_has_ledger_summary(cfg):
    md = pm.build()
    assert "| A | 止盈回落 50% / DTE 强平 |" in md and "500100.00" in md
    assert "100.00%" in md          # 唯一一笔已平仓且盈利 → 胜率 100%
    assert "| B | " not in md        # 未启用臂不进汇总表（active=False；② 里的配置行不算）


def test_iv_section_uses_cache_only(cfg, monkeypatch):
    """生成参数快照绝不能触发 IV 重算（panel() 会起后台线程）。"""
    def boom(*a, **k):
        raise AssertionError("params_md 不得调用 iv_panel.panel()")

    monkeypatch.setattr(ivp, "panel", boom)
    md = pm.build()
    assert "run_20261126_125304_p1120" in md
    assert "2026-11-25" in md


# ── 页面与路由接线（结构性 marker）──────────────────────────

def test_page_has_copy_button_and_fallback_modal():
    html = (ROOT / "src" / "nanobot_quant" / "ashare_page.html").read_text(encoding="utf-8")
    assert 'id="btn-params-md"' in html
    assert "'/config/ashare/params.md?_=' + Date.now()" in html
    # 剪贴板被拦时的兜底：弹窗 + 全选/复制按钮
    for el in ('id="md-mask"', 'id="md-text"', 'id="md-all"', 'id="md-copy"'):
        assert el in html, el
    assert "navigator.clipboard" in html


def test_route_registered():
    src = (ROOT / "src" / "nanobot_quant" / "ashare_handlers.py").read_text(encoding="utf-8")
    assert '@app.get("/config/ashare/params.md")' in src
    assert "text/markdown" in src
