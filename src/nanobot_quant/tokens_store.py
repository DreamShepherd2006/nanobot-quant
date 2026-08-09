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
