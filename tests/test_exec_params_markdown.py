"""执行参数 markdown 快照（2026-11-02）。

页面「📋 复制参数 Markdown」→ `GET /config/exec/params.md`。
约束：字段列表从 PARAM_META 自动生成、只含配置值、需重启的字段打 ⚠️、
交易对尽力解析（失败给 —，绝不抛）。
"""

from datetime import datetime, timezone
from pathlib import Path

from nanobot_quant.exec_params import (
    DEFAULT_EXEC_PARAMS,
    GROUP_TITLES,
    PARAM_META,
    RENDER_SKIP_GROUPS,
)
from nanobot_quant.exec_params_markdown import RESTART_REQUIRED, render_exec_params_md

HANDLERS = (Path(__file__).resolve().parents[1] / "src" / "nanobot_quant"
            / "exec_params_handlers.py")
FIXED_NOW = datetime(2026, 11, 2, 5, 1, 23, tzinfo=timezone.utc)   # → CST 13:01:23
PAIRS = {"SOL": ("SOL_USDT", "SOL-USDC"), "CRCL": ("XCRCL_USDT", "XCRCL-USDC")}


def _params(**over):
    p = dict(DEFAULT_EXEC_PARAMS)
    p["execution_channel"] = "okx_cex"
    p["quote_ccy"] = "USDC"
    p["td_symbols"] = ["SOL", "CRCL"]
    for k, v in over.items():
        p[k] = v
    return p


def _render(**over):
    return render_exec_params_md(_params(**over), now=FIXED_NOW, pairs=PAIRS)


def test_header_has_utc_and_cst():
    md = _render()
    assert "# 执行参数快照" in md
    assert "2026-11-02 05:01:23 UTC" in md
    assert "2026-11-02 13:01:23 CST" in md      # UTC+8


def test_all_param_meta_fields_rendered():
    """PARAM_META 的每个（当前通道可见的）字段都要出现——不能漏。"""
    md = _render()
    for key, meta in PARAM_META.items():
        if str(meta.get("group")) in RENDER_SKIP_GROUPS:
            continue
        label = str(meta.get("label", key))
        assert label in md, f"字段缺失: {key} -> {label}"


def test_group_titles_rendered():
    md = _render()
    for g, title in GROUP_TITLES.items():
        if g in RENDER_SKIP_GROUPS:
            continue
        assert title in md, f"分组缺失: {g}"


def test_restart_required_field_flagged():
    """计价币改后需重启循环才生效 —— 必须在快照里显式提醒。"""
    assert "quote_ccy" in RESTART_REQUIRED
    md = _render()
    line = next(ln for ln in md.splitlines() if "计价币" in ln)
    assert "⚠️" in line
    assert "重新激活" in line


def test_scene_three_columns():
    md = _render()
    assert "高频（1m）" in md and "中频（15m/1H）" in md and "低频（1D）" in md
    # 场景未填值 → 明确「跟随全局」，避免误读成空/0
    assert "（跟随全局）" in md


def test_scene_values_reflect_config():
    p = _params()
    p["scenes"] = dict(DEFAULT_EXEC_PARAMS.get("scenes") or {})
    p["scenes"]["high"] = dict(p["scenes"].get("high") or {},
                               enabled=True, wave_max_buys=3, entry_setup=8)
    md = render_exec_params_md(p, now=FIXED_NOW, pairs=PAIRS)
    row = next(ln for ln in md.splitlines() if "每波建仓上限" in ln)
    assert "3" in row
    entry = next(ln for ln in md.splitlines() if "入场 Setup 阈值" in ln)
    assert "8" in entry


def test_symbol_pairs_rendered():
    md = _render()
    assert "`SOL_USDT`" in md and "`SOL-USDC`" in md
    assert "`XCRCL_USDT`" in md and "`XCRCL-USDC`" in md
    # 列头带当前计价币
    assert "OKX 交易对（USDC）" in md


def test_symbol_pair_failure_degrades():
    """解析失败必须显示 —，不能抛异常、也不能留空。"""
    md = render_exec_params_md(_params(), now=FIXED_NOW,
                               pairs={"SOL": ("—", "—"), "CRCL": ("—", "—")})
    assert "`—`" in md


def test_bool_and_none_labels():
    md = _render()
    assert "✅ 开" in md and "⚪ 关" in md


def test_no_signal_or_ledger_content():
    """只含参数，不含台账/持仓/资金数据（那是状态不是参数）。"""
    md = _render()
    for word in ("batches.", "余额", "资金小表", "持仓批次"):
        assert word not in md, f"快照混入了状态类内容: {word}"


def test_other_channel_fields_marked():
    """非当前通道的字段仍列出（与页面一致：全渲染 + 前端显隐），但必须标注。"""
    md = _render()
    line = next((ln for ln in md.splitlines() if "滑点容忍" in ln), None)
    assert line is not None
    assert "仅 `dex` 通道" in line


def test_batches_label_follows_channel():
    """「批次数量」在 CEX 通道必须叫「子账号」——页面按通道切，快照不能写成子钱包。"""
    cex = _render()          # execution_channel=okx_cex
    assert "批次数量（子账号）" in cex
    assert "批次数量（子钱包）" not in cex

    dex = render_exec_params_md(_params(execution_channel="okx_dex"),
                                now=FIXED_NOW, pairs=PAIRS)
    assert "批次数量（子钱包）" in dex
    assert "批次数量（子账号）" not in dex


def test_route_registered():
    src = HANDLERS.read_text(encoding="utf-8")
    assert '"/config/exec/params.md", _params_md' in src
    assert "asyncio.to_thread(render_exec_params_md" in src, (
        "交易对解析会走网络，必须丢线程池，不能堵事件循环"
    )
