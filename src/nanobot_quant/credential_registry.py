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
from typing import Optional

# ── Storage path ────────────────────────────────────────────────
# Override via CREDENTIAL_STORAGE_DIR env var for different platforms.
CREDENTIAL_STORAGE_DIR = os.environ.get("CREDENTIAL_STORAGE_DIR", "/data/credentials")


# ── Spec dataclasses ─────────────────────────────────────────────

@dataclass
class FieldSpec:
    """A single credential field definition."""
    name: str               # "api_key"
    label: str              # "API Key"
    type: str = "password"  # "password" | "text"
    placeholder: str = ""
    required: bool = True


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
    os.makedirs(CREDENTIAL_STORAGE_DIR, exist_ok=True)
    return os.path.join(CREDENTIAL_STORAGE_DIR, f"{name}.json")


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
    if not os.path.isdir(CREDENTIAL_STORAGE_DIR):
        return result
    for fname in os.listdir(CREDENTIAL_STORAGE_DIR):
        if fname.endswith(".json"):
            name = fname[:-5]
            data = read_credential(name)
            if data:
                result[name] = data
    return result
