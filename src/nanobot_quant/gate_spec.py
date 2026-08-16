"""Gate.io credential spec — registers with credential_registry for WebUI management.

The Gate CEX credential covers the main account (spot trading via gate-api SDK)
plus up to 5 sub-accounts (gate_bot1..5) used for batch slot isolation in the
TD live loop (execution_channel=cex).

The main API key/secret is generated once and kept in gate.json. The WebUI form
edits the main key/secret, all sub-account UIDs, and optional sub-account
api_key/api_secret. Sub-account keys are only needed for P3 batched sub-account
ordering (Gate has no master-for-sub order API); balance lookup and main→sub
transfer run on the main key alone, so sub keys may stay empty until then.

WebUI uses a flat form (main fields + one row per sub-account). On save the
flat dict is normalized to the nested shape consumed by gate_credentials:

    {
      "main": {"uid": "...", "api_key": "...", "api_secret": "..."},
      "slot_map": {"1": "gate_bot1", ..., "5": "gate_bot5"},
      "sub_accounts": {
        "gate_bot1": {"uid": "...", "api_key": "...", "api_secret": "..."},
        ...
      }
    }

The normalizer is incremental: empty fields fall back to the previously stored
values (main and sub-accounts alike). A sub-account row is dropped only when
uid/key/secret are ALL empty.
"""

from .credential_registry import (
    CredentialSpec,
    FieldSpec,
    read_credential,
    register,
)

_SUB_NAMES = [f"gate_bot{i}" for i in range(1, 6)]


def _normalize_gate_form(data: dict) -> dict:
    """Flattened WebUI form → nested {main, slot_map, sub_accounts}.

    Incremental: empty api_key/api_secret (main and sub alike) fall back to
    the previously stored values. A sub-account row is kept when any of
    uid/api_key/api_secret is present, dropped only when all three are empty.
    slot_map is generated for all five slots (1..5 → gate_bot1..5) and
    persisted so the TD batch layer can consume it directly.
    """
    old = read_credential("gate") or {}
    old_main = old.get("main") or {}
    main = {
        "api_key": (data.get("api_key") or "").strip() or old_main.get("api_key", ""),
        "api_secret": (data.get("api_secret") or "").strip() or old_main.get("api_secret", ""),
        "uid": (data.get("uid") or "").strip(),
    }
    old_subs = old.get("sub_accounts") or {}
    subs: dict[str, dict] = {}
    for name in _SUB_NAMES:
        old_s = old_subs.get(name) or {}
        entry = {
            "uid": (data.get(f"sub_{name}_uid") or "").strip() or old_s.get("uid", ""),
            "api_key": (data.get(f"sub_{name}_api_key") or "").strip() or old_s.get("api_key", ""),
            "api_secret": (data.get(f"sub_{name}_api_secret") or "").strip() or old_s.get("api_secret", ""),
        }
        if entry["uid"] or entry["api_key"] or entry["api_secret"]:
            subs[name] = entry
    slot_map = {str(i): name for i, name in enumerate(_SUB_NAMES, start=1)}
    return {"main": main, "slot_map": slot_map, "sub_accounts": subs}


def _denormalize_gate_form(data: dict) -> dict:
    """Nested stored shape → flat form dict (for pre-filling the edit page)."""
    data = data or {}
    main = data.get("main") or {}
    subs = data.get("sub_accounts") or {}
    flat = {
        "api_key": main.get("api_key", ""),
        "api_secret": main.get("api_secret", ""),
        "uid": main.get("uid", ""),
    }
    for name in _SUB_NAMES:
        s = subs.get(name) or {}
        flat[f"sub_{name}_uid"] = s.get("uid", "")
        flat[f"sub_{name}_api_key"] = s.get("api_key", "")
        flat[f"sub_{name}_api_secret"] = s.get("api_secret", "")
    return flat


GATE_SPEC = CredentialSpec(
    name="gate",
    display="Gate.io",
    description=(
        "Gate CEX 交易凭证（执行通道=cex 时使用）。主账号 Key 生成后自动保留；"
        "gate_bot1-5 子账号 UID 用于 slot 1-5 ↔ gate_bot1-5 映射，"
        "子账号 API Key/Secret 仅 P3 分批下单（TD cex 子账号实跑）需要，余额/划转可留空。"
    ),
    icon="🏛️",
    fields=[
        FieldSpec("api_key", "API Key", group="🏛️ 主账号", placeholder="Gate.io → API 管理（需交易权限）", required=False),
        FieldSpec("api_secret", "API Secret", group="🏛️ 主账号", required=False),
        FieldSpec("uid", "UID", type="text", group="🏛️ 主账号", placeholder="Gate.io 个人中心显示（如 15119093）"),
    ]
    # 每个子账号一组（UID / API Key / API Secret 连续排列，按账号分组展示）
    + [
        f
        for name in _SUB_NAMES
        for f in (
            FieldSpec(f"sub_{name}_uid", "UID", type="text", group=f"🤖 {name}",
                      placeholder=f"子账号 {name} 的 UID（账户管理页查看）", required=False),
            FieldSpec(f"sub_{name}_api_key", "API Key", group=f"🤖 {name}",
                      placeholder=f"子账号 {name} 的 API Key（P3 分批下单签名用，可留空）", required=False),
            FieldSpec(f"sub_{name}_api_secret", "API Secret", group=f"🤖 {name}", required=False),
        )
    ],
    docs_url="https://www.gate.io/myaccount/api_keys",
    normalize=_normalize_gate_form,
    denormalize=_denormalize_gate_form,
)

register(GATE_SPEC)
