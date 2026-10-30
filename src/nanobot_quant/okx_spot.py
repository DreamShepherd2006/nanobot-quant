"""OKX 现货市价单公用原语（§33.52 P2）。

**唯一实现**：期权线补买/出货（``okx_options_trade.spot_cover`` / ``spot_exit``）
与 OKX CEX 现货执行通道（``brokers/okx_cex_broker.OkxCexBroker``）共用同一份
下单 / 查单 / 精度查询逻辑。不允许第二套（用户规则：同一功能一个模式）。

依赖方向单向：``okx_spot → okx_sdk``。上层（``okx_options_trade``、``brokers``）
依赖本模块，本模块不得反向 import 它们（否则循环依赖）。

关键事实（来自 §33.52 / 期权线实证，勿凭直觉改）：

- 现货单 ``tdMode`` 必须是 ``cross``：期权子账号 ``acctLv=3``（跨币种保证金），
  传 ``cash`` 报 51000、留空报 50014（2026-09-07 实证）。
- 2026-09-30 起 Crypto-USD（USDⓈ 计价）现货对下架，改 Crypto-USDC；
  对 ``-USD`` 结尾的 instId 需透传 ``tradeQuoteCcy=USDC``（普通 ``-USDT`` /
  ``-USDC`` 对不传）。``python-okx`` 的 ``set_order`` 未封装该参数，只能
  ``send_request`` 透传。
- 市价单按基础币下单（``tgtCcy="base_ccy"``）时 ``sz`` 即真实下单量，
  台账与成交天然一致；不要用 Gate 那种「quote 金额」语义（服务端按精度截断
  base，实测偏差 0.1–4.7%，曾致 BNB 精度死锁与 ETH slot2 卡仓）。
"""

from __future__ import annotations

import math
import sys
import time

from nanobot_quant import okx_sdk
from nanobot_quant.okx_sdk import OkxSdkError

#: 现货下单保证金模式默认值。
#:
#: **现货交易不需要保证金** ⇒ 一律 ``cash``。历史遗留值曾是 ``cross``（2026-09-07
#: 期权线在 Bot1 上实测「cash → 51000」后定稿），但 Bot1 是**跨币种保证金模式**
#: 子账号的特例；2026-10-30 用户核实子账号真实模式：
#:
#: | 子账号 | 账户模式 | 用途 |
#: |---|---|---|
#: | DreamShepherdbot1 | 跨币种保证金 | 期权线 |
#: | DreamShepherdbot2–5 | **现货模式** | 现货线 |
#:
#: 现货模式账号传 ``cross`` 直接 ``54000 Margin trading is not supported.``。
#: 「按 ``acctLv`` 判断」也不可靠——``acctLv`` 是主账户级属性，对子账号不成立
#: （Bot2 挂在统一账户下、子账号本身却是现货模式）。
#:
#: 所以策略 = **现金优先 + 撞错自适应**：默认 ``cash``；若该账号确实不接受
#: ``cash``（``51000 Parameter tdMode error``），自动切 ``cross`` 重试一次并
#: 记住（见 :func:`market_order`）。两种拒单都是「订单未成交」，重试无资金风险。
SPOT_TDMODE = "cash"

#: api_key → tdMode（撞错后自动切换并记忆，进程内缓存）
_TDMODE_CACHE: dict[str, str] = {}


def fmt_decimal(value) -> str:
    """定点十进制字符串，**禁止科学计数法**。

    OKX 的 ``sz`` 收到 ``5.13e-05`` 直接报 ``51000 Parameter sz error``
    （2026-10-30 实测：BTC 4U 下单量 4/77971.7 = 5.13e-05，三 slot 全失败）。
    精度给到 12 位小数足够覆盖所有现货 lotSz（最小 1e-8）。

    传字符串时**原样透传**（调用方可能已按业务精度格式化，如期权线补买的
    ``"50.00"``——不能因统一格式化把它变成 ``"50"`` 改掉既有行为）。
    """
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return "0"
        if "e" not in s.lower():
            return s
        value = float(s)
    v = num(value)
    if v == 0:
        return "0"
    return f"{v:.12f}".rstrip("0").rstrip(".") or "0"


def td_mode_for(creds: dict) -> str:
    """该账号当前使用的现货 ``tdMode``（默认 ``cash``，撞错后由下单路径改写）。"""
    return _TDMODE_CACHE.get(str(creds.get("api_key") or ""), SPOT_TDMODE)


#: 轮询节奏——与期权补买/出货原实现逐字一致（10 次 × 0.5s），勿独立调整
POLL_TRIES = 10
POLL_INTERVAL_S = 0.5


def num(value) -> float:
    """安全 float：None / 空串 / 非数字 → 0.0。"""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def floor_step(value: float, step: float) -> float:
    """按步进向下取整，带 1e-12 容差消浮点误差（避免 0.3/0.1=2.9999 掉一档）。

    步进非法（None / 0 / 负数）时原值返回——调用方需自行决定是否 fail-closed。
    """
    step = num(step)
    if step <= 0:
        return float(value)
    n = math.floor(float(value) / step + 1e-12)
    return round(n * step, 12)


def instrument_limits(inst_id: str) -> dict:
    """SPOT 合约规格 → ``{"lot_sz", "min_sz", "tick_sz"}``。

    查询失败或未返回规格一律抛 :class:`OkxSdkError` —— 调用方 fail-closed，
    不允许静默当 1 张处理（会下出超量订单）。
    """
    rows = okx_sdk.check(okx_sdk.public().get_instruments(instType="SPOT", instId=inst_id))
    row = rows[0] if isinstance(rows, list) and rows else {}
    if not row:
        raise OkxSdkError(f"OKX 未返回 {inst_id} 的现货合约规格（51001?）")
    return {
        "lot_sz": num(row.get("lotSz")),
        "min_sz": num(row.get("minSz")),
        "tick_sz": num(row.get("tickSz")),
    }


def market_order(
    creds: dict,
    *,
    inst_id: str,
    side: str,
    sz,
    tgt_ccy: str = "base_ccy",
    tag: str = "",
    td_mode: str | None = None,
) -> dict:
    """现货市价单 → ``{"ord_id", "cl_ord_id", "s_code", "s_msg", "rejected"}``。

    ``tgt_ccy="base_ccy"`` 时 ``sz`` 是基础币数量，``"quote_ccy"`` 时是计价币金额。

    **拒单不抛异常**：OKX 的 ``sCode`` 非 ``0`` 时返回 ``ord_id=""`` + ``s_code``，
    由调用方决定语义（期权线保留原行为：不落台账 failed；broker 用 :func:`strict_order`
    则 fail-closed 抛 :class:`OkxSdkError`）。传输层异常（网络 / 签名 / HTTP）仍原样抛出。
    """
    params = {
        "instId": inst_id,
        "tdMode": td_mode or td_mode_for(creds),
        "side": side,
        "ordType": "market",
        "sz": fmt_decimal(sz),
        "tgtCcy": tgt_ccy,
    }
    # OKX 的 tag 只接受 1–16 位字母数字：空串 / 含 `-`、`_` 都会 51000
    # Parameter tag error。不传该字段 = 不设标签，才是"没标签"的正确表达。
    if tag:
        params["tag"] = tag
    api = okx_sdk.trade_for(creds)

    def _send(p: dict):
        if inst_id.upper().endswith("-USD"):
            # Crypto-USD（统一 USD 订单簿）：用 USDC 成交须显式声明
            p["tradeQuoteCcy"] = "USDC"
            return okx_sdk.check(api.send_request("/api/v5/trade/order", "POST", **p))
        return okx_sdk.check(api.set_order(**p))

    try:
        data = _send(params)
    except OkxSdkError as e:
        # 54000 = 该账号不支持保证金交易（acctLv 查询不准、或子账号被限）。
        # 54000 是明确拒单（订单未成交），降级 cash 重试一次安全——
        # 2026-10-30 实测：Bot2/3 按 acctLv 选了 cross 仍报 54000，
        # 降级后同类报错不再阻断现货买卖。
        msg = str(e)
        cur = params.get("tdMode")
        alt = ""
        if "54000" in msg and cur != "cash":
            alt = "cash"          # 该账号不支持保证金交易
        elif "51000" in msg and "tdMode" in msg and cur != "cross":
            alt = "cross"         # 该账户模式不接受 cash（跨币种保证金子账号）
        if not alt:
            raise
        print(f"[DIAG] OKX tdMode {cur} → {alt} 重试 | {inst_id} | {e}",
              file=sys.stderr, flush=True)
        _TDMODE_CACHE[str(creds.get("api_key") or "")] = alt
        params["tdMode"] = alt
        data = _send(params)
    row = data[0] if isinstance(data, list) and data else {}
    s_code = row.get("sCode")
    rejected = s_code not in (None, "", "0")
    return {
        "ord_id": "" if rejected else (row.get("ordId") or ""),
        "cl_ord_id": row.get("clOrdId") or "",
        "s_code": "" if not rejected else str(s_code),
        "s_msg": (row.get("sMsg") or ""),
        "rejected": rejected,
    }


def strict_order(creds: dict, **kwargs) -> dict:
    """:func:`market_order` 的 fail-closed 包装：拒单 / 无 ``ordId`` 一律抛异常。

    broker 用它：下单要么拿到 ``ord_id``、要么明确报错，不存在「静默未下」的中间态。
    """
    req = market_order(creds, **kwargs)
    if req.get("rejected"):
        raise OkxSdkError(f"OKX {req.get('s_code')} {req.get('s_msg', '')}".strip())
    if not req.get("ord_id"):
        raise OkxSdkError(f"OKX 下单未返回 ordId（{kwargs.get('inst_id', '')}）")
    return req


def query_order(creds: dict, inst_id: str, ord_id: str) -> dict:
    """单次查单 → ``{state, status, ord_id, avg_px, acc_fill_sz, fee, fee_ccy}``。

    ``status`` 归一为 ``pending`` / ``filled`` / ``cancelled`` / 原始 state /
    ``unknown``（空响应）。
    """
    rows = okx_sdk.check(okx_sdk.trade_for(creds).get_order(instId=inst_id, ordId=ord_id))
    row = rows[0] if isinstance(rows, list) and rows else {}
    state = row.get("state", "")
    status = {
        "live": "pending",
        "partially_filled": "pending",
        "filled": "filled",
        "canceled": "cancelled",
        "mmp_canceled": "cancelled",
    }.get(state, state or "unknown")
    return {
        "state": state,
        "status": status,
        "ord_id": ord_id,
        "avg_px": num(row.get("avgPx")),
        "acc_fill_sz": num(row.get("accFillSz")),
        "fee": num(row.get("fee")),
        "fee_ccy": (row.get("feeCcy") or ""),
    }


def poll_fill(
    creds: dict,
    inst_id: str,
    ord_id: str,
    *,
    tries: int = POLL_TRIES,
    interval: float = POLL_INTERVAL_S,
) -> dict:
    """轮询到终态（``filled`` / ``cancelled``）或超时。

    返回最后一次 :func:`query_order` 结果，附加 ``timed_out``（bool）与
    ``tries_used``（int）。**超时不等于失败**——调度方按 pending 处理（fail-safe：
    台账保持 open，下一轮复检），不得据此判成交或判失败。
    """
    attempts = max(1, int(tries))
    result: dict = {}
    for i in range(attempts):
        result = query_order(creds, inst_id, ord_id)
        if result.get("status") in ("filled", "cancelled"):
            result["timed_out"] = False
            result["tries_used"] = i + 1
            return result
        if i + 1 < attempts:
            time.sleep(interval)
    result.setdefault("ord_id", ord_id)
    result["timed_out"] = True
    result["tries_used"] = attempts
    return result
