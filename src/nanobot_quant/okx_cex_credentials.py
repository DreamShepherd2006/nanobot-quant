"""OKX CEX credential loading — persistent file storage like okx.json.

Independent from the OKX OnchainOS credential (``okx.json``): this file holds
the OKX **exchange** sub-account key used for options trading.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

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


def load_okx_cex_credentials() -> Optional[dict]:
    """Read OKX CEX credentials from persistent file.

    Returns ``None`` when no file exists; an empty dict on unreadable/corrupt
    files (callers decide whether to raise).
    """
    path = _find_credential_file()
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def get_okx_cex_credentials(creds: Optional[dict] = None) -> dict:
    """Return validated OKX CEX credentials (api_key/secret_key/passphrase).

    Pass ``creds`` explicitly to inject credentials (tests / callers that
    already hold them); otherwise auto-load from the credential file.
    Raises ``RuntimeError`` with a clear message when any of the three
    OKX-required fields is missing.
    """
    if creds is None:
        creds = load_okx_cex_credentials() or {}
    missing = [k for k in _REQUIRED if not creds.get(k)]
    if missing:
        raise RuntimeError(
            "okx_cex 凭证不完整，缺: " + ", ".join(missing)
            + "（OKX API 需 api_key + secret_key + passphrase 三要素；"
            + "请到 WebUI 凭证管理页填写，勿与 OnchainOS okx 凭证混用）"
        )
    return {k: creds[k] for k in _REQUIRED}
