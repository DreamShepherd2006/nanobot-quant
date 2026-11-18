"""`/config/mode` 页面：交易模式未保存时的提示。

语义：
- `mode.json` 不存在 / 坏 JSON / mode 为空 → saved=False，页面出黄条提示保存，
  卡片徽标显示「当前（默认）」，当前模式标签追加「未保存 · 默认」
- 已保存 → 无黄条、徽标回到「当前」
- 读取函数本身不再改变"默认按 quant 处理"的既有业务语义
"""

import json
import types

import pytest

from nanobot_quant import mode_handlers as mh


class _FakeApp:
    def __init__(self):
        self.routes = {}

    def add_route(self, path, fn, methods=None):
        for m in methods or ["GET"]:
            self.routes[(path, m)] = fn


@pytest.fixture
def gatekeeper(tmp_path):
    return types.SimpleNamespace(
        _platform=types.SimpleNamespace(
            data_root=str(tmp_path), is_commander=lambda u: True
        )
    )


@pytest.fixture
def page_fn(gatekeeper):
    app = _FakeApp()
    mh.register_mode_routes(app, gatekeeper)
    return app.routes[("/config/mode", "GET")]


def _req(user="DreamShepherd2006"):
    return types.SimpleNamespace(session={"user": user})


def _render(fn):
    import asyncio

    resp = asyncio.run(fn(_req()))
    return resp.body.decode()


def test_read_mode_state_missing_file(gatekeeper):
    assert mh._read_mode_state(gatekeeper) == (None, False)


def test_read_mode_state_saved(gatekeeper, tmp_path):
    (tmp_path / "legion").mkdir()
    (tmp_path / "legion" / "mode.json").write_text(json.dumps({"mode": "research"}))
    assert mh._read_mode_state(gatekeeper) == ("research", True)


def test_read_mode_state_blank_mode_is_unsaved(gatekeeper, tmp_path):
    (tmp_path / "legion").mkdir()
    (tmp_path / "legion" / "mode.json").write_text(json.dumps({"mode": "  "}))
    assert mh._read_mode_state(gatekeeper) == (None, False)


def test_read_mode_keeps_quant_default(gatekeeper):
    assert mh._read_mode(gatekeeper) == "quant"


def test_page_shows_banner_when_unsaved(gatekeeper, page_fn):
    html = _render(page_fn)
    assert "当前交易模式尚未保存" in html
    assert "当前（默认）" in html
    assert "未保存 · 默认" in html


def test_page_hides_banner_when_saved(gatekeeper, page_fn, tmp_path):
    (tmp_path / "legion").mkdir()
    (tmp_path / "legion" / "mode.json").write_text(json.dumps({"mode": "research"}))
    html = _render(page_fn)
    assert "当前交易模式尚未保存" not in html
    assert "✓ 当前" in html
    assert "当前（默认）" not in html
    assert 'class="mode-card active"' in html  # research 卡片高亮


def test_page_marks_default_card_active_when_unsaved(page_fn):
    html = _render(page_fn)
    # Quant 卡片带 active（默认按 quant 处理），Research 卡片不带
    assert 'class="mode-card active" onclick="switchMode(\'quant\')"' in html
    assert 'class="mode-card" onclick="switchMode(\'research\')"' in html
