"""Gate.io credential spec — registers with credential_registry for WebUI management.

The Gate CEX credential covers the main account (spot trading via signed REST)
plus up to 5 sub-accounts (gate_bot1..5) used for batch slot isolation in the
TD live loop (execution_channel=cex).

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
"""

from .credential_registry import CredentialSpec, FieldSpec, register

_SUB_NAMES = [f"gate_bot{i}" for i in range(1, 6)]


def _normalize_gate_form(data: dict) -> dict:
    """Flattened WebUI form → nested {main, slot_map, sub_accounts}.

    Empty sub-account rows are dropped. slot_map is generated for all five
    slots (1..5 → gate_bot1..5) and persisted so the TD batch layer can
    consume it directly.
    """
    main = {
        k: (data.get(k) or "").strip()
        for k in ("api_key", "api_secret", "uid")
    }
    subs: dict[str, dict] = {}
    for name in _SUB_NAMES:
        uid = (data.get(f"sub_{name}_uid") or "").strip()
        key = (data.get(f"sub_{name}_api_key") or "").strip()
        secret = (data.get(f"sub_{name}_api_secret") or "").strip()
        if uid or key or secret:
            subs[name] = {"uid": uid, "api_key": key, "api_secret": secret}
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
        "Gate CEX 交易凭证（执行通道=cex 时使用）。主账号 Key 用于签名 REST 下单与"
        "主→子划转；gate_bot1-5 子账号 Key 用于 TD 分批（slot 1-5 ↔ gate_bot1-5）。"
    ),
    icon="🏛️",
    fields=[
        FieldSpec("api_key", "主账号 API Key", placeholder="Gate.io → API 管理（需交易权限）"),
        FieldSpec("api_secret", "主账号 API Secret"),
        FieldSpec("uid", "主账号 UID", type="text", placeholder="Gate.io 个人中心显示（如 15119093）"),
    ]
    + [
        FieldSpec(
            f"sub_{name}_uid",
            f"子账号 {name} · UID",
            type="text",
            placeholder=f"子账号 {name} 的 UID（账户管理页查看）",
            required=False,  # 只填 UID 即可保存（余额/划转走主 key，UID 仅用于名字↔余额匹配）
        )
        for name in _SUB_NAMES
    ]
    + [
        FieldSpec(
            f"sub_{name}_api_key",
            f"子账号 {name} · API Key",
            placeholder=f"子账号 {name} 的 Key（spot 交易权限）",
            required=False,  # 仅 P3 TD 分批子账号下单需要；留空不阻塞保存
        )
        for name in _SUB_NAMES
    ]
    + [
        FieldSpec(
            f"sub_{name}_api_secret",
            f"子账号 {name} · API Secret",
            placeholder=f"子账号 {name} 的 Secret",
            required=False,
        )
        for name in _SUB_NAMES
    ],
    docs_url="https://www.gate.io/myaccount/api_keys",
    normalize=_normalize_gate_form,
    denormalize=_denormalize_gate_form,
)

register(GATE_SPEC)
