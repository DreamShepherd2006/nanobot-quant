"""options_history 单测 —— 全部离线，不发网络请求。

锁定的是**实测事实**（2026-09-17 逐端点验证得到），不是实现细节：
北京日边界、UA 必需、窗口放宽与分段、缓存零网络命中、坏 zip 不入缓存、
解析只吐成交行且按家族过滤。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from pathlib import Path

import pytest

from nanobot_quant import options_history as oh

TRADE_ZIP_NAME = "alloption-trades-2026-09-15.csv"
ENTRY = {
    "filename": "alloption-trades-2026-09-15.zip",
    "url": "https://static.okx.com/cdn/x/alloption-trades-2026-09-15.zip",
    "size_mb": 0.22,
}


# ─────────────────────── 北京日 ↔ UTC 毫秒 ───────────────────────

def test_cn_day_start_ms_matches_live_archive():
    """实测真值：dateTs=1789401600000 ↔ 文件名 alloption-trades-2026-09-15.zip。

    该时间戳是 2026-09-14T16:00Z，即北京 09-15 00:00 —— 归档按 UTC+8 切日。
    """
    assert oh.cn_day_start_ms("2026-09-15") == 1789401600000
    assert oh.cn_day_start_ms(dt.date(2026, 9, 15)) == 1789401600000
    assert dt.datetime.fromtimestamp(1789401600, tz=dt.timezone.utc) == \
        dt.datetime(2026, 9, 14, 16, 0, tzinfo=dt.timezone.utc)


def test_cn_day_of_is_inverse_of_start_ms():
    assert oh.cn_day_of(1789401600000) == "2026-09-15"
    for day in ("2026-09-10", "2026-01-01", "2025-12-31", "2026-12-31"):
        assert oh.cn_day_of(oh.cn_day_start_ms(day)) == day


def test_cn_day_boundaries_are_half_open():
    base = oh.cn_day_start_ms("2026-09-15")
    assert oh.cn_day_of(base - 1) == "2026-09-14"
    assert oh.cn_day_of(base) == "2026-09-15"
    assert oh.cn_day_of(base + oh.DAY_MS - 1) == "2026-09-15"
    assert oh.cn_day_of(base + oh.DAY_MS) == "2026-09-16"


# ────────────────────────── UA 必需 ──────────────────────────

def test_user_agent_is_set_and_not_urllib_default():
    """默认 urllib UA 会被 WAF 判 403（实测）—— 这条是防回归的硬线。"""
    assert oh._UA
    assert "urllib" not in oh._UA.lower()


def test_http_get_sends_user_agent(monkeypatch):
    seen: dict[str, str] = {}

    class _Resp:
        def read(self) -> bytes:
            return b'{"code":"0","data":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        seen["ua"] = req.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(oh.urllib.request, "urlopen", fake_urlopen)
    oh._http_get("https://example.invalid/x", timeout=1.0, what="t")
    assert seen["ua"] == oh._UA


def test_http_json_raises_on_nonzero_code(monkeypatch):
    monkeypatch.setattr(oh, "_http_get",
                        lambda *a, **k: b'{"code":"51000","msg":"bad"}')
    with pytest.raises(oh.OptionsHistoryError):
        oh._http_json("https://example.invalid", timeout=1.0, what="t")


def test_http_json_raises_on_non_json(monkeypatch):
    monkeypatch.setattr(oh, "_http_get", lambda *a, **k: b"<html>403</html>")
    with pytest.raises(oh.OptionsHistoryError):
        oh._http_json("https://example.invalid", timeout=1.0, what="t")


# ─────────────────────── list_archives ───────────────────────

def _entry(day: str) -> dict:
    return {
        "filename": f"alloption-trades-{day}.zip",
        "url": f"https://cdn.invalid/{day}.zip",
        "sizeMB": "0.22",
        "dateTs": str(oh.cn_day_start_ms(day)),
    }


def test_list_archives_dedups_and_sorts(monkeypatch):
    calls: list[str] = []

    def fake_json(url, *, timeout, what):  # noqa: ARG001
        calls.append(url)
        return {"code": "0", "data": [{"details": [{"groupDetails": [
            _entry("2026-09-14"), _entry("2026-09-15"), _entry("2026-09-14")]}]}]}

    monkeypatch.setattr(oh, "_http_json", fake_json)
    out = oh.list_archives(oh.cn_day_start_ms("2026-09-14"),
                           oh.cn_day_start_ms("2026-09-16"))
    assert [e["day"] for e in out] == ["2026-09-14", "2026-09-15"]
    assert out[0]["size_mb"] == 0.22
    assert len(calls) == 1


def test_list_archives_segments_long_windows(monkeypatch):
    """单次窗口 ≤ 8 天（官方上限）—— 29 天的区间必须切成多段请求。"""
    calls: list[str] = []

    def fake_json(url, *, timeout, what):  # noqa: ARG001
        calls.append(url)
        return {"code": "0", "data": []}

    monkeypatch.setattr(oh, "_http_json", fake_json)
    oh.list_archives(oh.cn_day_start_ms("2026-09-01"),
                     oh.cn_day_start_ms("2026-09-30"))
    assert len(calls) >= 4


def test_list_archives_rejects_inverted_window():
    with pytest.raises(oh.OptionsHistoryError):
        oh.list_archives(1000, 1000)
    with pytest.raises(oh.OptionsHistoryError):
        oh.list_archives(2000, 1000)


def test_list_archives_propagates_failure(monkeypatch):
    """不静默降级：某段拉不到就整体报错，让调用方看见缺了哪天。"""

    def boom(url, *, timeout, what):  # noqa: ARG001
        raise oh.OptionsHistoryError("网络炸了")

    monkeypatch.setattr(oh, "_http_json", boom)
    with pytest.raises(oh.OptionsHistoryError, match="网络炸了"):
        oh.list_archives(oh.cn_day_start_ms("2026-09-14"),
                         oh.cn_day_start_ms("2026-09-16"))


def test_day_from_filename_only_accepts_archive_names():
    assert oh._day_from_filename("alloption-trades-2026-09-15.zip") == "2026-09-15"
    assert oh._day_from_filename("alloption-candlesticks-2026-09-15.zip") == "2026-09-15"
    assert oh._day_from_filename("random.zip") is None
    assert oh._day_from_filename("") is None


# ─────────────────────── fetch_archive ───────────────────────

def _zip_bytes(rows: list[dict] | None = None) -> bytes:
    rows = rows if rows is not None else [{
        "instrument_name": "SOL-USD_UM-260918-94-P", "trade_id": "1", "side": "sell",
        "price": "0.62", "size": "2", "created_time": "1789406460000", "source": "0"}]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(oh.TRADE_COLUMNS))
    w.writeheader()
    for r in rows:
        w.writerow(r)
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        zf.writestr(TRADE_ZIP_NAME, buf.getvalue())
    return bio.getvalue()


def test_fetch_archive_cache_hit_makes_no_request(tmp_path, monkeypatch):
    dest = tmp_path / ENTRY["filename"]
    dest.write_bytes(_zip_bytes())
    hit: list[str] = []
    monkeypatch.setattr(oh, "_http_get", lambda *a, **k: hit.append("x") or b"")
    out = oh.fetch_archive(ENTRY, dest_dir=tmp_path)
    assert out == dest
    assert hit == []          # 缓存命中 = 零网络


def test_fetch_archive_downloads_validates_and_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(oh, "_http_get", lambda *a, **k: _zip_bytes())
    out = oh.fetch_archive(ENTRY, dest_dir=tmp_path)
    assert out.exists()
    assert not list(tmp_path.glob("*.part"))     # 临时文件已原子替换
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == [TRADE_ZIP_NAME]


def test_fetch_archive_rejects_bad_zip_and_leaves_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(oh, "_http_get", lambda *a, **k: b"<html>not a zip</html>")
    with pytest.raises(oh.OptionsHistoryError, match="不是有效 zip"):
        oh.fetch_archive(ENTRY, dest_dir=tmp_path)
    assert not (tmp_path / ENTRY["filename"]).exists()    # 坏文件绝不进缓存
    assert not list(tmp_path.glob("*.part"))


def test_fetch_range_reports_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(oh, "list_archives", lambda *a, **k: [
        dict(ENTRY, day="2026-09-15"),
        dict(ENTRY, day="2026-09-16",
             filename="alloption-trades-2026-09-16.zip", url="https://cdn.invalid/b.zip")])
    monkeypatch.setattr(oh, "_http_get", lambda *a, **k: _zip_bytes())
    lines: list[str] = []
    out = oh.fetch_range(0, 1, dest_dir=tmp_path, progress=lines.append)
    assert len(out) == 2
    assert any("归档共 2 天" in s for s in lines)     # 取数过程必须留痕
    assert any("[1/2]" in s for s in lines)


# ─────────────────────────── 解析 ───────────────────────────

def _write_zip(tmp_path: Path, rows: list[dict], name: str = TRADE_ZIP_NAME) -> Path:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(oh.TRADE_COLUMNS))
    w.writeheader()
    for r in rows:
        w.writerow(r)
    p = tmp_path / "a.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(name, buf.getvalue())
    return p


ROWS = [
    {"instrument_name": "SOL-USD_UM-260918-94-P", "trade_id": "1", "side": "sell",
     "price": "0.62", "size": "2", "created_time": "1789406460000", "source": "0"},
    {"instrument_name": "SOL-USD_UM-260918-94-P", "trade_id": "2", "side": "buy",
     "price": "0.60", "size": "1", "created_time": "1789406100000", "source": "0"},
    {"instrument_name": "BTC-USD_UM-260918-70000-P", "trade_id": "3", "side": "sell",
     "price": "900", "size": "1", "created_time": "1789406400000", "source": "0"},
    {"instrument_name": "SOL-USD_UM-260918-96-P", "trade_id": "4", "side": "sell",
     "price": "0.8", "size": "0", "created_time": "1789406500000", "source": "0"},
]


def test_iter_trades_filters_family_and_zero_size(tmp_path):
    p = _write_zip(tmp_path, ROWS)
    got = list(oh.iter_trades(p, family="SOL-USD_UM"))
    # BTC 被家族过滤；size=0 的那行被剔除
    assert [r["trade_id"] for r in got] == ["1", "2"]
    assert got[0]["_source_file"] == "a.zip"


def test_iter_trades_without_family_keeps_all_non_zero(tmp_path):
    p = _write_zip(tmp_path, ROWS)
    assert [r["trade_id"] for r in oh.iter_trades(p)] == ["1", "2", "3"]


def test_iter_trades_limit(tmp_path):
    p = _write_zip(tmp_path, ROWS)
    assert len(list(oh.iter_trades(p, limit=1))) == 1


def test_iter_trades_accepts_single_path_and_list(tmp_path):
    p = _write_zip(tmp_path, ROWS)
    assert len(list(oh.iter_trades(p))) == 3
    assert len(list(oh.iter_trades([p, p]))) == 6


def test_group_by_contract_sorts_by_time(tmp_path):
    p = _write_zip(tmp_path, ROWS)
    grouped = oh.group_by_contract(oh.iter_trades(p, family="SOL-USD_UM"))
    assert set(grouped) == {"SOL-USD_UM-260918-94-P"}
    assert [r["trade_id"] for r in grouped["SOL-USD_UM-260918-94-P"]] == ["2", "1"]


def test_family_of_handles_um_suffix():
    """尾部固定 3 段、从右侧切 —— ``_UM`` 会破坏左侧段位假设。"""
    assert oh._family_of("SOL-USD_UM-260918-94-P") == "SOL-USD_UM"
    assert oh._family_of("BTC-USD-260918-70000-C") == "BTC-USD"
    assert oh._family_of("garbage") == ""


# ─────────────────── 复用而非重写：parse_inst_id ───────────────────

def test_parse_inst_id_delegates_to_options_data():
    """已到期合约的 strike/到期只能反解 —— 两处各写一份必然漂移。"""
    from nanobot_quant.okx_options_data import parse_inst_id as impl
    got = oh.parse_inst_id("SOL-USD_UM-260918-94-P")
    assert got == impl("SOL-USD_UM-260918-94-P")
    assert got["instFamily"] == "SOL-USD_UM"
    assert got["stk"] == "94"
    assert got["optType"] == "P"


def test_module_level_imports_stay_stdlib_only():
    """模块级不得拖入 data_sources 重依赖链（yfinance / pandas 等）。

    okx_options_data 会 import data_sources（含 yfinance）—— 归档取数层不需要它，
    一旦有人把它挪回模块级，测试容器裸跑（非 pytest，无 conftest stub）就会炸。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(oh))
    top_modules = {n.module or "" for n in tree.body if isinstance(n, ast.ImportFrom)}
    assert not any("okx_options_data" in m for m in top_modules), top_modules


def test_parse_inst_id_is_lazy_import():
    """延迟导入：不跑 pytest 也能 import 本模块（依赖限于标准库）。"""
    import ast
    import inspect

    src = inspect.getsource(oh.parse_inst_id)
    assert "okx_options_data" in src
    assert any(isinstance(n, ast.ImportFrom) for n in ast.walk(ast.parse(src)))
