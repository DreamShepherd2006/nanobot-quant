"""OKX CEX 私有账户只读 API — 期权线批次 A（基于官方 python-okx SDK）。

批次 A 原为手写 HMAC 签名客户端；2026-09-04 按用户偏好（交易所接入优先
官方 SDK，见 :mod:`okx_sdk`）整体迁移到官方 ``okx==1.0.9`` 的
``Account`` API。对外函数签名与返回形状保持不变，调用方零改动。

只读端点（config / balance / positions / trade-fee）；卖 put 下单在批次 C
经官方 ``Trade`` API 加入。
"""

from __future__ import annotations

from typing import Optional

from nanobot_quant import okx_sdk
from nanobot_quant.okx_sdk import OkxSdkError
from .okx_cex_credentials import get_okx_cex_credentials

#: instType / instFamily used by the options line (BTC-USD & ETH-USD are the
#: only option families OKX lists; cf. options research notes 2026-09-03).
OPTION_FAMILIES = ("BTC-USD", "ETH-USD")


def _account(creds: Optional[dict] = None):
    """按（可选的调用方覆盖）凭证取 SDK Account 实例。

    creds=None → 默认取 okx_cex 凭证存储的首个完整子账户（主行）。
    """
    creds = get_okx_cex_credentials(creds)
    return okx_sdk.account_for(creds)


# ── read-only account endpoints ──────────────────────────────────

def get_account_config(creds: Optional[dict] = None) -> dict:
    """GET /api/v5/account/config — account level / permissions / uid."""
    data = okx_sdk.check(_account(creds).get_config())
    return (data or [{}])[0]


def get_balance(creds: Optional[dict] = None) -> dict:
    """GET /api/v5/account/balance — normalized per-currency balances.

    Returns ``{"total_eq": float, "details": [{"ccy", "cash", "avail", "frozen", "eq"}, ...]}``
    """
    data = okx_sdk.check(_account(creds).get_balance())
    row = (data or [{}])[0]
    details = []
    for d in row.get("details") or []:
        details.append(
            {
                "ccy": d.get("ccy"),
                "cash": _f(d.get("cashBal")),
                "avail": _f(d.get("availBal")),
                "frozen": _f(d.get("frozenBal")),
                "eq": _f(d.get("eq")),
            }
        )
    return {"total_eq": _f(row.get("totalEq")), "details": details}


def get_positions(
    inst_type: str = "OPTION", creds: Optional[dict] = None
) -> list:
    """GET /api/v5/account/positions — current positions (default options)."""
    data = okx_sdk.check(_account(creds).get_positions(instType=inst_type))
    return data if isinstance(data, list) else []


def get_trade_fee(
    inst_family: str = "BTC-USD", creds: Optional[dict] = None
) -> dict:
    """GET /api/v5/account/trade-fee — maker/taker fee rate for a family."""
    data = okx_sdk.check(_account(creds).get_trade_fee(
        instType="OPTION", instFamily=inst_family))
    return (data or [{}])[0]


def _f(value) -> float:
    """Coerce OKX numeric string to float (empty/None → 0.0)."""
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
