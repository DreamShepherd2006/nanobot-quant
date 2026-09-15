"""OKX 期权标的映射助手（Asset ↔ instId 唯一实现）。

lumibot 的期权 Asset 与 OKX USDSⓈ-M 合约 ID 互转——Broker（下单/持仓）与
DataSource（期权链/报价/mark K 线）共用同一套映射，避免两处各写一份。

合约 ID 形态（U 本位线性，2026-09-04 定稿，docs/quant-system.md §33.5）：
    SOL-USD_UM-260918-94-P   标的-USD_UM 家族 + 到期 YYMMDD + 行权价 + C/P

每张面值（lot）随家族不同（SOL 0.1、BTC/ETH/XAU 0.01 币/张），由
``okx_options_trade.FAMILY_LOT`` 提供唯一来源；构造期权 Asset 时必须作为
``multiplier`` 显式传入——lumibot 对期权的默认乘数是 100（美股约定），
只有 lumibot fork 的 patch（honor explicit option multiplier）才让显式值生效。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from nanobot_quant.okx_options_trade import FAMILY_LOT

FAMILY_SUFFIX = "-USD_UM"


def base_of_family(family: str) -> str:
    """``SOL-USD_UM`` → ``SOL``。"""
    return str(family or "").split("-")[0].upper()


def family_of(base: str) -> str:
    """``SOL`` → ``SOL-USD_UM``。"""
    return f"{str(base).upper()}{FAMILY_SUFFIX}"


def lot_of(base: str) -> float:
    """该标的每张面值（币/张）；未知标的回退 0.0（调用方自行 fail-closed）。"""
    return float(FAMILY_LOT.get(str(base).upper(), 0.0))


def _strike_str(strike) -> str:
    f = float(strike)
    return str(int(f)) if f.is_integer() else ("%g" % f)


def _right_code(right) -> str:
    s = str(getattr(right, "value", right) or "").upper()
    return "C" if s.startswith("C") else "P"


def _exp_code(expiration) -> str:
    if isinstance(expiration, datetime):
        d = expiration.date()
    elif isinstance(expiration, date):
        d = expiration
    else:
        d = datetime.strptime(str(expiration)[:10], "%Y-%m-%d").date()
    return d.strftime("%y%m%d")


def asset_to_inst(asset) -> str:
    """lumibot 期权 Asset → OKX instId（如 SOL-USD_UM-260918-94-P）。"""
    base = str(getattr(asset, "symbol", "") or "").upper()
    return "-".join([
        family_of(base), _exp_code(asset.expiration),
        _strike_str(asset.strike), _right_code(asset.right),
    ])


def inst_to_asset(inst_id: str):
    """OKX instId → lumibot 期权 Asset（multiplier = 每张面值）。

    返回 None 表示 instId 非法/家族未知（调用方 fail-closed，不猜）。
    """
    try:
        from lumibot.entities import Asset
    except Exception:  # pragma: no cover - lumibot 缺失时调用方另有兜底
        return None
    parts = str(inst_id or "").split("-")
    if len(parts) < 5:
        return None
    base = parts[0].upper()
    lot = lot_of(base)
    if lot <= 0:
        return None
    try:
        exp = datetime.strptime(parts[-3], "%y%m%d").date()
        strike = float(parts[-2])
    except (ValueError, IndexError):
        return None
    right = "CALL" if parts[-1].upper() == "C" else "PUT"
    return Asset(symbol=base, asset_type="option", expiration=exp,
                 strike=strike, right=right, multiplier=lot)


def family_of_inst(inst_id: str) -> str:
    """``SOL-USD_UM-260918-94-P`` → ``SOL-USD_UM``。"""
    parts = str(inst_id or "").split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else ""
