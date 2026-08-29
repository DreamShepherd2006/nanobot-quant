"""Token address management handlers — WebUI for tokens.json entries.

Registered by gatekeeper as ``/config/tokens`` (business management chat).
Implements the A+C confirmation scheme: *entry does not imply trust*.

New entries are saved with ``confirmed=false``.  ``resolve_token`` (the
single source used by every execution gate) passes an entry whose local
validation is clean, and only blocks an entry with a questionable address
(wrong chain / malformed) with ``needs_confirmation`` until the user
confirms it here.  Editing the address automatically resets confirmation,
so a stale row can never pass the gate on the new address.

Built-in L1 whitelist (SOL/USDC/USDT) is not managed here.
"""

from __future__ import annotations

import json
import os
from html import escape as html_escape

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .onchainos_cli import (
    _BUILTIN_TOKENS,
    _validate_token_entry,
    confirm_token,
    normalize_symbol,
    token_json_path,
)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_template(name: str) -> str:
    with open(os.path.join(_HERE, name), encoding="utf-8") as f:
        return f.read()


_PAGE_HTML = _load_template("token_page.html")

# Chains supported by the on-chain execution layer (keep in sync with the
# resolve_token chain parameter; free-form chains fall back to validation
# defaults).
_CHAINS = (
    "solana", "xlayer", "ethereum",
    # 2026-08-26：补齐 L1 内建白名单主链（AVAX/ARB/OP/POL）
    "bnb", "avalanche", "arbitrum", "optimism", "polygon",
    # 2026-08-29：Tron 链（TRX 原生币；onchainos resolve_chain("tron") → 195）
    "tron",
    # 2026-08-29：XRP Ledger（XRPL）——仅 CEX 登记用（Gate XRP_USDT）；
    # onchainos 不支持 XRPL，DEX 执行会 fail-closed 明确报错，不会误下单
    "xrp",
)


# ── tokens.json storage (shared with the MCP pipeline) ─────────────


def _read_tokens() -> list[dict]:
    p = token_json_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_tokens(entries: list[dict]) -> None:
    """Atomically persist tokens.json (tmp + replace) so concurrent MCP
    subprocesses never observe a partially-written file."""
    p = token_json_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def _token_status(entry: dict) -> dict:
    """Classify an entry for display: clean / confirmed / needs-confirmation."""
    chain = str(entry.get("chain") or "solana")
    check = _validate_token_entry(entry, chain=chain)
    if check["ok"]:
        return {"cls": "status-clean", "label": "✅ 校验通过", "issue": ""}
    if entry.get("confirmed"):
        return {"cls": "status-confirmed", "label": "🔓 已确认", "issue": check["issue"]}
    return {"cls": "status-warn", "label": "⚠️ 待确认", "issue": check["issue"]}


# ── page rendering ─────────────────────────────────────────────────


def _render_list() -> str:
    rows = []
    for entry in _read_tokens():
        sym = str(entry.get("symbol", ""))
        addr = str(entry.get("address", ""))
        chain = str(entry.get("chain") or "solana")
        st = _token_status(entry)
        issue = f"<div class='issue'>{html_escape(st['issue'])}</div>" if st["issue"] else ""
        # Gate CEX pair for this entry (mirrors gate_pair(): gate_symbol wins, else symbol)
        gs = str(entry.get("gate_symbol") or entry.get("symbol") or sym).upper().strip()
        base = gs.replace("-", "").replace("_", "")
        if base.endswith("USDT"):
            base = base[:-4]
        pair = f"{base}_USDT"
        # OKX CEX ticker (research source): okx_symbol wins, else symbol; OKX pair uses '-'
        osym = str(entry.get("okx_symbol") or entry.get("symbol") or sym).upper().strip()
        opair = f"{osym.replace('-', '').replace('_', '')}-USDT"
        rows.append(
            "<tr>"
            f"<td class='sym'>{html_escape(sym)}</td>"
            f"<td class='addr mono'>{html_escape(addr)}</td>"
            f"<td>{html_escape(chain)}</td>"
            f"<td class='mono'>{html_escape(pair)}</td>"
            f"<td class='mono muted'>{html_escape(opair)}</td>"
            f"<td><span class='status {st['cls']}'>{st['label']}</span>{issue}</td>"
            "<td class='actions'>"
            f"<button class='btn-outline' data-act='confirm' data-symbol='{html_escape(sym)}' "
            f"data-address='{html_escape(addr)}' title='标记为已确认（仅当你核实过该地址后）'>确认</button>"
            f"<button class='btn-outline' data-act='edit' data-symbol='{html_escape(sym)}' "
            f"data-address='{html_escape(addr)}' data-chain='{html_escape(chain)}' "
            f"data-gate='{html_escape(entry.get('gate_symbol') or '')}' "
            f"data-okx='{html_escape(entry.get('okx_symbol') or '')}'>编辑</button>"
            f"<button class='btn-danger' data-act='delete' data-symbol='{html_escape(sym)}'>删除</button>"
            "</td>"
            "</tr>"
        )

    table = (
        "<table><thead><tr><th>Symbol</th><th>地址</th><th>链</th><th>Gate 交易对</th><th>OKX 交易对</th><th>状态</th><th>操作</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=7 class=empty>暂无自定义代币条目。'
        'L1 内建白名单（SOL / USDC / USDT）自动可用，无需录入。</td></tr>'}</tbody></table>"
    )

    builtin = "、".join(_BUILTIN_TOKENS) + "（ETH/BTC/BNB 自动填充 WETH/WBTC/WBNB 地址）"
    return (
        _PAGE_HTML
        .replace("{table_html}", table)
        .replace("{builtin_html}", html_escape(str(builtin)))
        .replace("{count_html}", str(len(_read_tokens())))
    )


async def token_list(request: Request) -> HTMLResponse:
    """GET /config/tokens — token address management page."""
    return HTMLResponse(_render_list())


# ── JSON handlers ──────────────────────────────────────────────────


async def _body(request: Request) -> dict | None:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def token_add(request: Request) -> JSONResponse:
    """POST /config/tokens/add — add a tokens.json entry (confirmed=false)."""
    data = await _body(request)
    if not data:
        return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)

    symbol = normalize_symbol(data.get("symbol", ""))
    address = str(data.get("address", "")).strip()
    chain = str(data.get("chain") or "solana").strip().lower()

    if not symbol:
        return JSONResponse({"ok": False, "error": "symbol 不能为空"}, status_code=400)

    # Native-coin handling: SOL can be registered as a TD target by symbol
    # only (address auto-filled from the builtin whitelist, trusted).
    # USDC/USDT are stablecoins with no analysis value — keep them out of
    # the TD target management table.
    if symbol in _BUILTIN_TOKENS:
        if symbol in ("USDC", "USDT"):
            return JSONResponse(
                {"ok": False,
                 "error": f"{symbol} 是稳定币，无分析价值，不需要登记为 TD 标的"},
                status_code=400,
            )
        _b = _BUILTIN_TOKENS[symbol]
        if chain != _b["chain"]:
            return JSONResponse(
                {"ok": False,
                 "error": f"{symbol} 内置地址在 {_b['chain']} 链，不能登记到 {chain} 链"},
                status_code=400,
            )
        address = _b["address"]
        if not address:
            return JSONResponse(
                {"ok": False, "error": "address 不能为空"}, status_code=400
            )
    elif not address:
        return JSONResponse({"ok": False, "error": "address 不能为空"}, status_code=400)

    entries = _read_tokens()
    if any(str(e.get("symbol", "")).upper() == symbol for e in entries):
        return JSONResponse({"ok": False, "error": f"{symbol} 已存在，请直接编辑"}, status_code=400)

    entry = {"symbol": symbol, "address": address, "chain": chain,
             "confirmed": symbol in _BUILTIN_TOKENS}
    _apply_meta_fields(entry, data)  # min_hold / cost_price（标的池编辑）
    for k in ("okx_symbol", "gate_symbol"):
        v = str(data.get(k) or "").upper().strip()
        if v:
            entry[k] = v
    check = _validate_token_entry(entry, chain=chain)
    entries.append(entry)
    _write_tokens(entries)

    if check["ok"]:
        return JSONResponse({"ok": True, "status": "clean",
                             "message": f"{symbol} 已添加，校验通过，执行时直接放行"})
    return JSONResponse({"ok": True, "status": "needs_confirmation",
                         "issue": check["issue"],
                         "message": f"{symbol} 已添加，但地址有疑问（{check['issue']}）——"
                                    "执行将被拦截，需在页面点击「确认」或带 confirm=true 重试"})


async def token_confirm(request: Request) -> JSONResponse:
    """POST /config/tokens/confirm — mark an entry as user-confirmed."""
    data = await _body(request)
    if not data:
        return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)
    symbol = normalize_symbol(data.get("symbol", ""))
    address = str(data.get("address", "")).strip()
    if not symbol:
        return JSONResponse({"ok": False, "error": "symbol 不能为空"}, status_code=400)

    result = confirm_token(symbol, address=address or None)
    if not result.get("ok"):
        return JSONResponse({"ok": False, "error": result.get("error", "确认失败")}, status_code=400)
    return JSONResponse({"ok": True, "message": f"{symbol} 已标记为确认，后续执行直接放行"})


async def token_edit(request: Request) -> JSONResponse:
    """POST /config/tokens/edit — edit address/chain; confirmation resets."""
    data = await _body(request)
    if not data:
        return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)

    symbol = normalize_symbol(data.get("symbol", ""))
    address = str(data.get("address", "")).strip()
    chain = str(data.get("chain") or "solana").strip().lower()

    if not symbol:
        return JSONResponse({"ok": False, "error": "symbol 不能为空"}, status_code=400)
    if not address:
        return JSONResponse({"ok": False, "error": "address 不能为空"}, status_code=400)

    entries = _read_tokens()
    for entry in entries:
        if str(entry.get("symbol", "")).upper() == symbol:
            addr_changed = entry.get("address", "") != address
            entry["address"] = address
            entry["chain"] = chain
            if addr_changed:
                entry["confirmed"] = False  # new address ⇒ re-confirm required
            _apply_meta_fields(entry, data)  # min_hold / cost_price
            # 交易对映射：传值=设置（大写归一化）；传空=清除映射（回退 symbol）
            for k in ("gate_symbol", "okx_symbol"):
                if k in data:
                    v = str(data.get(k) or "").upper().strip()
                    if v:
                        entry[k] = v
                    else:
                        entry.pop(k, None)
            _write_tokens(entries)
            check = _validate_token_entry(entry, chain=chain)
            if check["ok"]:
                return JSONResponse({"ok": True, "status": "clean",
                                     "message": f"{symbol} 已更新，校验通过"})
            return JSONResponse({"ok": True, "status": "needs_confirmation",
                                 "issue": check["issue"],
                                 "message": f"{symbol} 已更新，但地址有疑问（{check['issue']}）——"
                                            "执行将被拦截，需确认后才放行"})
    return JSONResponse({"ok": False, "error": f"{symbol} 不存在"}, status_code=404)


async def token_meta(request: Request) -> JSONResponse:
    """POST /config/tokens/meta — edit OKX CEX / Gate 交易对映射（可选，空=自动映射）。"""
    data = await _body(request)
    if not data:
        return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)
    symbol = normalize_symbol(data.get("symbol", ""))
    if not symbol:
        return JSONResponse({"ok": False, "error": "symbol 不能为空"}, status_code=400)

    entries = _read_tokens()
    entry = next((e for e in entries if str(e.get("symbol", "")).upper() == symbol), None)
    if entry is None:
        return JSONResponse({"ok": False, "error": f"{symbol} 不存在"}, status_code=404)

    for k in ("okx_symbol", "gate_symbol"):
        v = str(data.get(k) or "").upper().strip()
        if v:
            entry[k] = v
        else:
            entry.pop(k, None)  # 留空 = 恢复自动映射
    _write_tokens(entries)
    return JSONResponse({"ok": True,
                         "message": f"{symbol} 映射已更新（OKX: {entry.get('okx_symbol') or '自动'} / Gate: {entry.get('gate_symbol') or '自动'}）"})


async def token_delete(request: Request) -> JSONResponse:
    """POST /config/tokens/delete — remove a tokens.json entry."""
    data = await _body(request)
    if not data:
        return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)
    symbol = normalize_symbol(data.get("symbol", ""))
    if not symbol:
        return JSONResponse({"ok": False, "error": "symbol 不能为空"}, status_code=400)

    entries = _read_tokens()
    kept = [e for e in entries if str(e.get("symbol", "")).upper() != symbol]
    if len(kept) == len(entries):
        return JSONResponse({"ok": False, "error": f"{symbol} 不存在"}, status_code=404)
    _write_tokens(kept)
    return JSONResponse({"ok": True, "message": f"{symbol} 已删除"})


# ── Route registration helper ─────────────────────────────────────


def _apply_meta_fields(entry: dict, data: dict) -> None:
    """把标的池编辑的 min_hold / cost_price 写入 tokens.json 条目。

    两个字段都可选：min_hold 默认 0.0（不保留）；cost_price 空/0/非法
    时删除旧值（回退对账时当前价兜底）。"""
    raw_hold = str(data.get("min_hold") or "").strip()
    if raw_hold:
        try:
            entry["min_hold"] = max(0.0, float(raw_hold))
        except ValueError:
            entry["min_hold"] = 0.0
    else:
        entry["min_hold"] = 0.0
    raw_cost = str(data.get("cost_price") or "").strip()
    if raw_cost:
        try:
            v = float(raw_cost)
            entry["cost_price"] = v if v > 0 else None
        except ValueError:
            entry.pop("cost_price", None)
    else:
        entry.pop("cost_price", None)


def register_token_routes(app, gatekeeper) -> None:
    """Register all token management routes on the FastAPI app.

    Called by gatekeeper_routes.py during app creation.  Storage is lazily
    created on first write (directory created by ``_write_tokens``).
    """
    app.get("/config/tokens")(token_list)
    app.post("/config/tokens/add")(token_add)
    app.post("/config/tokens/confirm")(token_confirm)
    app.post("/config/tokens/edit")(token_edit)
    app.post("/config/tokens/meta")(token_meta)
    app.post("/config/tokens/delete")(token_delete)
