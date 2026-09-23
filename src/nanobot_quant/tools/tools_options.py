"""期权线只读工具（两者都**不下单、不改台账、不动配置**）。

``options_broker_selftest`` —— 期权执行层只读自检（E 期 Step 2a 验证入口）。
一次调用确认四件事，**全部只读**：

1. 期权链（okx_options_data）——家族/现货参考/到期档/可卖合约数
2. Asset ↔ instId 映射 + 每张面值 multiplier
   （multiplier 不符即说明 lumibot fork patch 未生效——期权会按美股 100 计）
3. 期权子账号（账户配置：期权权限/保证金模式/结算币；余额）
4. 当前期权持仓（张数/成本/标记价/保证金）

为什么需要它：Broker/DataSource 是薄适配层，单元测试只能证「转发逻辑」；
「lumibot 期权标的 × OKX 账户」两侧真的接上、multiplier 真的非 100，
只能在容器里实拉一次才算验证（用户原则：功能生效必须有运行时可见证据）。

``analyze_option_spread`` —— 盘口价差画像（C43① 测量前置）：用 tape 采样量出
真实 IV 价差（σ_ask − σ_bid）分布，决定回测的买卖价差该用常数还是分层。
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
        positions = oot.open_option_positions(account)
        checks["positions"] = {"status": "ok", "count": len(positions),
                               "rows": positions}
    except Exception as e:
        checks["positions"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    failed = [k for k, v in checks.items() if v.get("status") != "ok"]
    return {"status": "ok" if not failed else "partial",
            "failed": failed, "checks": checks}


def analyze_option_spread(family: str = "", days: int = 3,
                         min_samples: int = 200,
                         max_rows: int = 40000) -> dict:
    """期权盘口价差画像（只读）：实测 IV 价差（σ_ask − σ_bid）分布 vs 回测现模型。

    回答一个问题：期权回测的买卖价差该用一个常数（方案 A）还是按 delta/到期
    分层（方案 B）—— 先把真实分布量出来再决定。回测现模型是
    ``bid = mark×(1−0.5%)``、``ask = mark×(1+0.5%)``，它其实只等价于 0.1–0.7 个
    IV 点；真实市场报的是 IV 双边（本工具就是去量它）。

    数据来源：本空间 📼 盘口采集落的 ``option_tape/tape_YYYYMMDD.jsonl``
    （一天一文件，需先在期权页开启采集）。只读文件 + 纯计算：不拉网络、不下单。

    Args:
        family: 家族白名单（如 "SOL-USD_UM"），空 = tape 覆盖的全部家族。
        days: 回看天数（1–14），一天一文件；缺文件会在报告里列明。
        min_samples: 覆盖率门；可用样本低于此值 → 报告只摆分布、明确不给结论
            （覆盖率是第一门：「测不出来」≠「没有关系」）。
        max_rows: 报价行上限（默认 40000，0 = 不限）。tape 是每分钟一行/合约的
            快照、每行要做 5 次 BS 反解，不设上限会撞 MCP 的 tool_timeout(60s)；
            超限按天等额 + 固定种子抽样（可复现），覆盖率里如实标注。

    Returns:
        dict: ok / coverage / coverage_ok / overall / by_family / by_delta /
              by_dte / by_family_delta / notes / markdown（可直接粘贴的报告）。
              异常 → status=error + error（不静默失败）。
    """
    try:
        from nanobot_quant.analysis import option_spread as osp

        fams = [family] if family else []
        return osp.summarize(days=days, families=fams,
                             min_samples=min_samples, max_rows=max_rows)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
