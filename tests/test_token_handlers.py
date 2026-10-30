"""Unit tests for token WebUI handlers (tokens.json management).

Covers the A+C confirmation scheme surface exposed by ``/config/tokens``:
- routing registration (5 endpoints, same plugin pattern as credentials)
- add: clean entry saved as confirmed=false; questionable entry (EVM addr
  on solana) saved but flagged needs_confirmation; duplicates / built-in
  whitelist / empty fields rejected
- confirm: marks an entry confirmed (resolve_token then passes it)
- edit: changing the address resets confirmation (no stale bypass)
- delete: removes the entry
- ``tools_research_chain._load_tokens_json`` feeds tokens.json into the
  fail-closed pre-check and TD check (L2 tier honoured)

Async handlers are driven via asyncio.run() — plain-sync pytest, no
pytest-asyncio dependency.
"""

import asyncio
import json

import pytest

from nanobot_quant import onchainos_cli
from nanobot_quant.token_handlers import (
    _CHAINS,
    _read_tokens,
    _write_tokens,
    register_token_routes,
)
from nanobot_quant.tools import tools_research_chain


class TestBuiltinChainsCoverage:
    """L1 内建白名单的主链必须都能在代币页链下拉里选到。"""

    def test_builtin_chains_in_chaIns(self):
        from nanobot_quant.onchainos_cli import _BUILTIN_TOKENS

        missing = sorted(
            {b["chain"] for b in _BUILTIN_TOKENS.values()} - set(_CHAINS)
        )
        assert not missing, f"代币页链下拉缺少内置币主链: {missing}"

    def test_page_dropdown_has_builtin_chains(self):
        import pathlib

        from nanobot_quant.onchainos_cli import _BUILTIN_TOKENS

        html = pathlib.Path(__file__).resolve().parents[1] / "src" / "nanobot_quant" / "token_page.html"
        text = html.read_text()
        # 只取新增表单的第一个下拉（f-chain）
        f_chain = text.split('<select id="f-chain">', 1)[1].split("</select>", 1)[0]
        missing = sorted(
            {b["chain"] for b in _BUILTIN_TOKENS.values()} - {l.strip().split('"')[1] for l in f_chain.splitlines() if "<option" in l}
        )
        assert not missing, f"f-chain 下拉缺少内置币主链: {missing}"

SOLANA_ADDR = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
EVM_ADDR = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"


class _FakeApp:
    """Captures routes registered by register_token_routes."""

    def __init__(self):
        self.routes = []

    def get(self, path):
        def deco(handler):
            self.routes.append(("GET", path, handler))
            return handler

        return deco

    def post(self, path):
        def deco(handler):
            self.routes.append(("POST", path, handler))
            return handler

        return deco


class _FakeRequest:
    def __init__(self, body=None):
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _isolated_tokens(tmp_path, monkeypatch):
    """Point token_json_path (both import sites) at a temp file."""
    target = tmp_path / "tokens.json"

    def fake_path():
        return target

    monkeypatch.setattr(onchainos_cli, "token_json_path", fake_path)
    monkeypatch.setattr(
        "nanobot_quant.token_handlers.token_json_path", fake_path
    )
    yield target


def _call(handler, body=None):
    return asyncio.run(handler(_FakeRequest(body)))


class TestRouteRegistration:
    def test_six_endpoints(self):
        app = _FakeApp()
        register_token_routes(app, None)
        paths = sorted((m, p) for m, p, _ in app.routes)
        assert paths == sorted(
            [
                ("GET", "/config/tokens"),
                ("POST", "/config/tokens/add"),
                ("POST", "/config/tokens/confirm"),
                ("POST", "/config/tokens/edit"),
                ("POST", "/config/tokens/meta"),
                ("POST", "/config/tokens/delete"),
            ]
        )


class TestAdd:
    def test_add_clean_entry(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add

        resp = _call(
            token_add,
            {"symbol": "wEvm", "address": SOLANA_ADDR, "chain": "solana"},
        )
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert data["status"] == "clean"
        entries = _read_tokens()
        assert entries == [
            {"symbol": "WEVM", "address": SOLANA_ADDR,
             "chain": "solana", "confirmed": False, "min_hold": 0.0}
        ]

    def test_add_questionable_entry_flagged(self, _isolated_tokens):
        """EVM address on solana is saved but must NOT pass the gate."""
        from nanobot_quant.token_handlers import token_add

        resp = _call(token_add, {"symbol": "WEVM", "address": EVM_ADDR,
                                 "chain": "solana"})
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert data["status"] == "needs_confirmation"
        assert "issue" in data

        # resolution must be blocked until confirmed (fail-closed)
        resolved = onchainos_cli.resolve_token(
            "WEVM", tokens_json=_read_tokens(), chain="solana"
        )
        assert resolved["ok"] is True          # address is resolvable…
        assert resolved["needs_confirmation"] is True  # …but gated

    def test_add_duplicate_rejected(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add

        _call(token_add, {"symbol": "WEVM", "address": SOLANA_ADDR,
                          "chain": "solana"})
        resp = _call(token_add, {"symbol": "wevm", "address": SOLANA_ADDR,
                                 "chain": "solana"})
        assert resp.status_code == 400

    def test_add_builtin_sol_registered(self, _isolated_tokens):
        """SOL (native coin) registers by symbol: address auto-filled from
        the builtin whitelist, confirmed=True, chain forced to solana."""
        from nanobot_quant.token_handlers import token_add

        resp = _call(token_add, {"symbol": "SOL", "address": "",
                                 "chain": "solana"})
        assert resp.status_code == 200, resp.body
        entries = _read_tokens()
        assert len(entries) == 1
        assert entries[0]["symbol"] == "SOL"
        assert entries[0]["address"] == onchainos_cli._BUILTIN_TOKENS["SOL"]["address"]
        assert entries[0]["chain"] == "solana"
        assert entries[0]["confirmed"] is True

    def test_add_builtin_sol_other_chain_rejected(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add

        resp = _call(token_add, {"symbol": "SOL", "address": "",
                                 "chain": "xlayer"})
        assert resp.status_code == 400
        assert _read_tokens() == []

    def test_add_stablecoin_rejected(self, _isolated_tokens):
        """USDC/USDT are stablecoins — no analysis value, kept out of the
        TD target management table."""
        from nanobot_quant.token_handlers import token_add

        for sym in ("USDC", "USDT"):
            resp = _call(token_add, {"symbol": sym, "address": "",
                                     "chain": "solana"})
            assert resp.status_code == 400
            assert "稳定币" in resp.body.decode()
        assert _read_tokens() == []

    def test_add_empty_fields_rejected(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add

        assert _call(token_add, {"symbol": "", "address": SOLANA_ADDR,
                                 "chain": "solana"}).status_code == 400
        assert _call(token_add, {"symbol": "WEVM", "address": "",
                                 "chain": "solana"}).status_code == 400


class TestConfirm:
    def test_confirm_unblocks_execution(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add, token_confirm

        _call(token_add, {"symbol": "WEVM", "address": EVM_ADDR,
                          "chain": "solana"})
        resp = _call(token_confirm, {"symbol": "WEVM", "address": EVM_ADDR})
        assert resp.status_code == 200
        assert json.loads(resp.body.decode())["ok"] is True

        resolved = onchainos_cli.resolve_token(
            "WEVM", tokens_json=_read_tokens(), chain="solana"
        )
        assert resolved["ok"] is True
        assert resolved["address"] == EVM_ADDR

    def test_confirm_wrong_address_rejected(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add, token_confirm

        _call(token_add, {"symbol": "WEVM", "address": EVM_ADDR,
                          "chain": "solana"})
        resp = _call(token_confirm, {"symbol": "WEVM",
                                     "address": "0xdeadbeef"})
        assert resp.status_code == 400
        resolved = onchainos_cli.resolve_token(
            "WEVM", tokens_json=_read_tokens(), chain="solana"
        )
        assert resolved["needs_confirmation"] is True


class TestEdit:
    def test_edit_resets_confirmation(self, _isolated_tokens):
        from nanobot_quant.token_handlers import (
            token_add,
            token_confirm,
            token_edit,
        )

        _call(token_add, {"symbol": "WEVM", "address": EVM_ADDR,
                          "chain": "solana"})
        _call(token_confirm, {"symbol": "WEVM", "address": EVM_ADDR})
        assert _read_tokens()[0]["confirmed"] is True

        # switch to a different (clean) address — confirmation must reset
        resp = _call(token_edit, {"symbol": "WEVM", "address": SOLANA_ADDR,
                                  "chain": "solana"})
        assert resp.status_code == 200
        assert json.loads(resp.body.decode())["status"] == "clean"
        entries = _read_tokens()
        assert entries[0]["address"] == SOLANA_ADDR
        assert entries[0]["confirmed"] is False


class TestDelete:
    def test_delete_removes_entry(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_add, token_delete

        _call(token_add, {"symbol": "WEVM", "address": SOLANA_ADDR,
                          "chain": "solana"})
        resp = _call(token_delete, {"symbol": "WEVM"})
        assert resp.status_code == 200
        assert _read_tokens() == []

    def test_delete_unknown_404(self, _isolated_tokens):
        from nanobot_quant.token_handlers import token_delete

        assert _call(token_delete, {"symbol": "NOPE"}).status_code == 404


class TestResearchChainTokensLoading:
    def test_load_tokens_json_feeds_resolve(self, tmp_path, monkeypatch):
        """The research-chain pre-check must see tokens.json (L2 tier)."""
        target = tmp_path / "tokens.json"
        target.write_text(
            json.dumps([{"symbol": "WEVM", "address": EVM_ADDR,
                         "chain": "solana", "confirmed": False}]),
            encoding="utf-8",
        )

        def fake_path():
            return target

        monkeypatch.setattr(onchainos_cli, "token_json_path", fake_path)

        tokens = tools_research_chain._load_tokens_json()
        assert tokens == [{"symbol": "WEVM", "address": EVM_ADDR,
                           "chain": "solana", "confirmed": False}]

        # resolve through the same path the pre-check uses: questionable
        # entry must surface as needs_confirmation (no CLI call needed)
        resolved = onchainos_cli.resolve_token(
            "WEVM", tokens_json=tokens, chain="solana"
        )
        assert resolved["ok"] is True
        assert resolved["needs_confirmation"] is True
        assert resolved["category"] == "chain_mismatch"


class TestRenderListGatePair:
    def test_gate_pair_column(self, _isolated_tokens):
        """tokens 页 Gate 交易对列：symbol 回退 + gate_symbol 覆盖。"""
        from nanobot_quant.token_handlers import _render_list

        _write_tokens(
            [
                {"symbol": "CRCLX", "address": SOLANA_ADDR,
                 "chain": "solana", "confirmed": True},
                {"symbol": "SPX", "address": SOLANA_ADDR,
                 "chain": "solana", "confirmed": True,
                 "gate_symbol": "XSPX"},
                {"symbol": "MU", "address": SOLANA_ADDR,
                 "chain": "solana", "confirmed": True},
            ]
        )
        html = _render_list()
        # 表头与回退对（symbol → {SYMBOL}_USDT）
        assert "<th>Gate 交易对</th>" in html
        assert ">CRCLX_USDT<" in html
        assert ">MU_USDT<" in html
        # gate_symbol 覆盖 symbol（精确匹配 td 单元格，避免 XSPX_USDT 含 SPX_USDT 子串）
        assert ">XSPX_USDT<" in html
        assert ">SPX_USDT<" not in html


class TestEditMappings:
    def test_edit_set_and_clear_pair_mappings(self, _isolated_tokens):
        """编辑弹窗：gate_symbol/okx_symbol 传值=设置（大写归一化）、传空=清除。"""
        from nanobot_quant.token_handlers import token_add, token_edit

        _call(token_add, {"symbol": "WEVM", "address": SOLANA_ADDR,
                          "chain": "solana"})
        resp = _call(token_edit, {"symbol": "WEVM", "address": SOLANA_ADDR,
                                  "chain": "solana",
                                  "gate_symbol": "xspx_usdt", "okx_symbol": "XSPX"})
        assert resp.status_code == 200
        e = _read_tokens()[0]
        assert e["gate_symbol"] == "XSPX_USDT"  # 大写归一化
        assert e["okx_symbol"] == "XSPX"
        # 留空 = 清除映射（回退 symbol）
        _call(token_edit, {"symbol": "WEVM", "address": SOLANA_ADDR,
                           "chain": "solana",
                           "gate_symbol": "", "okx_symbol": ""})
        e = _read_tokens()[0]
        assert "gate_symbol" not in e
        assert "okx_symbol" not in e

    def test_edit_pair_mapping_keeps_confirmation(self, _isolated_tokens):
        """只改交易对映射、地址未变 → 确认门不重置。"""
        from nanobot_quant.token_handlers import (
            token_add,
            token_confirm,
            token_edit,
        )

        _call(token_add, {"symbol": "WEVM", "address": SOLANA_ADDR,
                          "chain": "solana"})
        _call(token_confirm, {"symbol": "WEVM", "address": SOLANA_ADDR})
        _call(token_edit, {"symbol": "WEVM", "address": SOLANA_ADDR,
                           "chain": "solana", "gate_symbol": "XSPX"})
        assert _read_tokens()[0]["confirmed"] is True


class TestRenderListOkxColumn:
    """OKX 列必须显示「解析结果」而非登记值——页面与执行同源。

    2026-10-30 旧实现自己拼 ``-USDT``（忽略计价币）、不剥 quote 后缀、
    不走 X 前缀探测，导致 CRCLX 在页面上显示 CRCLX-USDT「看着正常」、
    实际执行 51001。现改为直调 ``inst_id_for``。
    """

    @staticmethod
    def _mock_okx(monkeypatch, mapping):
        """mapping: ticker(大写) -> instId；缺省视为解析失败（返回空串）。"""
        from nanobot_quant import exec_params, okx_cex_data

        monkeypatch.setattr(
            exec_params, "load_exec_params", lambda: {"quote_ccy": "USDC"}
        )
        monkeypatch.setattr(
            okx_cex_data,
            "inst_id_for",
            lambda ticker, quote=None, session=None, strict=False: mapping.get(
                str(ticker).upper(), ""
            ),
        )

    def test_okx_column_shows_resolved_inst(self, _isolated_tokens, monkeypatch):
        """登记 okx_symbol → 显示真实 instId（含当前计价币），表头标明计价币。"""
        from nanobot_quant.token_handlers import _render_list

        self._mock_okx(monkeypatch, {"XCRCL": "XCRCL-USDC", "SPCX": "XSPX-USDC"})
        _write_tokens(
            [
                {"symbol": "CRCLX", "address": SOLANA_ADDR,
                 "chain": "solana", "confirmed": True,
                 "okx_symbol": "XCRCL"},
                {"symbol": "SPCX", "address": SOLANA_ADDR,
                 "chain": "solana", "confirmed": True},
            ]
        )
        html = _render_list()
        assert "<th>OKX 交易对（USDC）</th>" in html  # 表头带实际计价币
        assert ">XCRCL-USDC<" in html
        assert ">XSPX-USDC<" in html                  # symbol 回退 + X 前缀探测
        assert "CRCLX-USDC" not in html               # 不再是「登记值直接拼」

    def test_okx_column_flags_unresolved(self, _isolated_tokens, monkeypatch):
        """真没这个标的 → 红字「OKX 无此标的」（与「仅其它计价币」区分）。"""
        from nanobot_quant.token_handlers import _render_list

        self._mock_okx(monkeypatch, {})  # 全部解析失败
        _write_tokens(
            [{"symbol": "SPYX", "address": SOLANA_ADDR,
              "chain": "solana", "confirmed": True}]
        )
        html = _render_list()
        assert "class='bad'" in html
        assert "OKX 无此标的" in html
        assert "SPYX-USDT" not in html  # 不再凭空造一个「看着对」的交易对

    def test_okx_column_marks_other_quote_only(self, _isolated_tokens, monkeypatch):
        """有别的计价币对（XAUT 只有 -USDT）→ 橙字，不是「无对应交易对」。

        两种情形处置完全不同：缺对 = 换计价币；真没标的 = 换标的。
        """
        from nanobot_quant import exec_params, okx_cex_data
        from nanobot_quant.token_handlers import _render_list

        monkeypatch.setattr(
            exec_params, "load_exec_params", lambda: {"quote_ccy": "USDC"}
        )
        monkeypatch.setattr(
            okx_cex_data,
            "inst_id_for",
            lambda t, quote=None, session=None, strict=False: (
                "XAUT-USDT" if str(t).upper() == "XAUT" and quote == "USDT" else ""
            ),
        )
        _write_tokens(
            [{"symbol": "XAUT", "address": SOLANA_ADDR,
              "chain": "ethereum", "confirmed": True}]
        )
        html = _render_list()
        assert "class='warn'" in html
        assert "仅有 XAUT-USDT" in html
        assert "当前计价币 USDC" in html
        assert "OKX 无此标的" not in html

    def test_gate_column_flags_nonexistent_pair(self, _isolated_tokens, monkeypatch):
        """Gate 格式对 ≠ 存在：MU_USDT 拼得出来但交易所没有，必须标红。"""
        from nanobot_quant import token_handlers
        from nanobot_quant.token_handlers import _render_list

        monkeypatch.setattr(
            token_handlers, "_gate_pairs", lambda *a, **kw: {"SOL_USDT", "BTC_USDT"}
        )
        _write_tokens(
            [
                {"symbol": "MU", "address": SOLANA_ADDR,
                 "chain": "solana", "confirmed": True},
                {"symbol": "SOL", "address": SOLANA_ADDR,
                 "chain": "solana", "confirmed": True},
            ]
        )
        html = _render_list()
        assert "❌ MU_USDT" in html            # 假绿变红
        assert "❌ SOL_USDT" not in html        # 真实存在的不动

    def test_gate_column_unchecked_when_listing_fails(
        self, _isolated_tokens, monkeypatch
    ):
        """拉不到清单时显式标「未校验」，不猜、也不因为网络问题误标红。"""
        from nanobot_quant import token_handlers
        from nanobot_quant.token_handlers import _render_list

        monkeypatch.setattr(token_handlers, "_gate_pairs", lambda *a, **kw: None)
        _write_tokens(
            [{"symbol": "SOL", "address": SOLANA_ADDR,
              "chain": "solana", "confirmed": True}]
        )
        html = _render_list()
        assert ">SOL_USDT<" in html
        assert "(未校验)" in html
        assert "❌" not in html

    def test_gate_pairs_cache_reused(self, monkeypatch):
        """全量清单 600s 缓存：第二次渲染不再发请求。"""
        import time

        from nanobot_quant import token_handlers as th

        calls = {"n": 0}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return [{"id": "SOL_USDT"}, {"id": "BTC_USDT"}]

        def fake_get(*a, **kw):
            calls["n"] += 1
            return _Resp()

        monkeypatch.setitem(th._gate_pairs_cache, "ts", 0.0)
        monkeypatch.setitem(th._gate_pairs_cache, "pairs", None)
        monkeypatch.setattr("requests.get", fake_get, raising=False)
        real = getattr(th, "_gate_pairs_orig", th._gate_pairs)  # 绕开 autouse 的 stub
        first = real()
        second = real()
        assert first == {"SOL_USDT", "BTC_USDT"} == second
        assert calls["n"] == 1  # 第二次命中缓存
        assert time.time() - th._gate_pairs_cache["ts"] < 5

    def test_okx_column_survives_network_error(self, _isolated_tokens, monkeypatch):
        """拉清单抛异常时只降级标红，绝不把整个页面打成 500。"""
        from nanobot_quant import exec_params, okx_cex_data
        from nanobot_quant.token_handlers import _render_list

        monkeypatch.setattr(
            exec_params, "load_exec_params", lambda: {"quote_ccy": "USDC"}
        )

        def boom(*a, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(okx_cex_data, "inst_id_for", boom)
        _write_tokens(
            [{"symbol": "SOL", "address": SOLANA_ADDR,
              "chain": "solana", "confirmed": True}]
        )
        html = _render_list()
        assert ">SOL_USDT<" in html      # Gate 列照常
        assert "解析失败" in html         # OKX 列降级提示

    def test_add_message_carries_okx_resolution(self, _isolated_tokens, monkeypatch):
        """保存成功后的 message 附带 OKX 解析结论（仅提示，不阻断保存）。"""
        from nanobot_quant.token_handlers import token_add

        self._mock_okx(monkeypatch, {"XCRCL": "XCRCL-USDC"})
        resp = _call(
            token_add,
            {"symbol": "CRCLX", "address": SOLANA_ADDR, "chain": "solana",
             "okx_symbol": "XCRCL"},
        )
        assert resp.status_code == 200
        msg = json.loads(resp.body)["message"]
        assert "OKX 解析：XCRCL-USDC" in msg
        assert _read_tokens()[0]["okx_symbol"] == "XCRCL"  # 照样落盘

    def test_add_message_warns_when_unresolved(self, _isolated_tokens, monkeypatch):
        """解析失败：告警进 message，但保存仍然成功（网络/限流不该存不下去）。"""
        from nanobot_quant.token_handlers import token_add

        self._mock_okx(monkeypatch, {})
        resp = _call(
            token_add,
            {"symbol": "CRCLX", "address": SOLANA_ADDR, "chain": "solana"},
        )
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert "❌ OKX 无此标的" in body["message"]
        assert _read_tokens()[0]["symbol"] == "CRCLX"
