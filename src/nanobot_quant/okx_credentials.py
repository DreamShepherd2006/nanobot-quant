"""OKX credential management — persistent file storage like oauth.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# ── Default paths (checked in order) ──────────────────────────────
_CREDENTIAL_PATHS = [
    Path("/data/credentials/okx.json"),  # preferred: credential_registry path
    Path("/mnt/workspace/credentials/okx.json"),
    Path("/data/okx_credentials.json"),  # legacy
    Path("/mnt/workspace/okx_credentials.json"),
]


def _find_credential_file() -> Optional[Path]:
    """Return the first existing credential file, or the preferred path."""
    for p in _CREDENTIAL_PATHS:
        if p.exists():
            return p
    return _CREDENTIAL_PATHS[0]  # preferred path for writes


def _read_credentials() -> dict:
    """Read OKX credentials from persistent file.  Returns empty dict on failure."""
    path = _find_credential_file()
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def _write_credentials(data: dict) -> None:
    """Write OKX credentials to persistent file (atomic write)."""
    path = _find_credential_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    tmp.replace(path)
    tmp.chmod(0o600)


def get_okx_api_key() -> Optional[str]:
    return _read_credentials().get("api_key")


def get_okx_secret_key() -> Optional[str]:
    return _read_credentials().get("secret_key")


def get_okx_passphrase() -> Optional[str]:
    return _read_credentials().get("passphrase")


def is_configured() -> bool:
    """Return True when all three credentials are present."""
    c = _read_credentials()
    return bool(c.get("api_key") and c.get("secret_key") and c.get("passphrase"))


def inject_env() -> None:
    """Export credentials into os.environ so onchainos CLI can read them.

    Safe to call multiple times — never overwrites an already-set env var.
    """
    c = _read_credentials()
    _maybe_set("OKX_API_KEY", c.get("api_key"))
    _maybe_set("OKX_SECRET_KEY", c.get("secret_key"))
    _maybe_set("OKX_PASSPHRASE", c.get("passphrase"))


def _maybe_set(key: str, value: Optional[str]) -> None:
    if value and key not in os.environ:
        os.environ[key] = value
