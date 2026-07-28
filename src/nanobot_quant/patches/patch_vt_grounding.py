#!/usr/bin/env python3
"""Patch VT grounding to inject OnchainOS enrichment data into swarm prompts.

Adds chain-level data (real-time price, holder distribution, token risk) to the
grounding block that VT injects into every swarm worker's system prompt.
OnchainOS CLI failures are swallowed gracefully.

Usage:
    python3 -m nanobot_quant.patches.patch_vt_grounding
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def _find_vt_file(relative: str) -> str:
    """Locate a VT source file under site-packages."""
    import importlib as _importlib
    grounding_mod = _importlib.import_module("src.swarm.grounding")
    # grounding_mod.__file__ → .../site-packages/src/swarm/grounding.py
    base = os.path.dirname(os.path.abspath(grounding_mod.__file__))
    return os.path.join(base, relative)


# Enrichment code to append to grounding.py
ENRICHMENT_CODE = '''
# -- OnchainOS enrichment (added by nanobot-quant patch_vt_grounding) --

import logging as _logging
import os as _os

_enrich_log = _logging.getLogger("vt.enrich.onchainos")

try:
    from nanobot_quant.onchainos_cli import (
        extract_symbol as _extract_symbol,
        format_risk_level as _format_risk_level,
        get_advanced_info as _get_advanced_info,
        get_holders as _get_holders,
        get_price as _get_price,
        search_token as _search_token,
    )
    _HAS_SHARED = True
except ImportError:
    _enrich_log.warning("onchainos enrich: nanobot_quant.onchainos_cli not available")
    _HAS_SHARED = False


def _fetch_onchainos_data(user_vars):
    """Enrich swarm run with onchain data from the shared CLI module."""
    if not _HAS_SHARED:
        return {}

    symbol = _extract_symbol(user_vars)
    _enrich_log.info("onchainos enrich: extracted symbol=%s", symbol)
    if not symbol:
        _enrich_log.info("onchainos enrich: no symbol, skipping")
        return {}

    onchainos_avail = _os.path.exists("/usr/local/bin/onchainos")
    _enrich_log.info("onchainos enrich: CLI available=%s", onchainos_avail)
    if not onchainos_avail:
        return {symbol: {"ok": False, "error": "onchainos CLI not found",
                         "address": "", "data": {}}}

    addr = _search_token(symbol)
    if not addr:
        _enrich_log.warning("onchainos enrich: %s -> token not found on chain", symbol)
        return {symbol: {"ok": False, "error": "token not found on chain",
                         "address": "", "data": {}}}

    data = {}
    price_val = _get_price(addr)
    if price_val:
        data["price"] = price_val
        _enrich_log.info("onchainos enrich: %s price=%s", symbol, price_val)

    holders = _get_holders(addr)
    if holders:
        data["holders"] = holders
        _enrich_log.info("onchainos enrich: %s holders=%d", symbol, len(holders))

    risk_raw = _get_advanced_info(addr)
    if risk_raw:
        data["risk"] = _format_risk_level(risk_raw)
        _enrich_log.info("onchainos enrich: %s risk=ok", symbol)

    ok = bool(data)
    if ok:
        _enrich_log.info("onchainos enrich: %s complete", symbol)
    else:
        _enrich_log.warning("onchainos enrich: %s -> no data returned", symbol)

    return {symbol: {"ok": ok, "error": "" if ok else "no data returned",
                     "address": addr, "data": data}}


def _format_onchainos_block(onchainos_data):
    if not onchainos_data:
        _enrich_log.info("onchainos enrich: format skipped (empty data)")
        return ""

    all_failed = all(not v["ok"] for v in onchainos_data.values())
    _enrich_log.info(
        "onchainos enrich: formatting block symbols=%d all_failed=%s",
        len(onchainos_data), all_failed,
    )
    sections = []

    header = (
        "## Onchain Data\\n\\n"
        "**Chain-level data from OnchainOS.** These metrics complement the "
        "OHLCV table above. Prices are real-time snapshots taken at run start."
    )
    sections.append(header)

    for sym, entry in onchainos_data.items():
        if not entry["ok"]:
            err = entry["error"] or "unknown error"
            sections.append(
                f"### {sym}\\n"
                f"\\u26a0\\ufe0f **OnchainOS data unavailable** ({err}). "
                f"Analysis limited to OHLCV only."
            )
            continue

        d = entry["data"]
        lines = [f"### {sym}"]

        price = d.get("price")
        if price is not None:
            try:
                lines.append(f"- **Real-time price:** ${float(price):.2f}")
            except (TypeError, ValueError):
                lines.append(f"- **Real-time price:** {price}")

        holders = d.get("holders")
        if isinstance(holders, list) and holders:
            # Holders response is an array of individual holder entries
            lines.append(f"- **Holder entries tracked:** {len(holders)}")
            top_hold = sum(
                float(h.get("holdPercent", 0)) for h in holders[:10]
                if isinstance(h, dict)
            )
            if top_hold:
                lines.append(f"- **Top-10 hold%:** {top_hold:.1f}%")

        risk = d.get("risk")
        if isinstance(risk, dict):
            levels = {"0": "Unknown", "1": "Low", "2": "Medium", "3": "Med-High", "4": "High"}
            rl = risk.get("risk_level", "?")
            lines.append(f"- **Risk level:** {levels.get(str(rl), rl)}")
            for key, label in [
                ("top10_pct", "Top-10 hold"), ("dev_pct", "Dev hold"),
                ("bundle_pct", "Bundle hold"), ("suspicious_pct", "Suspicious hold"),
            ]:
                val = risk.get(key)
                if val and val != "?":
                    lines.append(f"- **{label}:** {val}%")
            if risk.get("snipers", "?") != "?":
                lines.append(f"- **Snipers:** {risk['snipers']}")
            if risk.get("creator_rugs", "?") != "?":
                lines.append(f"- **Creator rug-pulls:** {risk['creator_rugs']}/{risk.get('creator_tokens', '?')}")

        sections.append("\\n".join(lines))

    if all_failed:
        sections.append(
            "\\n\\u26a0\\ufe0f **OnchainOS data temporarily unavailable for all "
            "symbols.** Analysis based on OHLCV only \\u2014 conclusions may lack "
            "on-chain context."
        )

    block = "\\n\\n".join(sections)
    _enrich_log.info(
        "onchainos enrich: formatted block len=%d preview=%.150r",
        len(block), block,
    )
    return block
'''


def _patch_grounding() -> None:
    grounding_path = _find_vt_file("grounding.py")

    with open(grounding_path, "r") as f:
        content = f.read()

    if "_fetch_onchainos_data" in content:
        logger.info("grounding.py: already patched, skipping")
        return

    content = content.rstrip("\n") + "\n" + ENRICHMENT_CODE

    with open(grounding_path, "w") as f:
        f.write(content)

    logger.info("grounding.py: patched (onchainos enrichment functions)")


def _patch_runtime() -> None:
    runtime_path = _find_vt_file("runtime.py")

    with open(runtime_path, "r") as f:
        content = f.read()

    if "_onchainos_block" in content:
        logger.info("runtime.py: already patched, skipping")
        return

    old = "        grounding_block = grounding.format_grounding_block(run.grounding_data or {})"
    new = (
        "        grounding_block = grounding.format_grounding_block(run.grounding_data or {})\n"
        "\n"
        "        # OnchainOS enrichment: fetch chain-level data and append to prompt\n"
        '        _onchainos_block = ""\n'
        "        try:\n"
        "            _onchainos_data = grounding._fetch_onchainos_data(run.user_vars)\n"
        "            _onchainos_block = grounding._format_onchainos_block(_onchainos_data)\n"
        "        except Exception:\n"
        "            logger.warning(\n"
        '                "OnchainOS enrichment failed for run %s", run_id, exc_info=True\n'
        "            )\n"
        "        if _onchainos_block:\n"
        "            logger.info(\n"
        "                'onchainos enrich: appended %d chars to grounding block for run %s',\n"
        "                len(_onchainos_block), run_id,\n"
        "            )\n"
        '            grounding_block = grounding_block + "\\n\\n" + _onchainos_block\n'
        "        else:\n"
        "            logger.info(\n"
        "                'onchainos enrich: no block generated for run %s', run_id\n"
        "            )"
    )

    content = content.replace(old, new)

    with open(runtime_path, "w") as f:
        f.write(content)

    logger.info("runtime.py: patched (onchainos enrichment call site)")


def apply() -> None:
    _patch_grounding()
    _patch_runtime()


if __name__ == "__main__":
    apply()
    print("\u2705 VT grounding: onchainos enrichment patch applied")
