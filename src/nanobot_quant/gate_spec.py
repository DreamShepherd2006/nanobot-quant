"""Gate.io credential spec — registers with credential_registry for WebUI management.

The Gate CEX credential covers the main account (spot trading via gate-api SDK)
plus a dynamic list of sub-accounts (default name ``gate_botN``) used for batch
slot isolation in the TD live loop (execution_channel=cex).

The main API key/secret is generated once and kept in gate.json. The WebUI form
edits the main key/secret, all sub-account rows (name/UID/key/secret), and the
sub-account limit. Sub-account keys are only needed for P3 batched sub-account
ordering (Gate has no master-for-sub order API); balance lookup and main→sub
transfer run on the main key alone, so sub keys may stay empty until then.

WebUI uses a flat form (main fields + one row per sub-account, named
``sub_{i}_name/uid/api_key/api_secret``). On save the flat dict is normalized to
the nested shape consumed by gate_credentials::

    {
      "main": {"uid": "...", "api_key": "...", "api_secret": "..."},
      "max_sub_accounts": 10,
      "slot_map": {"1": "gate_bot1", ...},
      "sub_accounts": {
        "gate_bot1": {"uid": "...", "api_key": "...", "api_secret": "..."},
        ...
      }
    }

The normalizer is incremental: empty fields fall back to the previously stored
values (main and sub-accounts alike). A sub-account row is dropped only when
name/uid/key/secret are ALL empty. Renaming a row (same UID, different name)
migrates the sub-account key, slot_map references, and exec_params scene pools
(automatic replacement — user decision 2026-08-22).
"""

import json
import re

from .credential_registry import (
    CredentialSpec,
    FieldSpec,
    read_credential,
    register,
)

_DEFAULT_MAX_SUBS = 10
_MAX_SUBS_HARD = 50


def _strip(v) -> str:
    return (v or "").strip() if isinstance(v, str) else (str(v or "").strip())


def _parse_max_subs(v):
    try:
        n = int(_strip(v) or 0)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= _MAX_SUBS_HARD else None


def _find_by_uid(subs: dict, uid: str):
    for s in subs.values():
        if s.get("uid") == uid:
            return s
    return None


def next_free_num(used_nums: set[int]) -> int:
    """Smallest positive integer not in ``used_nums`` (for default gate_botN names)."""
    n = 1
    while n in used_nums:
        n += 1
    return n


def _used_nums(subs: dict) -> set[int]:
    nums: set[int] = set()
    for name in subs:
        m = re.search(r"(\d+)$", name)
        if m:
            nums.add(int(m.group(1)))
    return nums


def _migrate_scene_pools(renames: dict[str, str]) -> None:
    """Replace renamed sub-accounts inside exec_params scene pools (① auto-migrate).

    Runs on credential save when a row was renamed; keeps TD scene pools valid
    instead of leaving stale names that would fail-closed on the next save.
    """
    try:
        from . import exec_params as _ep

        path = _ep.exec_params_path()
        if not path.exists():
            return
        params = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — migration is best-effort
        return
    scenes = params.get("scenes")
    if not isinstance(scenes, dict):
        return
    changed = False
    for scene in scenes.values():
        pool = scene.get("sub_accounts")
        if isinstance(pool, list):
            new_pool = [renames.get(n, n) for n in pool]
            if new_pool != pool:
                scene["sub_accounts"] = new_pool
                changed = True
    if changed:
        try:
            path.write_text(
                json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass


# ── Normalizer / denormalizer ────────────────────────────────────


def _normalize_gate_form(data: dict) -> dict:
    """Flat WebUI form → nested {main, max_sub_accounts, slot_map, sub_accounts}.

    Incremental: empty api_key/api_secret (main and sub alike) fall back to the
    previously stored values. A sub-account row is kept when any of
    name/uid/api_key/api_secret is present, dropped only when all are empty.
    Renames (same UID, different name) migrate slot_map + exec_params scene
    pools. New rows get the next free ``gate_botN`` slot.
    """
    old = read_credential("gate") or {}
    old_main = old.get("main") or {}
    old_subs = old.get("sub_accounts") or {}
    main = {
        "api_key": _strip(data.get("api_key")) or old_main.get("api_key", ""),
        "api_secret": _strip(data.get("api_secret")) or old_main.get("api_secret", ""),
        "uid": _strip(data.get("uid")),
    }
    max_subs = (
        _parse_max_subs(data.get("max_sub_accounts"))
        or _parse_max_subs(old.get("max_sub_accounts"))
        or _DEFAULT_MAX_SUBS
    )

    # Collect rows (0..max_subs-1); a row is dropped when all four fields are empty.
    rows: list[tuple[str, dict]] = []
    for i in range(max_subs):
        name = _strip(data.get(f"sub_{i}_name"))
        uid = _strip(data.get(f"sub_{i}_uid"))
        key = _strip(data.get(f"sub_{i}_api_key"))
        secret = _strip(data.get(f"sub_{i}_api_secret"))
        if not (name or uid or key or secret):
            continue
        # Incremental fallback: match old entry by uid, else by name.
        old_entry = _find_by_uid(old_subs, uid) if uid else None
        if old_entry is None and name:
            old_entry = old_subs.get(name)
        entry = {
            "uid": uid or (old_entry or {}).get("uid", ""),
            "api_key": key or (old_entry or {}).get("api_key", ""),
            "api_secret": secret or (old_entry or {}).get("api_secret", ""),
        }
        if not name:
            if old_entry is not None:
                # uid matched an old row but name was cleared → keep old name.
                name = next((n for n, s in old_subs.items() if s is old_entry), "")
            else:
                # New uid without a name → auto-assign next free gate_botN.
                name = f"gate_bot{next_free_num(_used_nums(old_subs) | {n for n, _ in rows})}"
        if not name:
            continue
        rows.append((name, entry))

    # Build subs; detect renames (same uid, different name).
    subs: dict[str, dict] = {}
    renames: dict[str, str] = {}
    for name, entry in rows:
        if name in subs:
            raise ValueError(f"子账号名字重复: {name}")
        if entry["uid"]:
            for old_name, old_s in old_subs.items():
                if old_s.get("uid") == entry["uid"] and old_name != name:
                    renames[old_name] = name
        subs[name] = entry

    # slot_map: keep old entries (filter + rename), append new names after.
    old_slot_map = old.get("slot_map") or {}
    slot_map: dict[str, str] = {}
    for slot, sname in old_slot_map.items():
        target = renames.get(sname, sname)
        if target in subs:
            slot_map[str(slot)] = target
    next_slot = max([int(s) for s in slot_map] or [0]) + 1
    for name in subs:
        if name in slot_map.values():
            continue
        while str(next_slot) in slot_map:
            next_slot += 1
        slot_map[str(next_slot)] = name
        next_slot += 1

    if renames:
        _migrate_scene_pools(renames)

    return {
        "main": main,
        "max_sub_accounts": max_subs,
        "slot_map": slot_map,
        "sub_accounts": subs,
    }


def _denormalize_gate_form(data: dict) -> dict:
    """Nested stored shape → flat form dict (for pre-filling the edit page)."""
    data = data or {}
    main = data.get("main") or {}
    subs = data.get("sub_accounts") or {}
    flat = {
        "api_key": main.get("api_key", ""),
        "api_secret": main.get("api_secret", ""),
        "uid": main.get("uid", ""),
        "max_sub_accounts": str(data.get("max_sub_accounts") or _DEFAULT_MAX_SUBS),
    }
    for i, (name, s) in enumerate(subs.items()):
        flat[f"sub_{i}_name"] = name
        flat[f"sub_{i}_uid"] = s.get("uid", "")
        flat[f"sub_{i}_api_key"] = s.get("api_key", "")
        flat[f"sub_{i}_api_secret"] = s.get("api_secret", "")
    return flat


# ── Dynamic field builder ────────────────────────────────────────

_MAIN_FIELDS = [
    FieldSpec(
        "api_key", "API Key", group="🏛️ 主账号",
        placeholder="Gate.io → API 管理（需交易权限）", required=False,
    ),
    FieldSpec("api_secret", "API Secret", group="🏛️ 主账号", required=False),
    FieldSpec(
        "uid", "UID", type="text", group="🏛️ 主账号",
        placeholder="Gate.io 个人中心显示（如 15119093）",
    ),
    FieldSpec(
        "max_sub_accounts", "子账号上限", type="text", group="🏛️ 主账号",
        placeholder=f"默认 {_DEFAULT_MAX_SUBS} · 范围 1–{_MAX_SUBS_HARD}",
    ),
]


def _gate_fields_for(current: dict) -> list[FieldSpec]:
    """Dynamic fields: main block + one row per configured sub-account.

    Unconfigured rows are not rendered — the page adds rows via the
    「➕ 添加子账号」/「🔄 从 Gate 同步子账号」buttons (JS inserts sub_{i}_* inputs).
    """
    fields = list(_MAIN_FIELDS)
    max_subs = _parse_max_subs(current.get("max_sub_accounts")) or _DEFAULT_MAX_SUBS
    i = 0
    while i < max_subs:
        name = _strip(current.get(f"sub_{i}_name"))
        uid = _strip(current.get(f"sub_{i}_uid"))
        if not (name or uid):
            break  # denormalize emits consecutive rows; stop at first empty one
        fields += [
            FieldSpec(
                f"sub_{i}_name", "名字", type="text", group=f"🤖 子账号 {i + 1}",
                placeholder="gate_botN（可修改）",
            ),
            FieldSpec(
                f"sub_{i}_uid", "UID", type="text", group=f"🤖 子账号 {i + 1}",
                placeholder="子账号 UID（账户管理页查看）",
            ),
            FieldSpec(
                f"sub_{i}_api_key", "API Key", group=f"🤖 子账号 {i + 1}",
                placeholder="P3 分批下单签名用，可留空",
            ),
            FieldSpec(
                f"sub_{i}_api_secret", "API Secret", group=f"🤖 子账号 {i + 1}",
            ),
        ]
        i += 1
    return fields


def _gate_form_extra(current: dict) -> str:
    """Sub-account toolbar injected into the credential edit form.

    「🔄 从 Gate 同步子账号」queries the Gate API (POST
    /config/gate/sync-subaccounts, commander-guarded) and fills the form with
    newly discovered rows — nothing is written until the user clicks save.
    「➕ 添加子账号」inserts an empty row for manual entry.
    """
    configured = 0
    max_subs = _parse_max_subs(current.get("max_sub_accounts")) or _DEFAULT_MAX_SUBS
    for i in range(max_subs):
        if current.get(f"sub_{i}_name") or current.get(f"sub_{i}_uid"):
            configured += 1
        else:
            break
    return f"""
<div id="sub-rows"></div>
<div class="flex-row" style="margin:14px 0 4px;">
  <button type="button" class="btn-primary" onclick="syncSubs()">🔄 从 Gate 同步子账号</button>
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
      '<div class="form-group"><label>名字</label><input type="text" name="sub_' + i + '_name" value="' + (name || '') + '" placeholder="gate_botN（可修改）"></div>' +
      '<div class="form-group"><label>UID</label><input type="text" name="sub_' + i + '_uid" value="' + (uid || '') + '" placeholder="子账号 UID"></div>' +
      '<div class="form-group"><label>API Key</label><input type="text" name="sub_' + i + '_api_key" value="" placeholder="P3 分批下单签名用，可留空"></div>' +
      '<div class="form-group"><label>API Secret</label><input type="text" name="sub_' + i + '_api_secret" value=""></div>' +
      '<div style="text-align:right"><button type="button" class="btn-danger" style="padding:4px 12px;font-size:.82em" onclick='this.closest(".cred-group").remove()'>✖ 删除该行</button></div>' +
    '</div></div>';
}}
function addSubRow(name, uid) {{
  if (subRowIdx >= subRowMax) {{
    document.getElementById('sync-msg').innerHTML = '<div class="err">❌ 子账号数量已达上限 ' + subRowMax + '（可在主账号区修改「子账号上限」）</div>';
    return;
  }}
  document.getElementById('sub-rows').insertAdjacentHTML('beforeend', subRowHTML(name, uid));
}}
async function syncSubs() {{
  const el = document.getElementById('sync-msg');
  el.innerHTML = '<span style="color:#666">⏳ 正在从 Gate 查询子账号…</span>';
  try {{
    const resp = await fetch('/config/gate/sync-subaccounts', {{method:'POST'}});
    const r = await resp.json();
    if (!r.ok) {{ el.innerHTML = '<div class="err">❌ ' + (r.error || '同步失败') + '</div>'; return; }}
    if (!r.added || !r.added.length) {{ el.innerHTML = '<div class="ok">✅ 无新增子账号（Gate 共 ' + r.total + ' 个，已全部配置）</div>'; return; }}
    r.added.forEach(a => addSubRow(a.name, a.uid));
    el.innerHTML = '<div class="ok">✅ 已填充 ' + r.added.length + ' 个新增子账号（Gate 共 ' + r.total + ' 个）。确认无误后点「💾 保存」写入。</div>';
  }} catch (e) {{
    el.innerHTML = '<div class="err">❌ 同步请求失败: ' + e + '</div>';
  }}
}}
</script>
"""


GATE_SPEC = CredentialSpec(
    name="gate",
    display="Gate.io",
    description=(
        "Gate CEX 交易凭证（执行通道=cex 时使用）。主账号 Key 生成后自动保留；"
        "子账号（默认 gate_botN）UID 用于 slot↔子账号映射，"
        "子账号 API Key/Secret 仅 P3 分批下单（TD cex 子账号实跑）需要，余额/划转可留空。"
        "「🔄 从 Gate 同步子账号」可一键拉取官方后台新增的子账号（只填充表单，确认后保存）。"
    ),
    icon="🏛️",
    fields=_MAIN_FIELDS,
    docs_url="https://www.gate.io/myaccount/api_keys",
    normalize=_normalize_gate_form,
    denormalize=_denormalize_gate_form,
    fields_for=_gate_fields_for,
    form_extra=_gate_form_extra,
)

register(GATE_SPEC)
