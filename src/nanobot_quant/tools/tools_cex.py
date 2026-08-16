"""cex_sub_order: 子账号真实下单验证工具（官方 gate-api SDK）。

用指定子账号（gate_bot1..5）自身的 API Key/Secret 在 Gate spot 下市价单，
用于验证子账号交易链路（P3 TD 分批下单的前置验证），也可作手动子账号
下单/对账入口。

Gate 无「主账号代子账号下单」API，子账号订单必须子账号自己的 key 签名。

市价单参数语义（与 CexBroker 一致）：
    side=buy  → amount 为计价币金额（CRCLX_USDT 即 USDT）
    side=sell → amount 为基础币数量（CRCLX）
"""

from __future__ import annotations

import time

from nanobot_quant.gate_credentials import gate_pair, load_gate_credentials


def cex_sub_order(symbol: str, side: str, amount: float, sub_account: str) -> dict:
    """在指定子账号用其自身 key 下 Gate 市价单并轮询至成交/失败。

    Args:
        symbol: 标的符号（如 "CRCLX"，按 tokens.json gate_symbol 映射交易对）。
        side: "buy"（amount=USDT 金额）或 "sell"（amount=基础币数量）。
        amount: 数量/金额，必须 > 0。
        sub_account: 子账号名（"gate_bot1".."gate_bot5"）。

    Returns:
        dict: status=filled 含成交明细（filled_amount/avg_deal_price/fee）；
              status=pending 表示已提交未在 10s 内 closed；
              status=error 含明确原因（配置缺失/参数错误/订单被拒）。
    """
    side = (side or "").strip().lower()
    if side not in ("buy", "sell"):
        return {"status": "error", "error": f"side 必须是 buy/sell，收到: {side!r}"}
    try:
        amount_f = float(amount)
    except (TypeError, ValueError):
        return {"status": "error", "error": f"amount 必须是数字，收到: {amount!r}"}
    if amount_f <= 0:
        return {"status": "error", "error": f"amount 必须 > 0，收到: {amount_f}"}
    if not symbol or not sub_account:
        return {"status": "error", "error": "symbol 和 sub_account 均为必填"}

    creds = load_gate_credentials()
    subs = creds.get("sub_accounts") or {}
    sub = subs.get(sub_account)
    if not sub:
        return {
            "status": "error",
            "error": f"凭证中未找到子账号 {sub_account}（请在凭证页配置其 UID）",
        }
    api_key = (sub.get("api_key") or "").strip()
    api_secret = (sub.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        return {
            "status": "error",
            "error": f"{sub_account} 未配置 API Key/Secret，请在凭证页补录后重试",
        }

    pair = gate_pair(symbol)
    # gate_sdk 在函数内懒加载：其模块级 import gate_api（官方 SDK），仅真实执行
    # 环境安装（Dockerfile pin gate-api==7.2.123）；测试容器由 conftest stub 兜底，
    # 延迟到调用时导入避免 MCP server 启动即依赖 gate_api。
    from nanobot_quant.gate_sdk import create_order, get_order

    order = create_order(
        api_key,
        api_secret,
        pair,
        side,
        str(amount_f),
        time_in_force="ioc",
    )
    order_id = order.get("id")
    if not order_id:
        return {"status": "error", "error": f"下单失败（无订单号）：{order}"}

    # 轮询至 closed（市价单结算异步：下单后立即查询仍 open，~0.5s×10）
    for _ in range(20):
        time.sleep(0.5)
        o = get_order(api_key, api_secret, order_id, pair)
        status = o.get("status")
        if status == "closed":
            return {
                "status": "filled",
                "sub_account": sub_account,
                "symbol": symbol,
                "pair": pair,
                "side": side,
                "order_id": order_id,
                "filled_amount": o.get("filled_amount"),
                "avg_deal_price": o.get("avg_deal_price"),
                "fee": o.get("fee"),
                "finish_as": o.get("finish_as"),
            }
        if status in ("cancelled", "expired", "dead"):
            return {"status": "error", "error": f"订单 {status}：{o}"}
    return {
        "status": "pending",
        "sub_account": sub_account,
        "symbol": symbol,
        "pair": pair,
        "order_id": order_id,
        "note": "订单已提交但 10s 内未 closed，可继续轮询订单状态查询",
    }
