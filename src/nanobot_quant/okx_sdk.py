"""OKX 官方 python-okx SDK（okx==1.0.9）薄封装 — 期权线统一接入层。

- 修正官方 SDK 默认域名（``www.okex.com`` 已废弃 → ``www.okx.com``）
- 统一 code 校验：``code != "0"`` 抛 :class:`OkxSdkError`
- 实例按需缓存（公共单例 / 私有按 creds 三元组）

接入原则（用户拍板 2026-08-14 起 permanent）：交易所接入优先用官方 SDK，
不自写签名/请求层——本模块是唯一 import ``okx`` 的地方，数据/账户/交易
模块统一经它取实例。
"""

from __future__ import annotations

import functools
import types

from okx._client import ResponseStatusError
from okx.account import Account
from okx.market import Market
from okx.public import Public
from okx.trade import Trade

_OKX_BASE = "https://www.okx.com"

# 官方 SDK 1.0.9 默认 API_URL 指向已废弃的 www.okex.com（DNS 失效），
# 覆写为 www.okx.com（2026-09-04 实测 1.0.9 全部公共端点正常）。
Public.API_URL = _OKX_BASE
Market.API_URL = _OKX_BASE
Account.API_URL = _OKX_BASE
Trade.API_URL = _OKX_BASE


class OkxSdkError(RuntimeError):
    """OKX API 错误（code != 0 / HTTP 层 4xx），携带可读信息。"""


def _guard_okx_methods(cls: type) -> None:
    """包装 python-okx 类的公开方法：HTTP 层 :class:`ResponseStatusError` →
    :class:`OkxSdkError`。

    参数非法等会由 send_request 直接抛 ``ResponseStatusError``（如 400/50015、
    401/50123），发生在调用表达式求值阶段——``check()`` 包不住。统一在方法
    层转成业务异常，handler 只须 catch OkxSdkError 即可在页面显示 OKX 错误码，
    而不是 500。
    """
    for name in dir(cls):
        if name.startswith("_"):
            continue
        attr = getattr(cls, name)
        if not isinstance(attr, types.FunctionType):
            continue
        if getattr(attr, "_okx_guarded", False):
            continue

        @functools.wraps(attr)
        def _w(*args, _fn=attr, **kw):
            try:
                return _fn(*args, **kw)
            except ResponseStatusError as e:
                raise OkxSdkError(str(e)) from e

        _w._okx_guarded = True
        setattr(cls, name, _w)


for _cls in (Public, Market, Account, Trade):
    _guard_okx_methods(_cls)

_public = Public(flag="0")   # 公共数据（instruments/opt-summary）
_market = Market(flag="0")   # 行情（tickers/candles）
_accounts: dict[tuple, Account] = {}
_trades: dict[tuple, Trade] = {}


def public() -> Public:
    return _public


def market() -> Market:
    return _market


def account_for(creds: dict) -> Account:
    """按 (api_key, secret_key, passphrase) 三元组缓存的私有 Account 实例。"""
    key = (creds.get("api_key"), creds.get("secret_key"), creds.get("passphrase"))
    acc = _accounts.get(key)
    if acc is None:
        acc = Account(key=creds.get("api_key", ""), secret=creds.get("secret_key", ""),
                       passphrase=creds.get("passphrase", ""), flag="0")
        _accounts[key] = acc
    return acc


def trade_for(creds: dict) -> Trade:
    """按凭证三元组缓存的 Trade 实例（下单/撤单/查单）。"""
    key = (creds.get("api_key"), creds.get("secret_key"), creds.get("passphrase"))
    tr = _trades.get(key)
    if tr is None:
        tr = Trade(key=creds.get("api_key", ""), secret=creds.get("secret_key", ""),
                    passphrase=creds.get("passphrase", ""), flag="0")
        _trades[key] = tr
    return tr


def check(payload: dict):
    """校验 OKX 响应信封，code=0 返回 data，否则抛 :class:`OkxSdkError`。"""
    code = payload.get("code")
    if code != "0":
        msg = payload.get("msg", "")
        raise OkxSdkError(f"OKX {code} {msg}".strip())
    return payload.get("data")
