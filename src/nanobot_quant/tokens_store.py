"""tokens.json — shared loader (WebUI dropdown + execution gate).

tokens.json lives at ``{data_root}/legion/credentials/tokens.json``
(HF: /data, MS: /mnt/workspace) and is maintained through the WebUI
token management page (/config/tokens).  It registers token metadata
(symbol / chain / address / confirmed) for the L2 resolution gate.

This module is the single read path shared by:
- exec_params_handlers (TD 标的 dropdown)
- tools_execute / pipeline (live resolution gate)
- td_live (TD autonomous strategy tokens)
"""

from __future__ import annotations

import json
import os
from typing import Any


def _credentials_paths() -> list[str]:
    return [
        os.path.join(root, "legion", "credentials", "tokens.json")
        for root in ("/data", "/mnt/workspace")
    ]


def load_tokens_json() -> list[dict[str, Any]] | None:
    """Load tokens.json; None when missing/invalid (callers treat as no gate)."""
    for p in _credentials_paths():
        try:
            if not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except (OSError, ValueError):
            continue
    return None


def load_token_symbols() -> list[str]:
    """Sorted unique symbol list from tokens.json (empty when unavailable)."""
    data = load_tokens_json()
    if not data:
        return []
    syms: set[str] = set()
    for entry in data:
        if isinstance(entry, dict):
            sym = str(entry.get("symbol", "")).strip()
            if sym:
                syms.add(sym)
    return sorted(syms)


def _to_float(value, default=0.0) -> float:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def token_meta(
    symbol: str, tokens_json: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """tokens.json 条目 + 衍生元数据（min_hold / cost_price / chain / address）。

    缺失字段返回安全默认：min_hold=0.0（不保留）、cost_price=None（对账时
    用当前价兜底）、chain="solana"、address=""。对账导入与标的池编辑共用。
    """
    if tokens_json is None:
        tokens_json = load_tokens_json()
    raw = str(symbol or "").upper()
    entry: dict[str, Any] = {}
    for e in (tokens_json or []):
        if not isinstance(e, dict):
            continue
        if str(e.get("symbol", "")).upper() == raw:
            entry = e
            break
    return {
        "symbol": raw,
        "address": str(entry.get("address") or ""),
        "chain": str(entry.get("chain") or "solana").lower(),
        "min_hold": _to_float(entry.get("min_hold")),
        "cost_price": _to_float(entry.get("cost_price"), default=None),
        "confirmed": bool(entry.get("confirmed")),
    }


def token_chain(symbol: str, tokens_json: list[dict[str, Any]] | None = None) -> str:
    """Resolve the chain a registered token lives on.

    Returns the tokens.json entry's ``chain`` (default "solana" when the
    entry has none).  This is the single source for per-target chain
    propagation — TD data fetching, pricing and broker swaps all use it so
    a target like SPCXB (bnb) automatically runs on BNB Chain without a
    separate td_chain parameter.
    """
    if tokens_json is None:
        tokens_json = load_tokens_json()
    raw = str(symbol or "").upper()
    for entry in (tokens_json or []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("symbol", "")).upper() == raw:
            return str(entry.get("chain") or "solana").lower()
    return "solana"
def update_token_meta(
    symbol: str,
    min_hold: float | None = None,
    cost_price: float | None = None,
) -> bool:
    """更新 tokens.json 条目的 min_hold / cost_price（标的池行式编辑）。

    min_hold 必须 ≥0（None = 不修改）；cost_price None = 不修改，
    0/负数 = 清除（回退对账时当前价兜底）。返回是否成功。
    """
    path = None
    for p in _credentials_paths():
        try:
            if os.path.isfile(p):
                path = p
                break
        except OSError:
            continue
    if path is None:
        return False
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
        if not isinstance(entries, list):
            return False
    except (OSError, ValueError):
        return False
    raw = str(symbol or "").upper()
    found = False
    for e in entries:
        if not isinstance(e, dict):
            continue
        if str(e.get("symbol", "")).upper() == raw:
            found = True
            if min_hold is not None:
                e["min_hold"] = max(0.0, float(min_hold or 0))
            if cost_price is not None:
                try:
                    v = float(cost_price)
                except (TypeError, ValueError):
                    v = 0.0
                if v > 0:
                    e["cost_price"] = v
                else:
                    e.pop("cost_price", None)
            break
    if not found:
        return False
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False
