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

import json as _json
import logging as _logging
import os as _os
import subprocess as _subprocess

_enrich_log = _logging.getLogger("vt.enrich.onchainos")

def _onchainos(*args, timeout=15):
    try:
        r = _subprocess.run(
            ["/usr/local/bin/onchainos", "--format", "json", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return None
        return _json.loads(r.stdout) if r.stdout.strip() else None
    except Exception:
        return None


def _token_address(symbol):
    result = _onchainos("token", "search", "--query", symbol, "--limit", "1")
    if result:
        items = result if isinstance(result, list) else result.get("items") or result.get("data") or []
        if isinstance(items, list) and items:
            addr = items[0].get("tokenContractAddress") or items[0].get("address")
            if addr:
                return addr
    return None


def _extract_symbols(user_vars):
    """Extract bare token names from user_vars dict, stripping trading pair suffixes."""
    symbols = []
    target = user_vars.get("target", "").strip().upper()
    if not target:
        return symbols
    # Strip trading pair suffixes: BTC-USDT → BTC, ETH-USD → ETH
    for suffix in ("-USDT", "-USD", "-USDC"):
        if target.endswith(suffix):
            target = target[:-len(suffix)]
            break
    # Strip stock suffix: SPCX.US → SPCX
    base = target.split(".")[0]
    if base:
        symbols.append(base)
    return symbols


def _fetch_onchainos_data(user_vars):
    symbols = _extract_symbols(user_vars)
    _enrich_log.info("onchainos enrich: extracted symbols=%s", symbols)
    if not symbols:
        _enrich_log.info("onchainos enrich: no symbols, skipping")
        return {}

    onchainos_avail = _os.path.exists("/usr/local/bin/onchainos")
    _enrich_log.info("onchainos enrich: CLI available=%s", onchainos_avail)
    result = {}

    for sym in symbols:
        base = sym.split(".")[0] if "." in sym else sym
        entry = {"ok": False, "error": "", "address": "", "data": {}}

        if not onchainos_avail:
            entry["error"] = "onchainos CLI not found"
            _enrich_log.warning("onchainos enrich: %s -> %s", base, entry["error"])
            result[base] = entry
            continue

        addr = _token_address(base)
        if not addr:
            entry["error"] = "token not found on chain"
            _enrich_log.warning("onchainos enrich: %s -> %s", base, entry["error"])
            result[base] = entry
            continue
        entry["address"] = addr

        data = {}
        price = _onchainos("market", "price", "--address", addr)
        if price:
            data["price"] = price.get("price", "?")

        holders = _onchainos("token", "holders", "--address", addr)
        if holders:
            data["holders"] = holders

        risk = _onchainos("token", "advanced-info", "--address", addr)
        if risk:
            data["risk"] = {
                "risk_level": risk.get("riskControlLevel", "?"),
                "top10_pct": risk.get("top10HoldPercent", "?"),
                "dev_pct": risk.get("devHoldingPercent", "?"),
                "bundle_pct": risk.get("bundleHoldingPercent", "?"),
                "suspicious_pct": risk.get("suspiciousHoldingPercent", "?"),
                "snipers": risk.get("snipersTotal", "?"),
                "creator_rugs": risk.get("devRugPullTokenCount", "?"),
                "creator_tokens": risk.get("devCreateTokenCount", "?"),
            }

        if data:
            entry["ok"] = True
            entry["data"] = data
            _enrich_log.info(
                "onchainos enrich: %s ok price=%s holders=%s risk=%s",
                base,
                data.get("price", "?"),
                "yes" if data.get("holders") else "no",
                "yes" if data.get("risk") else "no",
            )
        else:
            entry["error"] = "no data returned"
            entry["data"] = {}
            _enrich_log.warning("onchainos enrich: %s -> %s", base, entry["error"])

        result[base] = entry

    return result


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
