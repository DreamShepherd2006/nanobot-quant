"""options_broker_selftest: 期权执行层只读自检（E 期 Step 2a 验证入口）。

一次调用确认四件事，**全部只读**（不下单、不改台账、不动保证金）：

1. 期权链（okx_options_data）——家族/现货参考/到期档/可卖合约数
2. Asset ↔ instId 映射 + 每张面值 multiplier
   （multiplier 不符即说明 lumibot fork patch 未生效——期权会按美股 100 计）
3. 期权子账号（账户配置：期权权限/保证金模式/结算币；余额）
4. 当前期权持仓（张数/成本/标记价/保证金）

为什么需要它：Broker/DataSource 是薄适配层，单元测试只能证「转发逻辑」；
「lumibot 期权标的 × OKX 账户」两侧真的接上、multiplier 真的非 100，
只能在容器里实拉一次才算验证（用户原则：功能生效必须有运行时可见证据）。
"""

from __future__ import annotations


def options_broker_selftest(account: str = "", family: str = "SOL-USD_UM") -> dict:
    """期权执行层只读自检（不下单/不改台账/不动保证金）。

    Args:
        account: 期权子账号名/label（如 "DreamShepherdbot1"），空 = 默认账号。
        family: OKX instFamily（如 "SOL-USD_UM"、"BTC-USD_UM"）。

    Returns:
        dict: status=ok（全部通过）/ partial（部分失败，failed 列出）/ error（链不可用）。
              checks 逐项含 status/数据/失败原因。
    """
    from nanobot_quant import okx_options_data as ood
    from nanobot_quant import okx_options_trade as oot

    checks: dict = {}

    # ① 期权链（同时为 ② 取一个真实合约样本）
    try:
        chain = ood.fetch_chain(family, spot_pct_range=None)
        rows = sum(len(g.get("rows") or []) for g in chain.get("groups") or [])
        checks["chain"] = {
            "status": "ok",
            "family": family,
            "spot": chain.get("spot"),
            "lot_coin": chain.get("lot_coin"),
            "expiries": [g.get("date") for g in chain.get("groups") or []],
            "strike_rows": rows,
        }
    except Exception as e:
        checks["chain"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        return {"status": "error", "checks": checks,
                "note": "期权链不可用（凭证/网络/家族名），后续检查跳过"}

    # ② Asset ↔ instId + 每张面值（lumibot fork patch 生效证据）
    sample = ""
    for g in chain.get("groups") or []:
        for row in g.get("rows") or []:
            inst = (row.get("P") or {}).get("inst_id")
            if inst:
                sample = inst
                break
        if sample:
            break
    if sample:
        try:
            from nanobot_quant.okx_options_assets import (
                asset_to_inst,
                inst_to_asset,
                lot_of,
            )
            asset = inst_to_asset(sample)
            back = asset_to_inst(asset) if asset is not None else None
            expected_lot = lot_of(family.split("-")[0])
            got = float(getattr(asset, "multiplier", 0) or 0) if asset is not None else 0.0
            ok = bool(asset is not None and back == sample
                      and expected_lot > 0 and abs(got - expected_lot) < 1e-12)
            checks["asset_mapping"] = {
                "status": "ok" if ok else "error",
                "inst_id": sample,
                "roundtrip": back,
                "multiplier": got,
                "expected_multiplier": expected_lot,
                "note": "lumibot fork patch 生效（期权乘数=每张面值）" if ok else
                        "multiplier 不符——lumibot 可能未装 fork patch，期权会按美股 100 计",
            }
        except Exception as e:
            checks["asset_mapping"] = {"status": "error",
                                      "error": f"{type(e).__name__}: {e}"}
    else:
        checks["asset_mapping"] = {"status": "error", "error": "链上无可用 put 合约样本"}

    # ③ 期权子账号（只读：配置 + 余额）
    try:
        checks["account"] = {
            "status": "ok",
            "config": oot.account_config(account),
            "balance": oot.account_balance(account),
        }
    except Exception as e:
        checks["account"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # ④ 当前期权持仓（只读）
    try:
        positions = oot.open_puts(account)
        checks["positions"] = {"status": "ok", "count": len(positions),
                               "rows": positions}
    except Exception as e:
        checks["positions"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    failed = [k for k, v in checks.items() if v.get("status") != "ok"]
    return {"status": "ok" if not failed else "partial",
            "failed": failed, "checks": checks}
