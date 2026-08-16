"""Gate.io credential spec — registers with credential_registry for WebUI management.

The Gate CEX credential covers the main account (spot trading via signed REST)
plus up to 5 sub-accounts (gate_bot1..5) used for batch slot isolation in the
TD live loop (execution_channel=cex).

The main API key/secret is generated once and kept in gate.json; the WebUI form
only edits UIDs (main + sub-accounts). Sub-accounts have NO own keys — balance
lookup and main→sub transfer run on the main key. Sub keys would only be needed
for P3 batched sub-account ordering (Gate has no master-for-sub order API), and
can be added back to the form when that lands.

WebUI uses a flat form (main fields + one row per sub-account). On save the
flat dict is normalized to the nested shape consumed by gate_credentials:

    {
      "main": {"uid": "...", "api_key": "...", "api_secret": "..."},
      "slot_map": {"1": "gate_bot1", ..., "5": "gate_bot5"},
      "sub_accounts": {
        "gate_bot1": {"uid": "..."},
        ...
      }
    }

Empty sub-account rows are dropped. The normalizer is incremental: when the
form submits empty main key/secret, the previously stored values are kept.
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

    Incremental: main api_key/api_secret fall back to the previously stored
    values when the form submits them empty (the form only edits UIDs after
    initial setup). slot_map is generated for all five slots (1..5 →
    gate_bot1..5) and persisted so the TD batch layer can consume it directly.
    """
    old = read_credential("gate") or {}
    old_main = old.get("main") or {}
    main = {
        "api_key": (data.get("api_key") or "").strip() or old_main.get("api_key", ""),
        "api_secret": (data.get("api_secret") or "").strip() or old_main.get("api_secret", ""),
        "uid": (data.get("uid") or "").strip(),
    }
    subs: dict[str, dict] = {}
    for name in _SUB_NAMES:
        uid = (data.get(f"sub_{name}_uid") or "").strip()
        if uid:
            subs[name] = {"uid": uid}
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
    return flat


GATE_SPEC = CredentialSpec(
    name="gate",
    display="Gate.io",
    description=(
        "Gate CEX 交易凭证（执行通道=cex 时使用）。主账号 Key 生成后自动保留，"
        "表单只需维护 UID；gate_bot1-5 子账号无独立 Key（余额/划转走主 Key），"
        "UID 用于 slot 1-5 ↔ gate_bot1-5 映射。"
    ),
    icon="🏛️",
    fields=[
        FieldSpec("api_key", "主账号 API Key", placeholder="Gate.io → API 管理（需交易权限）", required=False),
        FieldSpec("api_secret", "主账号 API Secret", required=False),
        FieldSpec("uid", "主账号 UID", type="text", placeholder="Gate.io 个人中心显示（如 15119093）"),
    ]
    + [
        FieldSpec(
            f"sub_{name}_uid",
            f"子账号 {name} · UID",
            type="text",
            placeholder=f"子账号 {name} 的 UID（账户管理页查看）",
            required=False,
        )
        for name in _SUB_NAMES
    ],
    docs_url="https://www.gate.io/myaccount/api_keys",
    normalize=_normalize_gate_form,
    denormalize=_denormalize_gate_form,
)

register(GATE_SPEC)
