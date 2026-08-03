"""OKX credential management — persistent file storage like oauth.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


# ── Preferred credential path (delegates to credential_registry) ─
def _get_credential_path() -> Path:
    """Return the primary credential file path for OKX."""
    from .credential_registry import _get_storage_dir
    return Path(_get_storage_dir()) / "okx.json"


# ── Discovery: preferred path + legacy fallbacks ──────────────────
def _find_credential_file() -> Optional[Path]:
    """Return the first existing credential file, or the preferred path."""
    primary = _get_credential_path()
    if primary.exists():
        return primary
    # Legacy fallbacks
    for legacy in (
        Path("/data/okx_credentials.json"),
        Path("/mnt/workspace/okx_credentials.json"),
    ):
        if legacy.exists():
            return legacy
    return primary  # preferred path for writes


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
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    tmp.replace(path)
    tmp.chmod(0o600)


def get_okx_api_key() -> Optional[str]:
    return _read_credentials().get("api_key")


def get_okx_secret_key() -> Optional[str]:
    return _read_credentials().get("secret_key")


def get_okx_passphrase() -> Optional[str]:
    return _read_credentials().get("passphrase")


def get_wallet_address() -> Optional[str]:
    """Return the user's personal OKX Web3 wallet address from okx.json.

    NOTE: This is the USER'S PERSONAL wallet address (e.g. ArWUBs...) —
    kept only for backward compatibility / display purposes. It does NOT
    determine the swap broadcast or quote address. The actual trading
    address is the Agentic Wallet bound to the API key, resolved at
    runtime via get_active_wallet_address().
    """
    return _read_credentials().get("wallet_address")


def get_chain() -> str:
    """Return the trading chain from okx.json (default "solana")."""
    return _read_credentials().get("chain") or "solana"


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
