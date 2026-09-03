"""OKX CEX credential loading — persistent file storage like okx.json.

Independent from the OKX OnchainOS credential (``okx.json``): this file holds
OKX **exchange** sub-account keys used for options trading.

Stored shape (see okx_cex_spec for canonicalization)::

    {"max_sub_accounts": 10,
     "sub_accounts": [{"name", "uid", "api_key", "secret_key", "passphrase"}, ...]}

Legacy single flat entries (top-level api_key/secret_key/passphrase) are
migrated to the first sub-account row transparently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .okx_cex_spec import normalize_stored

_REQUIRED = ("api_key", "secret_key", "passphrase")


# ── Preferred credential path (delegates to credential_registry) ─
def _get_credential_path() -> Path:
    """Return the primary credential file path for OKX CEX."""
    from .credential_registry import _get_storage_dir

    return Path(_get_storage_dir()) / "okx_cex.json"


def _find_credential_file() -> Optional[Path]:
    """Return the first existing credential file, or the preferred path."""
    primary = _get_credential_path()
    if primary.exists():
        return primary
    # Legacy fallbacks (kept for parity with okx_credentials discovery)
    for legacy in (
        Path("/data/legion/credentials/okx_cex.json"),
        Path("/mnt/workspace/legion/credentials/okx_cex.json"),
    ):
        if legacy.exists():
            return legacy
    return primary  # preferred path for writes


def save_okx_cex_credentials(data: dict) -> Path:
    """Atomically persist a canonicalized okx_cex credential dict."""
    stored = normalize_stored(data)
    path = _get_credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)
    return path


def load_okx_cex_credentials() -> Optional[dict]:
    """Read OKX CEX credentials (canonical nested shape).

    Returns ``None`` when no file exists; a canonicalized empty dict
    (``{"max_sub_accounts": 10, "sub_accounts": []}``) for empty/corrupt files.
    """
    path = _find_credential_file()
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            return normalize_stored({})
        return normalize_stored(data)
    except (json.JSONDecodeError, OSError):
        return normalize_stored({})


def list_sub_accounts(creds: Optional[dict] = None) -> list[dict]:
    """Return sub-account entries with a ``configured`` flag (three fields set)."""
    stored = creds if creds is not None else (load_okx_cex_credentials() or {})
    subs = normalize_stored(stored).get("sub_accounts") or []
    out = []
    for s in subs:
        missing = [k for k in _REQUIRED if not s.get(k)]
        out.append(
            {
                "name": s.get("name", ""),
                "uid": s.get("uid", ""),
                "configured": not missing,
                "missing": missing,
            }
        )
    return out


def get_okx_cex_credentials(
    creds: Optional[dict] = None, account: Optional[str] = None
) -> dict:
    """Return validated three-field credentials for one sub-account.

    ``account`` selects by sub-account ``name`` or ``uid``; when omitted the
    first fully-configured sub-account is used. Pass ``creds`` to inject the
    stored dict explicitly (tests / callers that already hold it); otherwise
    auto-load from the credential file.
    Raises ``RuntimeError`` with a clear message when no suitable entry exists
    or the OKX-required fields (api_key/secret_key/passphrase) are missing.
    """
    stored = normalize_stored(creds) if creds is not None else (
        load_okx_cex_credentials() or {}
    )
    subs = stored.get("sub_accounts") or []

    entry = None
    if account:
        for s in subs:
            if s.get("uid") == account or s.get("name") == account:
                entry = s
                break
        if entry is None:
            raise RuntimeError(
                f"okx_cex 未找到子账户: {account}（已配置: "
                + (", ".join(s.get("name") or s.get("uid") for s in subs) or "无")
                + "）"
            )
    else:
        entry = next((s for s in subs if all(s.get(k) for k in _REQUIRED)), None)
        if entry is None and subs:
            missing = [k for k in _REQUIRED if not subs[0].get(k)]
            raise RuntimeError(
                "okx_cex 凭证不完整，缺: " + ", ".join(missing)
                + "（OKX API 需 api_key + secret_key + passphrase 三要素；"
                + "请到 WebUI 凭证管理页填写，勿与 OnchainOS okx 凭证混用）"
            )

    if entry is None:
        raise RuntimeError(
            "okx_cex 凭证未配置（WebUI 凭证管理页添加子账户后即可使用）"
        )
    missing = [k for k in _REQUIRED if not entry.get(k)]
    if missing:
        raise RuntimeError(
            f"okx_cex 子账户 {entry.get('name') or entry.get('uid') or '?'} "
            "凭证不完整，缺: " + ", ".join(missing)
        )
    return {k: entry[k] for k in _REQUIRED}
