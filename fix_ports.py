"""Fix quant agent port conflict on startup.

Problem: quant gateway_port=18791 clashes with neo ws_port=18791.
Fix: shift quant to gw=18792, ws=18793.
"""
import json, os, sys

SQUAD_CFG = "/data/legion/squad_config.json"
QUANT_CFG = "/data/legion/instances/quant/config.json"

# ── 1. Fix squad_config.json ──
if os.path.exists(SQUAD_CFG):
    with open(SQUAD_CFG) as f:
        cfg = json.load(f)
    peers = cfg.get("peers", {})
    if "quant" in peers:
        changed = False
        if peers["quant"].get("gateway_port") != 18792:
            peers["quant"]["gateway_port"] = 18792
            changed = True
        if peers["quant"].get("ws_port") != 18793:
            peers["quant"]["ws_port"] = 18793
            changed = True
        if changed:
            with open(SQUAD_CFG, "w") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            print(f"[fix_ports] ✅ squad_config: quant → gw=18792 ws=18793", file=sys.stderr)
        else:
            print(f"[fix_ports] ⏭️ squad_config already correct", file=sys.stderr)

    # Also check no overlap with neo
    for name, p in peers.items():
        if name == "quant":
            continue
        gw = p.get("gateway_port", 0)
        ws = p.get("ws_port", 0)
        if 18792 in (gw, ws) or 18793 in (gw, ws):
            print(f"[fix_ports] ⚠️ WARNING: quant ports clash with {name} (gw={gw} ws={ws})",
                  file=sys.stderr)

# ── 2. Fix quant config.json ──
if os.path.exists(QUANT_CFG):
    with open(QUANT_CFG) as f:
        qcfg = json.load(f)
    if qcfg.get("gateway", {}).get("port") != 18792:
        qcfg.setdefault("gateway", {})["port"] = 18792
        with open(QUANT_CFG, "w") as f:
            json.dump(qcfg, f, indent=2, ensure_ascii=False)
        print(f"[fix_ports] ✅ quant config: gateway.port=18792", file=sys.stderr)
    else:
        print(f"[fix_ports] ⏭️ quant config already correct", file=sys.stderr)
else:
    print(f"[fix_ports] ⚠️ quant config not found at {QUANT_CFG}", file=sys.stderr)

print("[fix_ports] done", file=sys.stderr)
