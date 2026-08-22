"""API Credential registry — pluggable credential types for business management.

Each credential type (OKX, Binance, Alpaca…) defines a CredentialSpec with
its fields, and registers via ``credential_registry.register()``.

Gatekeeper discovers registered credentials at startup and auto-generates
the credential management page under /config/credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

# ── Storage path ────────────────────────────────────────────────
# Set via init_storage() at startup (called by gatekeeper with platform data_root).
# Fallback path for non-gatekeeper contexts.
_initialized_dir: Optional[str] = None


def init_storage(data_root: str) -> None:
    """Set credential storage directory. Called by gatekeeper at startup.

    credentials live at ``{data_root}/credentials/`` (e.g. /data/legion/credentials/).
    """
    global _initialized_dir
    _initialized_dir = os.path.join(data_root, "credentials")


def _get_storage_dir() -> str:
    """Return the active credential storage directory."""
    if _initialized_dir:
        return _initialized_dir
    # Fallback: env override or platform-agnostic default
    if d := os.environ.get("CREDENTIAL_STORAGE_DIR"):
        return d
    # Try known data roots
    for root in ("/data/legion", "/mnt/workspace/legion"):
        if os.path.isdir(root):
            return os.path.join(root, "credentials")
    return "/data/credentials"


# ── Spec dataclasses ─────────────────────────────────────────────

@dataclass
class FieldSpec:
    """A single credential field definition."""
    name: str               # "api_key"
    label: str              # "API Key"
    type: str = "password"  # "password" | "text"
    placeholder: str = ""
    required: bool = True
    readonly: bool = False  # render as disabled input (display-only, not editable)
    options: list[str] = field(default_factory=list)  # non-empty → render as <select>
    group: str = ""        # non-empty → rendered as a titled card section (e.g. "🤖 gate_bot1")


@dataclass
class CredentialSpec:
    """Credential type definition for auto-discovery.

    Example:
        CredentialSpec(
            name="okx",
            display="OKX OnchainOS",
            description="OKX DEX API 凭证，用于获取链上行情数据和交易执行",
            icon="🔑",
            fields=[
                FieldSpec("api_key", "API Key", placeholder="从 OKX 开发者中心获取"),
                FieldSpec("secret_key", "Secret Key"),
                FieldSpec("passphrase", "Passphrase"),
            ],
            docs_url="https://www.okx.com/account/my-api",
        )
    """
    name: str
    display: str
    description: str
    icon: str = "🔑"
    fields: list[FieldSpec] = field(default_factory=list)
    docs_url: Optional[str] = None
    # Optional flat-form → stored-shape normalizer (e.g. gate: flat WebUI form
    # with sub-account keys → nested {main, sub_accounts, slot_map}). Called by
    # credential_save before write. Inverse is denormalize (stored → form).
    normalize: Optional[Callable[[dict], dict]] = field(default=None, repr=False)
    denormalize: Optional[Callable[[dict], dict]] = field(default=None, repr=False)
    # Optional dynamic field builder: fields_for(current_flat) -> list[FieldSpec]
    # used instead of ``fields`` when the form shape depends on stored data
    # (e.g. gate sub-account rows grow with synced accounts).
    fields_for: Optional[Callable[[dict], list]] = field(default=None, repr=False)
    # Optional extra HTML injected into the edit form between the fields and the
    # save buttons (e.g. gate sub-account sync toolbar + JS).
    form_extra: Optional[Callable[[dict], str]] = field(default=None, repr=False)


# ── Registry ─────────────────────────────────────────────────────

_registry: dict[str, CredentialSpec] = {}


def register(spec: CredentialSpec) -> None:
    """Register a credential type. Called at module import time."""
    _registry[spec.name] = spec


def discover() -> dict[str, CredentialSpec]:
    """Return all registered credential specs. Gatekeeper calls this."""
    return dict(_registry)


# ── Persistence helpers ──────────────────────────────────────────


def _credential_path(name: str) -> str:
    """Return the JSON file path for a named credential."""
    d = _get_storage_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{name}.json")


def read_credential(name: str) -> dict:
    """Read a stored credential, returning {} if not found."""
    try:
        with open(_credential_path(name)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_credential(name: str, data: dict) -> None:
    """Write a credential to disk."""
    dp = _credential_path(name)
    with open(dp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(dp, 0o600)


def delete_credential(name: str) -> bool:
    """Delete a stored credential. Returns True if deleted, False if not found."""
    dp = _credential_path(name)
    try:
        os.remove(dp)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def is_configured(name: str) -> bool:
    """Check if a credential has been configured."""
    return os.path.exists(_credential_path(name))


def all_configured() -> dict[str, dict]:
    """Return all configured credentials as {name: data}."""
    result = {}
    d = _get_storage_dir()
    if not os.path.isdir(d):
        return result
    for fname in os.listdir(d):
        if fname.endswith(".json"):
            name = fname[:-5]
            data = read_credential(name)
            if data:
                result[name] = data
    return result
