"""展示层与执行层同源的结构性断言（2026-11-05 三小修批次）。

覆盖：
1. C92 — ``OkxCexBroker.inst_id_of`` 的进程级日志节流 key 必须含**实际生效的
   计价币**。只按 symbol 节流时，把计价币从 USDT 切到 USDC 后不再重打，
   日志会一直留着旧对（如 ``SOL → SOL-USDT`` 而实际下单走 ``SOL-USDC``），
   排查时极易误判成「配置没生效」。
2. C99 — TD 页顶部徽标必须区分「目标变体」（strategy.json）与「执行层运行中
   变体」（LIVE_STATE["strategy_variant"]）：TD 循环构造时只读一次 strategy.json,
   改完不重启仍是旧变体，页面写死「执行层同步使用」会用错误口径误导读数。
3. C108 — SKIP 原因键的中文映射在 ``backtest_markdown._SKIP_LABELS`` 与
   ``backtest_page.html`` 的 ``SKIP_LABELS`` 两处**必须键集合一致**：任一表漏键
   就会在页面/报告里原样输出英文键名（静默降级成看不懂的键）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nanobot_quant import backtest_markdown as bm
from nanobot_quant.brokers import okx_cex_broker as ob
from nanobot_quant.strategies.registry import get_strategy
from nanobot_quant.td_table_handlers import _strategy_badge

_SRC = Path(bm.__file__).resolve().parent


# ── C92：日志节流 key 含计价币 ──────────────────────────────────────────


@pytest.fixture()
def _clean_pair_logged():
    ob._PAIR_LOGGED.clear()
    yield
    ob._PAIR_LOGGED.clear()


def _mk_broker(quote: str):
    """手工构造 broker（绕过 __init__ 的凭证校验）。"""
    b = object.__new__(ob.OkxCexBroker)
    b._quote = quote
    b._tokens_json = None
    b._sub_account = None
    b._slippage = 0.1
    return b


def test_pair_log_throttle_key_includes_quote(monkeypatch, capsys, _clean_pair_logged):
    monkeypatch.setattr("nanobot_quant.gate_credentials.okx_ticker",
                        lambda s, t=None: s)
    monkeypatch.setattr(ob, "inst_id_for",
                        lambda base, quote=None, strict=False: f"{base}-{quote}")
    monkeypatch.setattr(ob, "spot_inst_ids", lambda: frozenset())

    b = _mk_broker("USDT")
    b.inst_id_of("SOL")
    b.inst_id_of("SOL")
    first = capsys.readouterr().err
    assert first.count("OKX-CEX pair") == 1, "同 symbol 同计价币应只打一次"

    # 切计价币 → 必须重打（旧实现按 symbol 节流，这里会得到 0 行）
    b._quote = "USDC"
    b.inst_id_of("SOL")
    second = capsys.readouterr().err
    assert second.count("OKX-CEX pair") == 1
    assert "SOL-USDC" in second


def test_pair_log_throttle_still_dedups_across_broker_instances(
        monkeypatch, capsys, _clean_pair_logged):
    monkeypatch.setattr("nanobot_quant.gate_credentials.okx_ticker",
                        lambda s, t=None: s)
    monkeypatch.setattr(ob, "inst_id_for",
                        lambda base, quote=None, strict=False: f"{base}-{quote}")
    monkeypatch.setattr(ob, "spot_inst_ids", lambda: frozenset())

    # broker 在部分路径会被重建：进程级节流不应因实例重建而重打
    _mk_broker("USDT").inst_id_of("SOL")
    _mk_broker("USDT").inst_id_of("SOL")
    assert capsys.readouterr().err.count("OKX-CEX pair") == 1


# ── C99：badge 三态 ────────────────────────────────────────────────────


def test_strategy_badge_three_states():
    futu, orig = "td_sequential_futu", "td_sequential"
    lf, lo = get_strategy(futu).label, get_strategy(orig).label

    # ① TD 未运行：只能说「下次启动将使用」
    idle = _strategy_badge(futu, lf, "")
    assert "未运行" in idle and "运行中" not in idle

    # ② 目标与执行层一致
    same = _strategy_badge(futu, lf, futu)
    assert "执行层运行中" in same and "⚠️" not in same

    # ③ 不一致：必须显式告警，且两个 label 都要出现（不能只显示目标值）
    diff = _strategy_badge(futu, lf, orig)
    assert "⚠️" in diff and "执行层仍为" in diff
    assert lf in diff and lo in diff
    assert "/config/exec" in diff, "必须指引用户去哪里重启循环"


def test_strategy_badge_unknown_running_variant_does_not_crash():
    """运行中变体不在注册表（历史值/手改文件）时不得抛异常。"""
    out = _strategy_badge("td_sequential_futu", "富途", "some_removed_variant")
    assert "⚠️" in out and "some_removed_variant" in out


# ── C108：两张 SKIP 映射表键集合一致 ───────────────────────────────────


def _page_skip_labels() -> dict[str, str]:
    html = (_SRC / "backtest_page.html").read_text(encoding="utf-8")
    m = re.search(r"const SKIP_LABELS = \{(.*?)\n\};", html, re.S)
    assert m, "backtest_page.html 里找不到 const SKIP_LABELS"
    body = m.group(1)
    body = re.sub(r"//[^\n]*", "", body)  # 去掉行注释
    pairs = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\"([^\"]*)\"", body)
    return dict(pairs)


def test_skip_labels_tables_in_sync():
    page = _page_skip_labels()
    md = dict(bm._SKIP_LABELS)
    assert set(page) == set(md), (
        f"两表键集合不一致：仅页面有 {sorted(set(page) - set(md))}，"
        f"仅 markdown 有 {sorted(set(md) - set(page))}"
    )
    # 同一键的文案也必须一致（否则页面与报告读起来是两回事）
    diff = {k: (md[k], page[k]) for k in md if md[k] != page[k]}
    assert not diff, f"同一键文案不一致：{diff}"


def test_entry_gate_skip_labels_present():
    """C108：入场门四键此前两表都漏映射，会原样显示英文键名。"""
    keys = ("entry_gap", "entry_gap_na", "entry_ema", "entry_ema_na")
    page = _page_skip_labels()
    for k in keys:
        assert k in bm._SKIP_LABELS, f"{k} 缺 markdown 映射"
        assert k in page, f"{k} 缺页面映射"
        assert re.search(r"[\u4e00-\u9fff]", bm._SKIP_LABELS[k]), f"{k} 应给中文文案"


# ── 买入手数为 0 的可见化（2026-11-05）────────────────────────────────


def _mk_strategy_for_skip():
    """手工构造策略（绕过 lumibot 初始化），只测 SKIP 可见化路径。"""
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    s = object.__new__(TdSequentialStrategy)
    s.symbol = "SOL"
    s.quantity_mode = "fixed"
    s.quantity = None
    s.fixed_amount = 10.0
    s._skip_counts = {}
    s.events = []
    s._record = lambda ev, note="", **kw: s.events.append((ev, note, kw))
    return s


def test_zero_qty_emits_skip_event_and_count():
    """qty=0 曾完全静默：4 个 slot 依次 return None，上层只报「无可用 slot」。"""
    s = _mk_strategy_for_skip()
    s._skip_zero_qty({"slot": 2}, 92.46, 0.0)

    assert s._skip_counts == {"qty_zero": 1}
    assert len(s.events) == 1
    ev, note, kw = s.events[0]
    assert ev == "SKIP"
    assert "quantity_mode=fixed" in note and "price=92.460000" in note
    assert kw.get("symbol") == "SOL"


def test_both_buy_paths_use_zero_qty_helper():
    """DEX 与 CEX 两条 BUY 路径都必须走同一 helper（不留第二套写法）。"""
    src = (Path(bm.__file__).resolve().parent
           / "strategies" / "td_sequential_strategy.py").read_text(encoding="utf-8")
    assert src.count("self._skip_zero_qty(slot, price, qty)") == 2, (
        "qty<=0 的静默 return 必须只在两处 BUY 路径统一改为 _skip_zero_qty"
    )
    assert "if qty <= 0:\n                return None" not in src, (
        "仍有裸 `if qty <= 0: return None` 未补事件"
    )


def test_eod_hold_records_bar_level_event():
    """EOD 跳过此前只有 skip_counts，bars_rows 里查不到任何 EOD 痕迹。"""
    src = (Path(bm.__file__).resolve().parent
           / "strategies" / "td_sequential_strategy.py").read_text(encoding="utf-8")
    marker = 'self._bump_skip("eod_hold")'
    idx = src.index(marker)
    tail = src[idx: idx + 800]
    assert 'self._record(' in tail and '"SKIP"' in tail
    assert "eod_hold:" in tail, "SKIP 文案需带 eod_hold 前缀，便于逐 bar 表定位"


def test_qty_zero_label_in_both_tables():
    page = _page_skip_labels()
    assert "qty_zero" in bm._SKIP_LABELS
    assert "qty_zero" in page
    assert bm._SKIP_LABELS["qty_zero"] == page["qty_zero"]
