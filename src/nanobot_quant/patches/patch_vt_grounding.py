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
    grounding_mod = __import__("src.swarm.grounding")
    base = os.path.dirname(os.path.dirname(os.path.abspath(grounding_mod.__file__)))
    return os.path.join(base, relative)


# Enrichment code to append to grounding.py
ENRICHMENT_CODE = '''
# -- OnchainOS enrichment (added by nanobot-quant patch_vt_grounding) --

import json as _json
import subprocess as _subprocess
import os as _os

def _onchainos(*args, timeout=15):
    try:
        r = _subprocess.run(
            ["/usr/local/bin/onchainos", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return None
        return _json.loads(r.stdout) if r.stdout.strip() else None
    except Exception:
        return None


def _token_address(symbol):
    result = _onchainos("token", "search", symbol, "--limit", "1")
    if result:
        items = result.get("items") or result.get("data") or []
        if isinstance(items, list) and items:
            addr = items[0].get("address") or items[0].get("token_address")
            if addr:
                return addr
    return None


def _fetch_onchainos_data(user_vars):
    symbols = extract_symbols(user_vars)
    if not symbols:
        return {}

    onchainos_avail = _os.path.exists("/usr/local/bin/onchainos")
    result = {}

    for sym in symbols:
        base = sym.split(".")[0] if "." in sym else sym
        entry = {"ok": False, "error": "", "address": "", "data": {}}

        if not onchainos_avail:
            entry["error"] = "onchainos CLI not found"
            result[base] = entry
            continue

        addr = _token_address(base)
        if not addr:
            entry["error"] = "token not found on chain"
            result[base] = entry
            continue
        entry["address"] = addr

        data = {}
        price = _onchainos("market", "price", "--address", addr)
        if price:
            data["price"] = price.get("price") or price.get("usdPrice")

        holders = _onchainos("token", "holders", "--address", addr)
        if holders:
            data["holders"] = holders

        risk = _onchainos("token", "risk", "--address", addr)
        if risk:
            data["risk"] = risk

        if data:
            entry["ok"] = True
            entry["data"] = data
        else:
            entry["error"] = "no data returned"
            entry["data"] = {}

        result[base] = entry

    return result


def _format_onchainos_block(onchainos_data):
    if not onchainos_data:
        return ""

    all_failed = all(not v["ok"] for v in onchainos_data.values())
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
        if isinstance(holders, dict):
            total = (holders.get("total") or holders.get("holderCount")
                     or holders.get("holders"))
            if total is not None:
                lines.append(f"- **Holder addresses:** {total}")
            top10 = holders.get("top10Ratio") or holders.get("top10Percent")
            if top10 is not None:
                try:
                    lines.append(f"- **Top-10 concentration:** {float(top10) * 100:.1f}%")
                except (TypeError, ValueError):
                    lines.append(f"- **Top-10 concentration:** {top10}")

        risk = d.get("risk")
        if isinstance(risk, dict):
            level = (risk.get("level") or risk.get("riskLevel")
                     or risk.get("result"))
            if level is not None:
                lines.append(f"- **Safety scan:** {level}")
            flags = risk.get("flags") or risk.get("warnings") or []
            if isinstance(flags, list) and flags:
                lines.append(f"- **Risk flags:** {', '.join(str(f) for f in flags)}")

        sections.append("\\n".join(lines))

    if all_failed:
        sections.append(
            "\\n\\u26a0\\ufe0f **OnchainOS data temporarily unavailable for all "
            "symbols.** Analysis based on OHLCV only \\u2014 conclusions may lack "
            "on-chain context."
        )

    return "\\n\\n".join(sections)
'''


def _patch_grounding() -> None:
    grounding_path = _find_vt_file("swarm/grounding.py")

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
    runtime_path = _find_vt_file("swarm/runtime.py")

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
        '            grounding_block = grounding_block + "\\n\\n" + _onchainos_block'
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
