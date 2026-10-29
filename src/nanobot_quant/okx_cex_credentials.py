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
import sys
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
        matches = [
            s for s in subs
            if s.get("uid") == account or s.get("name") == account
        ]
        if len(matches) > 1:
            # 同名/同 uid 多行（手工编辑或历史遗留）→ 取第一个会命中旧 key，
            # 表现为「期权能用、现货 50119」：优先选三字段齐全的那条。
            configured = [m for m in matches if all(m.get(k) for k in _REQUIRED)]
            print(
                f"[DIAG] okx_cex 凭证存在 {len(matches)} 条同名/同 uid 条目"
                f"（{account}），已优先选三字段齐全的一条——建议在凭证页删掉重复行",
                file=sys.stderr, flush=True,
            )
            matches = configured or matches
        entry = matches[0] if matches else None
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
    return {k: str(entry[k]).strip() for k in _REQUIRED}


def option_account_name() -> str:
    """期权线占用的子账号（现货池必须排除它）。

    设计（用户 2026-10-29 确认）：**期权用第一个子账号，现货按场景用剩下的**。
    来源 ``option_params.json → live.strategy.account``；留空表示「默认账号」，
    即 ``okx_cex.json`` 里第一个已配置的子账号（与 ``_entry_account`` 同口径）。
    """
    name = ""
    try:
        from .credential_registry import _get_storage_dir

        path = Path(_get_storage_dir()) / "option_params.json"
        cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        strat = ((cfg.get("live") or {}).get("strategy") or {})
        name = str(strat.get("account") or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    if name:
        return name
    try:
        rows = [s for s in list_sub_accounts() if s.get("configured")]
    except Exception:  # noqa: BLE001
        return ""
    return (rows[0].get("name") or rows[0].get("uid") or "") if rows else ""


def spot_account_names(creds: Optional[dict] = None) -> list[str]:
    """现货池账号名 = 已配置子账号 − 期权占用账号。

    与 ``batches.shared_pool_account_ids`` 同一口径（单一来源：期权账号一律排除），
    供 broker 聚合余额用：shared_pool 下主 broker 不绑具体子账号，若不显式
    指定就会落到 ``account_balance(None)`` = **默认账号（第一个，即期权账号）**
    → 50119 → 余额 0 → portfolio BLOCK（2026-10-29 okx_cex 实测）。
    """
    try:
        rows = [s for s in list_sub_accounts(creds) if s.get("configured")]
    except Exception:  # noqa: BLE001
        return []
    names = [str(s.get("name") or s.get("uid") or "") for s in rows]
    names = [n for n in names if n]
    try:
        opt = option_account_name()
    except Exception:  # noqa: BLE001
        opt = ""
    return [n for n in names if n != opt]


def preflight_accounts(creds: Optional[dict] = None) -> list[dict]:
    """启动自检：对每个子账号做一次余额探测，返回逐账号结果。

    动因（2026-10-29 实测）：切到 ``okx_cex`` 后每轮只看到一句
    ``OKX-CEX balance error: 401 … code 50119``（API key doesn't exist），
    无法看出是**哪个**子账号、也没在启动时暴露——这里在循环启动前逐个
    校验，把「凭证无效 / 缺字段」一次性打清楚（密钥一律脱敏，只给尾四位）。
    """
    out: list[dict] = []
    opt_account = option_account_name()
    stored = normalize_stored(creds) if creds is not None else (
        load_okx_cex_credentials() or {}
    )
    raw = {
        (s.get("name") or s.get("uid") or ""): s
        for s in (stored.get("sub_accounts") or [])
    }

    def _fp(entry: dict) -> str:
        """密钥指纹（**脱敏**）：只给长度与尾四位。

        50119（API key doesn't exist）最常见诱因是“别的 key / 粘截断了 /
        带空白”——长度与尾四位足以和 OKX 后台逐位对账，且不泄露密钥。
        """
        key = str(entry.get("api_key") or "")
        if not key:
            return "key=(空)"
        dirty = "含空白" if key != key.strip() or any(c.isspace() for c in key) else ""
        return f"key=****{key[-4:]}(len={len(key)}{',' + dirty if dirty else ''})"

    dup = {
        n: sum(1 for s in (stored.get("sub_accounts") or [])
               if (s.get("name") or s.get("uid") or "") == n)
        for n in raw
    }

    for s in list_sub_accounts(stored):
        name = s.get("name") or s.get("uid") or "(未命名)"
        fp = _fp(raw.get(name) or raw.get(s.get("uid") or "") or {})
        if dup.get(name, 0) > 1:
            fp += f" dup={dup[name]}"
        fp += " role=option" if name == opt_account else " role=spot"
        if not s.get("configured"):
            out.append({"name": name, "ok": False, "fingerprint": fp,
                        "error": "缺字段: " + ", ".join(s.get("missing") or [])})
            continue
        try:
            from .okx_options_trade import account_balance

            account_balance(s.get("name") or s.get("uid") or "")
            out.append({"name": name, "ok": True, "fingerprint": fp, "error": ""})
        except Exception as exc:  # noqa: BLE001
            out.append({"name": name, "ok": False, "fingerprint": fp,
                        "error": f"{type(exc).__name__}: {exc}"})
    return out
