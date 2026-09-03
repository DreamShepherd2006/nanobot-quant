"""OKX CEX credential spec — registers with credential_registry for WebUI management.

NOTE: this is the OKX **exchange (CEX)** credential — an OKX API key belongs to
exactly one account (master or a sub-account). Options trading runs inside
sub-accounts (each sub-account has its own key), so the credential stores a
dynamic list of sub-account entries, one per row::

    {
      "max_sub_accounts": 10,
      "sub_accounts": [
        {"name": "DreamShepherdbot1", "uid": "881574754615066858",
         "api_key": "...", "secret_key": "...", "passphrase": "..."},
        ...
      ]
    }

This is completely independent from ``okx`` (OKX OnchainOS / Web3 market-data
credential). Legacy single flat entry (top-level api_key/secret_key/passphrase)
is migrated to the first sub-account row on read/save.

WebUI uses a flat form (one row per sub-account, named
``sub_{i}_name/uid/api_key/secret_key/passphrase`` + a sub-account limit). On
save the flat form is normalized to the nested shape above. The normalizer is
incremental: empty api_key/secret_key/passphrase fall back to the previously
stored values (secret-style fields never round-trip through the HTML form).
A row is dropped only when name/uid/api_key/secret_key/passphrase are ALL empty.
"""

from .credential_registry import (
    CredentialSpec,
    FieldSpec,
    read_credential,
    register,
)

_DEFAULT_MAX_SUBS = 10
_MAX_SUBS_HARD = 50
#: Flat-form storage key for the sub-account limit.
_MAX_KEY = "max_sub_accounts"


def _strip(v) -> str:
    return (v or "").strip() if isinstance(v, str) else (str(v or "").strip())


def _parse_max_subs(v):
    try:
        n = int(_strip(v) or 0)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= _MAX_SUBS_HARD else None


def _find_entry(subs: list, uid: str, name: str):
    """Locate a stored sub-account entry by uid first, then by name."""
    for s in subs:
        if uid and s.get("uid") == uid:
            return s
    for s in subs:
        if name and s.get("name") == name:
            return s
    return None


# ── Stored-shape helpers (shared with okx_cex_credentials) ──────


def normalize_stored(raw: dict) -> dict:
    """Canonicalize a stored okx_cex credential dict (flat → sub-account list).

    Legacy flat shape (top-level api_key/secret_key/passphrase, no
    ``sub_accounts``) is migrated to a single unnamed sub-account row.
    """
    raw = raw or {}
    subs = raw.get("sub_accounts")
    if isinstance(subs, dict):  # tolerate accidental mapping shape
        subs = list(subs.values())
    if not isinstance(subs, list):
        subs = []
    if not subs and (raw.get("api_key") or raw.get("secret_key") or raw.get("passphrase")):
        subs = [
            {
                "name": "",
                "uid": "",
                "api_key": raw.get("api_key", ""),
                "secret_key": raw.get("secret_key", ""),
                "passphrase": raw.get("passphrase", ""),
            }
        ]
    return {
        "max_sub_accounts": (
            _parse_max_subs(raw.get(_MAX_KEY)) or _DEFAULT_MAX_SUBS
        ),
        "sub_accounts": subs,
    }


def _default_name(index: int) -> str:
    return f"okx_sub{index + 1}"


# ── Normalizer / denormalizer (WebUI flat form ↔ stored shape) ───

def _normalize_okx_cex_form(data: dict) -> dict:
    """Flat WebUI form → nested {max_sub_accounts, sub_accounts:[...]}.

    Incremental: empty api_key/secret_key/passphrase fall back to previously
    stored values (matched by uid, then name). A row is dropped only when all
    five fields are empty. New rows keep their entered name, or fall back to a
    stored name when the name field was cleared, or ``okx_subN`` when brand new.
    """
    old = normalize_stored(read_credential("okx_cex"))
    old_subs = old.get("sub_accounts") or []
    max_subs = (
        _parse_max_subs(data.get(_MAX_KEY))
        or old.get("max_sub_accounts")
        or _DEFAULT_MAX_SUBS
    )

    rows: list[dict] = []
    for i in range(max_subs):
        name = _strip(data.get(f"sub_{i}_name"))
        uid = _strip(data.get(f"sub_{i}_uid"))
        api_key = _strip(data.get(f"sub_{i}_api_key"))
        secret_key = _strip(data.get(f"sub_{i}_secret_key"))
        passphrase = _strip(data.get(f"sub_{i}_passphrase"))
        if not (name or uid or api_key or secret_key or passphrase):
            continue
        old_entry = _find_entry(old_subs, uid, name)
        entry = {
            "name": name or (old_entry or {}).get("name", "") or _default_name(len(rows)),
            "uid": uid or (old_entry or {}).get("uid", ""),
            "api_key": api_key or (old_entry or {}).get("api_key", ""),
            "secret_key": secret_key or (old_entry or {}).get("secret_key", ""),
            "passphrase": passphrase or (old_entry or {}).get("passphrase", ""),
        }
        rows.append(entry)
    return {"max_sub_accounts": max_subs, "sub_accounts": rows}


def _denormalize_okx_cex_form(data: dict) -> dict:
    """Nested stored shape → flat form dict (pre-fill for the edit page)."""
    stored = normalize_stored(data)
    subs = stored.get("sub_accounts") or []
    flat = {_MAX_KEY: str(stored.get("max_sub_accounts") or _DEFAULT_MAX_SUBS)}
    for i, s in enumerate(subs):
        flat[f"sub_{i}_name"] = s.get("name", "")
        flat[f"sub_{i}_uid"] = s.get("uid", "")
        # secret-style fields intentionally NOT emitted (never round-trip).
        flat[f"sub_{i}_api_key"] = ""
        flat[f"sub_{i}_secret_key"] = ""
        flat[f"sub_{i}_passphrase"] = ""
    return flat


# ── Dynamic field builder & form toolbar ─────────────────────────

def _fields_for(current: dict) -> list:
    """Render existing sub-account rows as editable groups."""
    max_subs = _parse_max_subs(current.get(_MAX_KEY)) or _DEFAULT_MAX_SUBS
    fields: list = []
    for i in range(max_subs):
        name = _strip(current.get(f"sub_{i}_name"))
        uid = _strip(current.get(f"sub_{i}_uid"))
        if not (name or uid):
            break  # denormalize emits consecutive rows; stop at first empty one
        group = f"🤖 子账号 {i + 1}"
        fields += [
            FieldSpec(f"sub_{i}_name", "别名", type="text", group=group,
                      placeholder="OKX 子账户名（可留空）"),
            FieldSpec(f"sub_{i}_uid", "UID", type="text", group=group,
                      placeholder="子账户 UID（18 位数字）"),
            FieldSpec(f"sub_{i}_api_key", "API Key", group=group,
                      placeholder="该子账户的 API Key，留空=保留已存"),
            FieldSpec(f"sub_{i}_secret_key", "Secret Key", group=group),
            FieldSpec(f"sub_{i}_passphrase", "Passphrase", group=group),
        ]
    return fields


def _form_extra(current: dict) -> str:
    """「➕ 添加子账号」button — inserts an empty dynamic row."""
    max_subs = _parse_max_subs(current.get(_MAX_KEY)) or _DEFAULT_MAX_SUBS
    configured = 0
    for i in range(max_subs):
        if current.get(f"sub_{i}_name") or current.get(f"sub_{i}_uid"):
            configured += 1
        else:
            break
    return f"""
<div id="sub-rows"></div>
<div class="flex-row" style="margin:14px 0 4px;">
  <button type="button" class="btn-outline" onclick="addSubRow()">➕ 添加子账号</button>
</div>
<div id="sync-msg" style="margin:8px 0;font-size:.88em;"></div>
<script>
let subRowIdx = {configured};
const subRowMax = {max_subs};
function subRowHTML(name, uid) {{
  const i = subRowIdx++;
  return '<div class="cred-group" data-sub="' + i + '">' +
    '<div class="cred-group-title">🤖 子账号 ' + (i + 1) + '</div>' +
    '<div class="cred-group-body">' +
      '<div class="form-group"><label>别名</label><input type="text" name="sub_' + i + '_name" value="' + (name || '') + '" placeholder="OKX 子账户名（可留空）"></div>' +
      '<div class="form-group"><label>UID</label><input type="text" name="sub_' + i + '_uid" value="' + (uid || '') + '" placeholder="子账户 UID（18 位数字）"></div>' +
      '<div class="form-group"><label>API Key</label><input type="password" name="sub_' + i + '_api_key" value="" placeholder="该子账户的 API Key，留空=保留已存"></div>' +
      '<div class="form-group"><label>Secret Key</label><input type="password" name="sub_' + i + '_secret_key" value=""></div>' +
      '<div class="form-group"><label>Passphrase</label><input type="password" name="sub_' + i + '_passphrase" value=""></div>' +
      '<div style="text-align:right"><button type="button" class="btn-danger" style="padding:4px 12px;font-size:.82em" data-remove>✖ 删除该行</button></div>' +
    '</div></div>';
}}
function addSubRow(name, uid) {{
  if (subRowIdx >= subRowMax) {{
    document.getElementById('sync-msg').innerHTML = '<div class="err">❌ 子账号数量已达上限 ' + subRowMax + '（可修改「子账号上限」）</div>';
    return;
  }}
  document.getElementById('sub-rows').insertAdjacentHTML('beforeend', subRowHTML(name, uid));
}}
// 事件委托：删除动态子账号行（避免内联 onclick 转义问题）
document.addEventListener('click', function(ev) {{
  const btn = ev.target.closest('button[data-remove]');
  if (btn) btn.closest('.cred-group').remove();
}});
</script>
"""


OKX_CEX_SPEC = CredentialSpec(
    name="okx_cex",
    display="OKX CEX（期权/合约）",
    description=(
        "OKX 交易所子账户凭证（期权卖 put/平仓等 CEX 交易用）。"
        "OKX 的 API Key 绑定单个账户——每个子账户一把 Key，可在此维护多个子账户。"
        "与 okx（OnchainOS）凭证相互独立，请勿混填。"
    ),
    icon="🔑",
    fields=[
        FieldSpec("max_sub_accounts", "子账号上限", type="text",
                  placeholder="默认 10"),
    ],
    docs_url="https://www.okx.com/account/my-api",
    normalize=_normalize_okx_cex_form,
    denormalize=_denormalize_okx_cex_form,
    fields_for=_fields_for,
    form_extra=_form_extra,
)

register(OKX_CEX_SPEC)
