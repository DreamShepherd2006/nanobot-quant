"""OKX 期权卖 put 执行层 — 期权线批次 C（U 本位 USDSⓈ-M，官方 python-okx Trade/Account）。

职责：
- 卖 put（开仓，side=sell）+ 买回平仓（side=buy）——OKX ``Trade.set_order``
- 现货补买（到期 ITM 现金结算后，手动闭环持有现货）
- 本地台账（``okx_options_ledger.json``，credential 同目录）：卖出的 put 跨重启
  保留，OKX 仓位在平仓/结算后消失，页面历史与到期监控依赖台账
- 持仓/到期只读查询（Account.get_positions）

口径（批次 B/C 定稿，官方 docs + 实测 2026-09-04）：
- U 本位线性：bidPx/askPx = USD/1 单位名义币，每张面值 lot = ctVal×ctMult
  （BTC 0.01 / ETH 0.01 / SOL 0.1 / XAU 0.01），每张权利金(USD) = px × lot
- 卖 put 逐仓（isolated）模式，每张独立保证金/强平边界（多笔低9 卖 put 同账号
  并存互不拖累）；等效现金担保（自留口径）= Σ(strike × lot × sz)（账户现金，不设杠杆）
- 到期结算：欧式、现金结算（settle = 到期前 30 分钟标的指数算术平均，官方口径；
  到期时刻 08:00 UTC）；
  OKX 到期自动结算入账，本模块不重复算钱，到期后经台账标 ``settled`` 并引导核对账单
"""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import okx_sdk
from .okx_sdk import OkxSdkError

#: OKX 期权保证金模式（2026-09-04 实测网页 + 官方 agent-skills 确认）：
#: 纯买方开仓(cash)付全权利金无杠杆；我们只卖 put（side=sell）= isolated
#: 逐仓——TD 低9 多笔卖 put 在同一子账号并存，逐仓给每张独立保证金与
#: 强平边界（亏穿只平该张，不拖累同账号其他仓位），无需每笔开子账号。
#: 等效现金担保 = 账户自留 ≥ Σ(strike×面值) 现金（不设杠杆，平台不冻结全额）。
SELL_TDMODE = "isolated"
BUY_TDMODE = "cash"
#: 跨币种保证金（acctLv=3 net_mode）下逐仓期权仓的 posSide 标识为 net
POS_SIDE = "net"
#: 平仓/减仓单的 tdMode 必须与持仓保证金模式一致（OKX 规则：不匹配 →
#: 51000 Parameter tdMode error）。本仓开在 isolated（SELL_TDMODE），
#: 买回平仓同样 isolated；cash 仅用于无平仓对象的纯买入开仓。
CLOSE_TDMODE = "isolated"

#: OKX 期权无市价单（官方 docs："For OPTION, market order is not supported yet"——
#: 成交价无法预知、保证金无法预冻结；实测 isolated buy market 亦 50016）。
#: px 必须客户端自定价——建议价规则（页面预填与自动化共用同一规则）：
#: 卖 put（开仓）：IOC px = bid × 0.5 保护线——正常盘口 px<bid 吃单成交在最优买价，
#:   盘口闪崩至 bid×0.5 以下自动拒单（防贱卖）；低比例非目标价而是「最差接受线」。
#: 买回（平仓）：IOC px = ask——精确吃当前卖一；ask 波动升高自动撤、下轮重试不追价。
SELL_PX_BID_RATIO = 0.5

#: 定价保护线（C22b，2026-09-15）：px = N 张盘口模拟均价 × (1 ∓ 容忍滑点%)
#:   卖出方向（卖 put / 卖 call 开仓，吃买盘）：均价 × (1 − tol)
#:   买入方向（买回平仓，吃卖盘）：均价 × (1 + tol)
#: 盘口深度不可用（sim 为 None）时回退旧规则（bid×0.5 / ask）——fail-safe 不变。
#: 与旧规则的区别：旧规则是固定 −50% 的极宽闸门（几乎不触发），新规则以「N 张
#: 实际可成交均价」为基准、容忍度可配，盘口在提交瞬间崩坏时更早拒单。
DEFAULT_PX_TOLERANCE_PCT = 5.0
PX_TOLERANCE_MIN, PX_TOLERANCE_MAX = 0.0, 50.0


def px_tolerance_pct() -> float:
    """定价保护线容忍滑点（%）：卖出方向均价×(1−tol)，买入方向×(1+tol)。"""
    try:
        v = float(load_option_params().get(
            "px_tolerance_pct", DEFAULT_PX_TOLERANCE_PCT))
    except (TypeError, ValueError):
        return DEFAULT_PX_TOLERANCE_PCT
    return min(max(v, PX_TOLERANCE_MIN), PX_TOLERANCE_MAX)


def suggest_px_from_sim(sim: Optional[dict], side: str,
                        tolerance_pct: Optional[float] = None) -> Optional[float]:
    """由盘口吃单模拟推价格保护线（C22b）。

    sim 为 ``simulate_fill()`` 的结果；``avg_px`` 缺失/非正或 sim 为 None →
    返回 None（调用方回退旧规则）。IOC 语义下 px 是「最差接受线」而非目标价：
    正常盘口仍成交在最优档，仅当盘口崩至该线以下（卖出）/ 跳高至该线以上
    （买入）时自动拒单。
    """
    if not sim or not isinstance(sim, dict):
        return None
    try:
        avg = float(sim.get("avg_px"))
    except (TypeError, ValueError):
        return None
    if avg <= 0:
        return None
    tol = px_tolerance_pct() if tolerance_pct is None else float(tolerance_pct)
    tol = min(max(tol, PX_TOLERANCE_MIN), PX_TOLERANCE_MAX)
    mult = (1.0 - tol / 100.0) if (side or "sell").lower() == "sell" \
        else (1.0 + tol / 100.0)
    px = round(avg * mult, 2)
    return px if px > 0 else None


def suggest_px_for_order(inst_id: str, side: str, sz: int = 1) -> dict:
    """下单页预填用的保护线（含口径说明）；前端与自动化共用同一规则。

    返回 ``{ok, px, basis, ref}``；盘口不可用时 ``px=None`` +
    ``basis.mode="none"``（调用方提示手动填价，不阻塞）。
    """
    inst_id = (inst_id or "").strip().upper()
    side = (side or "sell").lower()
    if side not in ("sell", "buy"):
        raise OkxSdkError(f"side 仅支持 sell/buy，收到 {side}")
    sz = max(1, int(sz or 1))
    q = ticker_quote(inst_id)
    try:
        sim = simulate_fill(inst_id, side, sz)
    except (OkxSdkError, RuntimeError, ValueError):
        sim = None
    tol = px_tolerance_pct()
    px = suggest_px_from_sim(sim, side, tol)
    if px is not None:
        basis = {"mode": "sim_avg", "tolerance_pct": tol,
                 "sim_avg_px": sim.get("avg_px"), "sz": sz, "side": side}
    else:
        px = (suggest_sell_px(q["bid"]) if side == "sell"
              else suggest_close_px(q["ask"]))
        basis = {"mode": "fallback", "tolerance_pct": tol,
                 "fallback": "bid×0.5" if side == "sell" else "ask",
                 "sim_avg_px": None, "sz": sz, "side": side}
        if px is None:
            basis["mode"] = "none"
    return {"ok": True, "px": px, "inst_id": inst_id, "basis": basis,
            "ref": {"bid": q["bid"], "ask": q["ask"], "last": q["last"]}}


def suggest_sell_px(bid: Optional[float], sim: Optional[dict] = None,
                    tolerance_pct: Optional[float] = None) -> Optional[float]:
    """卖 put/call 开仓建议 px（保护线）。

    优先：N 张盘口模拟均价 × (1 − 容忍滑点%)（C22b）；盘口深度不可用时回退旧规则
    bid×0.5。px 只是保护线不是目标价：正常盘口 IOC 扫单仍按最优买价（bid）成交，
    仅当盘口崩至保护线以下时自动撤单。bid 缺失/非正且无 sim → None
    （自动化调用方须 fail-closed 不下单）。
    """
    px = suggest_px_from_sim(sim, "sell", tolerance_pct)
    if px is not None:
        return px
    if not bid or bid <= 0:
        return None
    return round(bid * SELL_PX_BID_RATIO, 2)


def suggest_close_px(ask: Optional[float], sim: Optional[dict] = None,
                     tolerance_pct: Optional[float] = None) -> Optional[float]:
    """买回平仓建议 px（最高接受价）。

    优先：N 张盘口模拟均价 × (1 + 容忍滑点%)（C22b）；无盘口深度时回退当前 ask
    （精确吃卖一，不留缓冲）。ask 缺失/非正且无 sim → None。
    """
    px = suggest_px_from_sim(sim, "buy", tolerance_pct)
    if px is not None:
        return px
    if not ask or ask <= 0:
        return None
    return round(ask, 2)
#: 订单来源标签（OKX tag：纯字母数字、<=16 位）
TAG_OPEN = "nbputo1"
TAG_CLOSE = "nbputc1"
TAG_COVER = "nbcov1"
TAG_EXIT = "nbexit1"

_LEDGER_NAME = "okx_options_ledger.json"
_TTL = 30.0  # 两步确认一次性 tx_id 有效期（秒）


# ── 台账（持久化）──────────────────────────────────────────────

def _storage_dir() -> Path:
    from .credential_registry import _get_storage_dir

    return Path(_get_storage_dir())


def ledger_path() -> Path:
    return _storage_dir() / _LEDGER_NAME


def load_ledger() -> list[dict]:
    p = ledger_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_ledger(entries: list[dict]) -> None:
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)


# ── 期权参数（WebUI 担保设置，独立于现货 exec_params）───────────

_PARAMS_NAME = "okx_options_params.json"
DEFAULT_COLLATERAL_RATIO_PCT = 100


def params_path() -> Path:
    return _storage_dir() / _PARAMS_NAME


def load_option_params() -> dict:
    p = params_path()
    if not p.exists():
        return {"collateral_ratio_pct": DEFAULT_COLLATERAL_RATIO_PCT}
    try:
        d = json.loads(p.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_option_params(**fields) -> dict:
    d = load_option_params()
    d.update(fields)
    ratio = d.get("collateral_ratio_pct", DEFAULT_COLLATERAL_RATIO_PCT)
    try:
        ratio = int(ratio)
    except (TypeError, ValueError):
        ratio = DEFAULT_COLLATERAL_RATIO_PCT
    d["collateral_ratio_pct"] = min(max(ratio, 0), 200)
    try:
        tol = float(d.get("px_tolerance_pct", DEFAULT_PX_TOLERANCE_PCT))
    except (TypeError, ValueError):
        tol = DEFAULT_PX_TOLERANCE_PCT
    d["px_tolerance_pct"] = min(max(tol, PX_TOLERANCE_MIN), PX_TOLERANCE_MAX)
    p = params_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)
    return d


def collateral_ratio_pct() -> int:
    """逐仓自动追加担保比例：全损(strike×面值×张数)的百分比，0 = 关闭自动追加。"""
    return load_option_params().get(
        "collateral_ratio_pct", DEFAULT_COLLATERAL_RATIO_PCT)


def _new_id() -> str:
    return f"{int(time.time()*1000):x}{secrets.token_hex(3)}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_ledger(**fields) -> dict:
    entry = {"id": _new_id(), "ts": _utc_now(), "status": "pending", **fields}
    entries = load_ledger()
    entries.append(entry)
    save_ledger(entries)
    return entry


def update_ledger(pred: Callable[[dict], bool], **fields) -> Optional[dict]:
    entries = load_ledger()
    hit = None
    for e in entries:
        if pred(e):
            e.update(fields)
            hit = e
            break
    if hit is not None:
        save_ledger(entries)
    return hit


def find_entry(pred: Callable[[dict], bool]) -> Optional[dict]:
    for e in reversed(load_ledger()):
        if pred(e):
            return e
    return None


# ── instrument / 盘口辅助 ──────────────────────────────────────

def inst_family_of(inst_id: str) -> str:
    """从 instId 解析 instFamily（如 SOL-USD_UM-260905-101-P → SOL-USD_UM）。

    OKX OPTION instId = {instFamily}-{yyMMdd}-{strike}-{C/P}；instFamily
    本身含连字符（SOL-USD_UM），故从尾部日期段反向切分。
    """
    m = re.search(r"-\d{6}-\d+(?:\.\d+)?-[CP]$", inst_id or "")
    return inst_id[: m.start()] if m else (inst_id or "")


def resolve_instrument(inst_id: str) -> dict:
    """单只期权合约规格（instId → stk/expTime/ctVal/ctMult/lot/optType/uly）。"""
    fam = inst_family_of(inst_id)
    # OPTION 查询必须带 instFamily/uly（官方 50015 约束），即使给了 instId
    rows = okx_sdk.check(okx_sdk.public().get_instruments(
        instType="OPTION", instFamily=fam, instId=inst_id))
    if not rows:
        raise OkxSdkError(f"OKX 查无期权合约 {inst_id}")
    r = rows[0]
    lot = _f(r.get("ctVal")) * _f(r.get("ctMult"))
    return {
        "inst_id": inst_id,
        "inst_family": r.get("instFamily", ""),
        "opt_type": r.get("optType", ""),
        "strike": _f(r.get("stk")),
        "exp_ms": int(r.get("expTime") or 0),
        "lot": lot or 0.0,
        "uly": r.get("uly", ""),
        "state": r.get("state", ""),
    }


# U 本位线性期权家族固定面值（ctVal=1 × ctMult，官方规格；SOL 0.1 币/张，其余 0.01）。
# 供到期补买预填等场景：合约到期后 OKX instruments 不再返回规格，面值须本地解析。
FAMILY_LOT = {"BTC": 0.01, "ETH": 0.01, "SOL": 0.1, "XAU": 0.01}

# ── 期权盘口价差模型（C43-① 方案 A：家族常数 Δσ + px tick 地板）────────
# Δσ = 盘口买卖 IV 价差（单位：IV 点，1 点 = 1%）。用途：把「中价 IV」还原成
# 可成交的 bid/ask —— 回测原先用「mark × (1 ∓ 滑点%)」代理价差，实测其等价
# 量仅 0.34~0.54 IV 点，比真实盘口窄一到两个数量级（SOL 差～21 倍）。
# 取值 = 生产 tape（2026-09-24/25，可用样本 31032）**卖出带 |Δ| 0.15–0.50
# 两桶中位的均值**：SOL 11.79/12.62→12.2、XAU 4.58/4.82→4.7、
# BTC 1.57/0.91→1.24、ETH 1.59/1.01→1.30。家族间差 ~10×（SOL vs BTC），
# 故必须按家族取值、不能全局一个常数。
# 详见 docs/quant-system.md §33.41 与 skill okx-options-historical-data §3。
FAMILY_DSIGMA_PTS = {"SOL": 12.2, "XAU": 4.7, "BTC": 1.24, "ETH": 1.30}
DEFAULT_DSIGMA_PTS = 2.0     # 未实测家族的兜底（FAMILIES 四个均已实测）

# 报价最小变动（px tick）—— 价差地板：模型价差 < 1 tick 时按 1 tick 展开。
# SOL/BTC/XAU 来自实盘下单实测（SOL 0.01、BTC 1、XAU 0.1）；ETH 未实测，
# 暂取 0.05（偏保守：tick 越大地板越宽），对 ETH 影响可忽略（其 Δσ 1.3 点
# 已对应远超 1 tick 的价差）。
FAMILY_TICK = {"SOL": 0.01, "BTC": 1.0, "XAU": 0.1, "ETH": 0.05}
DEFAULT_TICK = 0.01


def family_of(inst_id: str) -> str:
    """instId / 家族名 → 基础币（``SOL-USD_UM-260927-124-C`` → ``SOL``）。"""
    return (inst_id or "").split("-")[0].upper()


def family_dsigma_pts(inst_id: str) -> float:
    """该家族的 Δσ（IV 点）。"""
    return float(FAMILY_DSIGMA_PTS.get(family_of(inst_id), DEFAULT_DSIGMA_PTS))


def family_tick(inst_id: str) -> float:
    """该家族的报价最小变动（px tick）。"""
    return float(FAMILY_TICK.get(family_of(inst_id), DEFAULT_TICK))

#: OKX 期权吃单手续费率（按「名义价值 = strike × 面值 × 张数」计）——
#: 2026-09-14 实盘账单反推：call 104-C 1 张 fee 0.00309 USDC，
#: 名义 104×0.1 = 10.4 → 0.0297% ≈ 0.03%（低于 OKX 最低手续费时按最低收，
#: 故薄权利金合约的实际费率占比会显著更高）。仅用于预览估算；实盘
#: 实际手续费以订单详情 fee 字段为准并记入台账。
OPTION_FEE_RATE_TAKER = 0.0003

#: 手续费上限系数（OKX 官方规则）：实收 = Min(费率 × 名义价值, cap × 权利金)。
#: 2026-09-19 实盘 94-P 验证（普通用户 0.03%）：名义费 0.00315 > 7%×权利金
#: 0.00175，账单实收恰为 cap 值 —— **薄权利金合约受此保护，权利金越薄越明显**
#: （px 0.25 时领先 1.6 倍、px 0.01 时领先 40 倍）。
#: 触发线：px < strike / (费率/cap) —— 吃单 0.03% 时为 strike / 233。
OPTION_FEE_CAP_RATIO = 0.07


def option_fee_est(strike: float, lot: float, sz: int,
                   premium_px: float | None = None) -> float:
    """期权吃单手续费预估（USD）= Min(名义价值 × 吃单费率, cap × 权利金)。

    ``premium_px``（每名义币的权利金，与 strike 同尺度）为 None 时退化为纯名义
    口径（向后兼容）；调用方拿得到权利金时**务必传入**，否则薄权利金合约的
    手续费会被高估（实测最高 40 倍），进而低估其净收益率、埋没优质的薄权利金档。
    """
    try:
        fee = float(strike) * float(lot) * int(sz) * OPTION_FEE_RATE_TAKER
        if premium_px is not None:
            premium = abs(float(premium_px)) * float(lot) * int(sz)
            fee = min(fee, OPTION_FEE_CAP_RATIO * premium)
        return max(0.0, fee)
    except (TypeError, ValueError):
        return 0.0


def _floor_step(value: float, step: float) -> float:
    """按交易对步进向下取整（浮点安全）；step<=0 原值返回。"""
    try:
        v = float(value or 0.0)
        s = float(step or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if s > 0:
        return round(max(0.0, math.floor(v / s + 1e-9) * s), 10)
    return round(max(0.0, v), 10)


def spot_limits(account: str = "", spot_inst: str = "",
                px: Optional[float] = None) -> dict:
    """现货出货/补买上下文（只读）：可用余额 + 交易对精度 → 可卖/可买量。

    - avail：基础币可用余额（出货的可卖上限来源）
    - quote_avail：计价币（USDC）可用（补买的金额上限来源）
    - lot_sz/min_sz：OKX public instruments 的数量步进/最小下单量
    - sellable：按 lot_sz 向下取整后的最大可卖量（≤ avail）
    - buyable_qty：quote_avail ÷ px 再按 lot_sz 向下取整（px 缺失 → None）

    取数失败不抛错——各项 0/None + err 说明，页面照常可下单（超量由
    交易所拒单裁决，2026-09-14 用户拍板：不做前端硬拦截）。
    """
    base = (spot_inst or "").split("-")[0]
    quote = "USDC"   # 统一 USD 订单簿以 USDC 结算（Crypto-USDC 对 2026-09-23 上架）
    out = {"spot_inst": spot_inst, "base": base, "quote": quote,
           "avail": 0.0, "quote_avail": 0.0, "lot_sz": 0.0, "min_sz": 0.0,
           "sellable": 0.0, "buyable_qty": None, "err": ""}
    if not base or not spot_inst:
        out["err"] = "缺少现货对"
        return out
    try:
        rows = okx_sdk.check(okx_sdk.market().get_instruments(
            instType="SPOT", instId=spot_inst))
        r = rows[0] if isinstance(rows, list) and rows else {}
        out["lot_sz"] = _f(r.get("lotSz"))
        out["min_sz"] = _f(r.get("minSz"))
    except (okx_sdk.OkxSdkError, RuntimeError) as e:
        out["err"] = f"交易对精度查询失败：{e}"
    try:
        bal = account_balance(account)
        for d in bal.get("details") or []:
            if d.get("ccy") == base:
                out["avail"] = _f(d.get("avail_bal"))
            elif d.get("ccy") == quote:
                out["quote_avail"] = _f(d.get("avail_bal"))
    except (okx_sdk.OkxSdkError, RuntimeError) as e:
        out["err"] = ((out["err"] + "；") if out["err"] else "") + f"余额查询失败：{e}"
    out["sellable"] = _floor_step(out["avail"], out["lot_sz"])
    try:
        pxv = float(px or 0.0)
    except (TypeError, ValueError):
        pxv = 0.0
    if pxv > 0 and out["quote_avail"] > 0:
        out["buyable_qty"] = _floor_step(out["quote_avail"] / pxv, out["lot_sz"])
    return out


def cover_prefill_defaults(inst_id: str, sz: int, spot_px: float | None = None,
                           entry: Optional[dict] = None,
                           limits: Optional[dict] = None) -> dict:
    """到期 ITM 补买预填：数量 = 面值(lot)×张数；价格默认 = 现货现价
    （fallback：结算价 → 行权价，全部可在页面修改）。

    limits（spot_limits 结果）提供时附账户可用（USDC）与可买量，供页面提示。
    """
    lot = FAMILY_LOT.get((inst_id or "").split("-")[0], 0.0)
    if not lot:
        try:
            inst = resolve_instrument(inst_id)
            lot = float(inst.get("lot") or 0.0)
        except Exception:
            lot = 0.0
    px, src = spot_px, "spot"
    if not px and entry:
        if entry.get("settle_px"):
            px, src = entry.get("settle_px"), "settle"
        elif entry.get("strike"):
            px, src = entry.get("strike"), "strike"
    out = {"qty": round(lot * sz, 6) if lot else 0.0,
           "px": px, "px_src": src if px else None}
    if limits:
        out["quote_avail"] = round(limits.get("quote_avail") or 0.0, 6)
        out["lot_sz"] = limits.get("lot_sz") or 0.0
        out["min_sz"] = limits.get("min_sz") or 0.0
        out["buyable_qty"] = limits.get("buyable_qty")
        out["limits_err"] = limits.get("err") or ""
    return out


def ticker_quote(inst_id: str) -> dict:
    """当前盘口/最新（bidPx/askPx/last —— USD/1 名义币）。"""
    rows = okx_sdk.check(okx_sdk.market().get_ticker(instId=inst_id))
    r = rows[0] if rows else {}
    return {
        "bid": _f(r.get("bidPx")),
        "ask": _f(r.get("askPx")),
        "last": _f(r.get("last")),
    }


def order_book(inst_id: str, levels: int = 5) -> dict:
    """多档盘口（USD/1 名义币；size 单位=张；bids 降序 / asks 升序）。

    C22a 盘口深度模拟数据源：卖 put 吃买盘(bids)、买回平仓吃卖盘(asks)。
    期权 books 公共端点与现货同构——level = [price, size, ...]。
    """
    rows = okx_sdk.check(okx_sdk.market().get_books(instId=inst_id, sz=str(levels)))
    book = rows[0] if rows else {}
    bids, asks = [], []
    for lv in book.get("bids") or []:
        if len(lv) >= 2:
            px, amt = _f(lv[0]), _f(lv[1])
            if px and amt and px > 0 and amt > 0:
                bids.append([px, amt])
    for lv in book.get("asks") or []:
        if len(lv) >= 2:
            px, amt = _f(lv[0]), _f(lv[1])
            if px and amt and px > 0 and amt > 0:
                asks.append([px, amt])
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    return {"bids": bids, "asks": asks, "ts": book.get("ts")}


def simulate_fill(inst_id: str, side: str, sz_qty: int, levels: int = 5) -> Optional[dict]:
    """盘口吃单模拟（C22a 报价模拟；与 IOC/限价扫单同向吃档）。

    side="sell"（卖 put 开仓）→ 吃买盘 bids（最优价起向下）；
    side="buy"（买回平仓）→ 吃卖盘 asks（最优价起向上）。
    按 sz_qty 张逐档累计，输出吃穿档数 / 加权均价 / 相对最优价折让%
    与权利金滑点 USD——回答「现在按盘口市价这单会以什么价成交」。

    盘口不可用 / 无对手档 → 返回 None（调用方容错，不阻塞下单）。
    盘口总量 < sz_qty → full=False + missing（IOC 部分成交后撤余量）。
    """
    side = (side or "sell").lower()
    if side not in ("sell", "buy"):
        raise OkxSdkError(f"side 仅支持 sell/buy，收到 {side}")
    want = float(int(sz_qty) or 0)
    if want <= 0:
        raise OkxSdkError("张数必须为正整数")
    try:
        book = order_book(inst_id, levels)
    except OkxSdkError:
        return None
    legs = book["bids"] if side == "sell" else book["asks"]
    if not legs:
        return None
    lot = resolve_instrument(inst_id)["lot"]
    best = legs[0][0]
    got, cost, used = 0.0, 0.0, 0
    for px, amt in legs:
        if want <= 0:
            break
        take = min(want, amt)
        got += take
        cost += take * px
        want -= take
        used += 1
    avg = cost / got if got > 0 else None
    dip = (best - avg) / best * 100.0 if avg else None
    return {
        "ok": True, "side": side, "qty": int(sz_qty), "best_px": best,
        "avg_px": round(avg, 4) if avg is not None else None,
        "levels_used": used, "filled": round(got, 4), "full": want <= 0,
        "missing": round(want, 4) if want > 0 else 0,
        "dip_pct": round(dip, 3) if dip is not None else None,
        "slip_usd": round((best - avg) * lot * got, 6) if avg else None,
    }


def preview_open_put(inst_id: str, sz: int, ord_type: str = "limit",
                     px: Optional[float] = None) -> dict:
    """卖 put 订单预览（纯计算，不下单）。

    输出含：合约规格、参考盘口、订单参数（side=sell tdMode=isolated 逐仓）、
    预计权利金 = px×lot×sz（limit）或 ask×lot×sz（market 参考）、
    等效现金担保（自留口径 Σ strike×面值）与提示。
    """
    spec = resolve_instrument(inst_id)
    if spec["opt_type"] != "P":
        raise OkxSdkError(f"{inst_id} 不是 Put 合约（{spec['opt_type']}）")
    lot = spec["lot"]
    if lot <= 0:
        raise OkxSdkError(f"{inst_id} 面值解析失败")
    q = ticker_quote(inst_id)
    ord_type = (ord_type or "limit").lower()
    if ord_type not in ("limit", "post_only", "fok", "ioc"):
        raise OkxSdkError(
            f"不支持的订单类型 {ord_type}：OKX 期权不支持市价单（50016），"
            "仅限价/IOC/FOK/post_only（需带价格）")
    if px is None:
        raise OkxSdkError("期权限价类订单需提供价格 px")
    ref_px = px
    try:
        sim = simulate_fill(inst_id, "sell", int(sz))
    except (OkxSdkError, RuntimeError, ValueError):
        sim = None
    tol = px_tolerance_pct()
    suggested_px = suggest_sell_px(q["bid"], sim=sim, tolerance_pct=tol)
    # 预期成交价（口径）：盘口模拟均价优先（IOC 吃买盘实际能拿到的价）→ 回退下单价
    try:
        fill_px = float(sim["avg_px"]) if (sim and sim.get("avg_px")) else ref_px
    except (TypeError, ValueError):
        fill_px = ref_px
    prem = fill_px * lot * sz
    collat = spec["strike"] * lot * sz
    fee_est = option_fee_est(spec["strike"], lot, sz, premium_px=fill_px)
    ratio = collateral_ratio_pct()
    exp_iso = _exp_str(spec["exp_ms"])
    return {
        "ok": True,
        "inst_id": inst_id,
        "opt_type": "P",
        "strike": spec["strike"],
        "exp_ms": spec["exp_ms"],
        "exp_date": exp_iso,
        "lot": lot,
        "family": spec["inst_family"],
        "sz": int(sz),
        "ord_type": ord_type,
        "px": ref_px,
        "suggested_px": suggested_px,
        "px_basis": {"mode": ("sim_avg" if (sim and sim.get("avg_px"))
                                else "fallback_bid_ratio"),
                     "tolerance_pct": tol,
                     "sim_avg_px": (sim.get("avg_px") if sim else None),
                     "fallback": "bid×0.5"},
        "fill_px_est": round(fill_px, 4),
        "ref": {"bid": q["bid"], "ask": q["ask"], "last": q["last"]},
        "td_mode": SELL_TDMODE,
        "est_premium_usd": round(prem, 4),
        "fee_est_usd": round(fee_est, 4),
        "net_premium_usd": round(prem - fee_est, 4),
        "net_yield_pct": (round((prem - fee_est) / collat * 100, 3)
                          if collat else None),
        "collateral_est_usd": round(collat, 2),
        "collateral_target_usd": round(collat * ratio / 100.0, 2),
        "collateral_ratio_pct": ratio,
        "note": ("U 本位线性：盘口=USD/1 名义币，权利金(USD)=px×每张面值；"
                 "卖方以逐仓(isolated)冻结——每张独立保证金/强平边界，多笔卖 put"
                 "共存互不拖累（TD 低9 分批同账号操作，无需多子账号）；"
                 f"成交后自动把该仓保证金追加至担保目标（全损×{ratio}%≈"
                 f"collateral_target_usd），实现等效现金担保——浮亏上限 < 保证金则"
                 "数学上无强平路径，可扛到到期；0% = 关闭自动追加（仅平台默认 IM"
                 "冻结，浮亏可能击穿提前强平）；追加资金在平仓/到期后自动释放；"
                 "账户可用余额须≥追加额。欧式现金结算，不可提前行权。"),
        "sim": sim,
    }


def preview_open_call(inst_id: str, sz: int, ord_type: str = "limit",
                      px: Optional[float] = None,
                      cost_basis: Optional[float] = None,
                      no_cost_ack: bool = False) -> dict:
    """卖 call（covered call）订单预览（纯计算，不下单）——镜像 preview_open_put。

    输出含：合约规格、参考盘口、订单参数（side=sell tdMode=isolated 逐仓）、
    预计权利金 = px×lot×sz、保本门状态（K_call + px ≥ C，cost_basis 提供时）、
    covered 语义说明（现货在手覆盖上行，不做全损现金担保）。
    """
    spec = resolve_instrument(inst_id)
    if spec["opt_type"] != "C":
        raise OkxSdkError(f"{inst_id} 不是 Call 合约（{spec['opt_type']}）")
    lot = spec["lot"]
    if lot <= 0:
        raise OkxSdkError(f"{inst_id} 面值解析失败")
    q = ticker_quote(inst_id)
    ord_type = (ord_type or "limit").lower()
    if ord_type not in ("limit", "post_only", "fok", "ioc"):
        raise OkxSdkError(
            f"不支持的订单类型 {ord_type}：OKX 期权不支持市价单（50016），"
            "仅限价/IOC/FOK/post_only（需带价格）")
    if px is None:
        raise OkxSdkError("期权限价类订单需提供价格 px")
    ref_px = px
    notional = spec["strike"] * lot * sz
    exp_iso = _exp_str(spec["exp_ms"])
    try:
        sim = simulate_fill(inst_id, "sell", int(sz))
    except (OkxSdkError, RuntimeError, ValueError):
        sim = None
    tol = px_tolerance_pct()
    suggested_px = suggest_sell_px(q["bid"], sim=sim, tolerance_pct=tol)
    try:
        fill_px = float(sim["avg_px"]) if (sim and sim.get("avg_px")) else ref_px
    except (TypeError, ValueError):
        fill_px = ref_px
    prem = fill_px * lot * sz
    fee_est = option_fee_est(spec["strike"], lot, sz, premium_px=fill_px)
    gate = None
    if cost_basis is not None:
        cb = float(cost_basis)
        gate = {"cost_basis": round(cb, 4),
                "guard": round(spec["strike"] + ref_px, 4),
                "ok": (spec["strike"] + ref_px) >= cb - 1e-9}
    return {
        "ok": True,
        "inst_id": inst_id,
        "opt_type": "C",
        "strike": spec["strike"],
        "exp_ms": spec["exp_ms"],
        "exp_date": exp_iso,
        "lot": lot,
        "family": spec["inst_family"],
        "sz": int(sz),
        "ord_type": ord_type,
        "px": ref_px,
        "suggested_px": suggested_px,
        "px_basis": {"mode": ("sim_avg" if (sim and sim.get("avg_px"))
                                else "fallback_bid_ratio"),
                     "tolerance_pct": tol,
                     "sim_avg_px": (sim.get("avg_px") if sim else None),
                     "fallback": "bid×0.5"},
        "fill_px_est": round(fill_px, 4),
        "ref": {"bid": q["bid"], "ask": q["ask"], "last": q["last"]},
        "td_mode": SELL_TDMODE,
        "est_premium_usd": round(prem, 4),
        "fee_est_usd": round(fee_est, 4),
        "net_premium_usd": round(prem - fee_est, 4),
        "notional_usd": round(notional, 2),
        "net_yield_pct": (round((prem - fee_est) / notional * 100, 3)
                          if notional else None),
        "gate": gate,
        "no_cost_ack": bool(no_cost_ack),
        "cost_basis": (round(float(cost_basis), 4) if cost_basis is not None else None),
        "note": ("卖 call（covered call）：持有现货 + 卖虚值/平值 call 收权利金。"
                 "被行权 = 现金结算赔付 (结算价−K)×面值 后现货市价卖出，"
                 "等效按 K 出货（保本门保证 K+权利金 ≥ 被动持仓成本 C）。"
                 "call 上行无界，**不做全损现金担保**——covered 由现货在手实现；"
                 "isolated 仅平台 IM 冻结，暴涨接近强平价时需主动买回平仓。"
                 "欧式现金结算，不可提前行权。"),
        "sim": sim,
    }


def _exp_str(exp_ms: int) -> str:
    try:
        return datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "?"


# ── 下单（真实操作，两步确认由 handler 层执行）──────────────

def _entry_account(account: str) -> dict:
    """account = 子账号 name 或 uid。返回 creds + 匹配条目的 name/uid/label。

    单独再查一次存储以拿到 name/uid 元数据——get_okx_cex_credentials 只返回
    三要素（api_key/secret_key/passphrase），不含账号身份字段。
    """
    from .okx_cex_credentials import (
        get_okx_cex_credentials, load_okx_cex_credentials, normalize_stored,
        _REQUIRED,
    )

    creds = get_okx_cex_credentials(account=account or None)
    name, uid = "", ""
    try:
        stored = normalize_stored(load_okx_cex_credentials() or {})
        subs = stored.get("sub_accounts") or []
        for s in subs:
            hit = (account and (s.get("uid") == account or s.get("name") == account)) or (
                not account and all(s.get(k) for k in _REQUIRED))
            if hit:
                name = s.get("name") or ""
                uid = s.get("uid") or ""
                if account:
                    break
    except Exception:
        pass
    return {"creds": creds, "label": name or account or "",
            "name": name, "uid": uid or account or ""}


def _place(creds: dict, *, inst_id: str, side: str, sz: int,
           ord_type: str, px: Optional[float], tag: str,
           td_mode: Optional[str] = None) -> dict:
    """OKX 下单统一入口。td_mode 缺省按 side 分流：sell（卖 put 开仓）→
    isolated 逐仓；buy → cash（纯买开仓）。平仓单必须显式传 CLOSE_TDMODE
    （跟随持仓保证金模式），cash 平 isolated 仓位会报 51000。"""
    if td_mode is None:
        td_mode = BUY_TDMODE if side == "buy" else SELL_TDMODE
    params = {
        "instId": inst_id,
        "tdMode": td_mode,
        "side": side,
        "ordType": ord_type,
        "sz": str(int(sz)),
        "tag": tag,
    }
    if ord_type not in ("limit", "post_only", "fok", "ioc"):
        raise OkxSdkError(
            f"期权不支持 ordType={ord_type}（OKX 50016，无纯市价单）——"
            "用 limit（px=盘口价立即成交）或 IOC（px=保底价扫单）")
    if px is None:
        raise OkxSdkError("该订单类型需提供 px")
    # 原样透传用户价格（BTC tick=1、SOL/XAU tick=0.1），合法性交给 OKX 校验
    params["px"] = str(px)
    data = okx_sdk.check(okx_sdk.trade_for(creds).set_order(**params))
    row = data[0] if isinstance(data, list) and data else {}
    s_code = row.get("sCode")
    if s_code not in (None, "", "0"):
        raise OkxSdkError(f"OKX {s_code} {row.get('sMsg', '')}".strip())
    return {"ord_id": row.get("ordId") or "", "cl_ord_id": row.get("clOrdId") or ""}


def poll_order(creds: dict, inst_id: str, ord_id: str) -> dict:
    """查询订单状态 → {state, avg_px, acc_fill_sz, fee, status}。"""
    rows = okx_sdk.check(okx_sdk.trade_for(creds).get_order(
        instId=inst_id, ordId=ord_id))
    r = rows[0] if isinstance(rows, list) and rows else {}
    st = r.get("state", "")
    status = {"live": "pending", "partially_filled": "pending",
              "filled": "filled", "canceled": "cancelled",
              "mmp_canceled": "cancelled"}.get(st, st or "unknown")
    return {
        "state": st,
        "status": status,
        "ord_id": ord_id,
        "avg_px": _f(r.get("avgPx")),
        "acc_fill_sz": _f(r.get("accFillSz")),
        "fee": _f(r.get("fee")),
        "fee_ccy": (r.get("feeCcy") or ""),
    }


def fee_usd_fields(fee, side: str, base: str = "", px: float = 0.0,
                   option: bool = False, fee_ccy: str = "") -> dict:
    """手续费归一为 USD 口径（台账展示统一口径）。

    OKX 现货手续费从「所得币」扣：买单扣**基础币**（fee 单位 = base，如补买
    0.1 SOL 的 fee 为 0.0001 SOL，按成交价 104.25 折算 ≈ $0.0104），卖单扣
    **计价币**（USDC）；期权手续费恒以计价币（USDC）计。台账保留交易所原值
    fee，另记 fee_ccy（原始币种）与 fee_usd（按成交价折算）供页面统一按
    USD 展示。

    背景（2026-09-15）：此前只存 fee 数值、不存币种，补买行把 0.0001 SOL
    当成 $0.0001 展示，与真实支出（$0.0104）相差约 100 倍。
    """
    f = _f(fee)
    ccy = (fee_ccy or "").strip().upper()
    if not ccy:
        ccy = (base or "").upper() if (not option and side == "buy") else "USDC"
    usd = f
    if ccy not in ("", "USDC", "USDT"):
        try:
            if px:
                usd = f * float(px)
        except (TypeError, ValueError):
            usd = f
    return {"fee": f, "fee_ccy": ccy, "fee_usd": round(abs(usd), 8)}


def cancel_order(account: str, *, inst_id: str, ord_id: str) -> dict:
    """撤销未成交委托（单步，撤单不产生资金流）。

    先 get_order 查状态：filled → 返回 status=filled（无法撤，提示刷新）；
    已 cancelled → 幂等（顺带把台账条目对齐）。live/partially_filled →
    set_cancel_order 撤销。撤单成功后按 ord_id 把对应台账条目置 cancelled
    （保留记录）。平仓单（close_put）撤销时对应 open_put 行保持 open——
    持仓未动、仍受到期监控。
    """
    a = _entry_account(account)
    creds = a["creds"]
    o = poll_order(creds, inst_id, ord_id)
    if o["status"] == "filled":
        return {"status": "filled", "message": "订单已成交，无需撤销",
                "ord_id": ord_id}
    if o["status"] == "cancelled":
        update_ledger(lambda x: x.get("ord_id") == ord_id, status="cancelled")
        return {"status": "cancelled", "message": "订单已是撤销状态",
                "ord_id": ord_id}
    rows = okx_sdk.check(okx_sdk.trade_for(creds).set_cancel_order(
        instId=inst_id, ordId=ord_id))
    row = rows[0] if isinstance(rows, list) and rows else {}
    s_code = row.get("sCode")
    if s_code not in (None, "", "0"):
        raise OkxSdkError(f"OKX {s_code} {row.get('sMsg', '')}".strip())
    update_ledger(lambda x: x.get("ord_id") == ord_id, status="cancelled",
                  cancel_ts=_utc_now())
    return {"status": "cancelled", "message": "已撤销", "ord_id": ord_id}


def pending_orders(account: str = "", inst_family: str = "") -> list[dict]:
    """子账号当前未成交委托（只读，含官方后台手动挂的单）。

    OKX /trade/orders-pending 要求 instId / instFamily / uly 至少一个——
    本实现按 instFamily 拉取该家族全部未成交委托；family 缺失时显式报错
    （不静默回退）。归一化输出 inst_id/ord_id/px/sz/side/ord_type/ts_ms。
    """
    if not inst_family:
        raise OkxSdkError("pending_orders 需 inst_family（OKX 不支持无 family 全量拉取）")
    a = _entry_account(account)
    rows = okx_sdk.check(okx_sdk.trade_for(a["creds"]).get_orders_pending(
        instType="OPTION", instFamily=inst_family))
    out = []
    for r in rows if isinstance(rows, list) else []:
        out.append({
            "inst_id": r.get("instId", ""),
            "ord_id": r.get("ordId", ""),
            "px": _f(r.get("px")),
            "sz": _f(r.get("sz")),
            "side": r.get("side", ""),
            "ord_type": r.get("ordType", ""),
            "td_mode": r.get("tdMode", ""),
            "ts_ms": int(r.get("cTime") or 0) or None,
        })
    out.sort(key=lambda x: x["ts_ms"] or 0)
    return out


def open_put(account: str, *, inst_id: str, sz: int,
             ord_type: str = "limit", px: Optional[float] = None) -> dict:
    """卖 put 开仓（真实下单）。成功后写入台账（pending → 轮询 filled）。"""
    return _open_option(account=account, inst_id=inst_id, sz=sz,
                        ord_type=ord_type, px=px, kind="open_put")


def open_call(account: str, *, inst_id: str, sz: int,
              ord_type: str = "limit", px: Optional[float] = None,
              cost_basis: Optional[float] = None,
              no_cost_basis_ack: bool = False) -> dict:
    """卖 call（covered call）开仓——镜像 open_put（side=sell、isolated 逐仓）。

    三道入场门（均 fail-closed，在真实下单前判定）：

    ① covered 门（§33.39）：现货覆盖 ≥ 卖出张数才算 covered——判据来自
    covered_context().sellable_sz（现货 ≥ 每张面值×99%）。不足即拒绝，
    绝不裸空 call：covered 语义是「现货在手对冲上行」，裸卖 call 不是本策略，
    且上行无界、只靠逐仓 IM 挡（强平路径）——与「不碰杠杆」铁律冲突。
    该门在后端强制，绕过页面（直调端点）同样拦。

    ② 无成本锚确认门（§33.39 / C46）：保本门依赖成本锚 C（= 接货价）才能判定
    K + px ≥ C。**未提供 C 时后端默认拒绝**（不可静默跳过保本门）；确需放弃保本门
    （如手动补买现货后卖 call、接货价不便填）必须显式确认——传
    no_cost_basis_ack=True（页面勾选「确认无成本锚卖出」），台账会记 no_cost_ack
    标记备查。

    ③ 保本门（§33.23）：提供 C 时要求 strike + px ≥ C（px 为 IOC 保底线 =
    最差接受成交价，门成立则实际成交必成立）；不满足直接拒绝该轮，不硬卖。

    担保差异：call 上行无界，**不做全损现金担保**（_ensure_collateral 仅用于
    卖 put）——covered 语义 = 现货在手，被行权 = 现金结算赔付后现货市价卖出
    （等效按 K 出货）；isolated 仅平台 IM 冻结，极端暴涨接近强平时需主动
    买回平仓或补保证金（页面强平价可见）。
    """
    return _open_option(account=account, inst_id=inst_id, sz=sz,
                        ord_type=ord_type, px=px, kind="open_call",
                        cost_basis=cost_basis,
                        no_cost_basis_ack=no_cost_basis_ack)


def _open_option(account: str, *, inst_id: str, sz: int,
                 ord_type: str = "limit", px: Optional[float] = None,
                 kind: str = "open_put",
                 cost_basis: Optional[float] = None,
                 no_cost_basis_ack: bool = False) -> dict:
    """卖期权开仓公共路径：kind=open_put/open_call 决定合约类型断言与担保行为。"""
    a = _entry_account(account)
    spec = resolve_instrument(inst_id)
    want = "P" if kind == "open_put" else "C"
    if spec["opt_type"] != want:
        raise OkxSdkError(
            f"{inst_id} 不是 {'Put' if want == 'P' else 'Call'} 合约"
            f"（opt_type={spec['opt_type']}，请选 {'P' if want == 'P' else 'C'} 侧合约）")
    if int(sz) <= 0:
        raise OkxSdkError("张数 sz 必须为正整数")
    if kind == "open_call":
        # covered 门（fail-closed，后端强制）：现货不足 = 裸空 call，一律拒绝。
        # 累计口径（2026-09-24 修）：必须减去**已在仓的 short call 张数** —— 否则
        # 现货只够 1 张时分两笔各卖 1 张，两笔都能过（覆盖被重复使用 = 裸空）。
        cov = covered_context(account or a["label"], spec["inst_family"])
        covered_sz = int(cov.get("sellable_sz") or 0)
        inflight = _inflight_call_sz(account or a["label"], spec["inst_family"])
        if covered_sz < inflight + int(sz):
            raise OkxSdkError(
                f"covered 门不通过（fail-closed，该轮跳过）：现货 "
                f"{cov.get('spot_avail')} {cov.get('base')} 覆盖 {covered_sz} 张"
                f" − 在仓 call {inflight} 张 = 可用 {max(covered_sz - inflight, 0)} 张"
                f" < 卖出 {sz} 张（现货覆盖 {cov.get('spot_cov_pct')}%，"
                f"判据 ≥ 每张面值×99%）。现货不足时卖出等于裸空 call"
                f"（上行无界、只靠逐仓 IM 挡），请先补足现货（补买/划入）后再卖 call。")
    if kind == "open_call":
        # ② 无成本锚确认门（fail-closed）：无 C 则保本门不可判定 → 默认拒绝
        if cost_basis is None and not no_cost_basis_ack:
            raise OkxSdkError(
                "保本门缺少成本锚（fail-closed，该轮跳过）：卖 call 必须提供成本锚 C"
                "（=接货价）才能校验 K + px ≥ C。若确认放弃保本门（例如手动补买现货后"
                "卖 call、接货价不便填写），请在页面勾选「确认无成本锚卖出」后重试——"
                "该次卖出会在台账标记「无成本锚」备查。")
        # ③ 保本门：提供 C 时强制 K + px ≥ C
        if cost_basis is not None:
            guard = spec["strike"] + (px or 0.0)
            if guard < float(cost_basis) - 1e-9:
                raise OkxSdkError(
                    f"保本门不通过（fail-closed，该轮跳过）：K({spec['strike']})"
                    f" + px({px or 0}) = {guard:.4f} < 成本锚 C({float(cost_basis):.4f})。"
                    "请抬高行权价、等待权利金回升，或上调成本锚后再卖 call。")
    # 开盘前快照盘口供参考（下单后立即轮询会很快，先落台账 pending）
    q = ticker_quote(inst_id)
    entry = add_ledger(
        kind=kind, account=account or a["label"], inst_id=inst_id,
        strike=spec["strike"], exp_ms=spec["exp_ms"], lot=spec["lot"],
        family=spec["inst_family"], side="sell", ord_type=ord_type,
        px=px,
        sz=int(sz), status="pending", ref_bid=q["bid"], ref_ask=q["ask"],
        **({"no_cost_ack": True} if (kind == "open_call" and cost_basis is None
                                     and no_cost_basis_ack) else {}),
    )
    if kind == "open_call" and cost_basis is not None:
        update_ledger(lambda x: x["id"] == entry["id"], cost_basis=round(float(cost_basis), 6))
        entry["cost_basis"] = round(float(cost_basis), 6)
    try:
        res = _place(a["creds"], inst_id=inst_id, side="sell", sz=int(sz),
                     ord_type=ord_type, px=px, tag=TAG_OPEN)
        ord_id = res["ord_id"]
    except Exception as e:
        update_ledger(lambda x: x["id"] == entry["id"], status="failed",
                      note=f"下单失败: {e}")
        raise
    update_ledger(lambda x: x["id"] == entry["id"], ord_id=ord_id)
    entry["ord_id"] = ord_id
    return _settle_open_entry(a["creds"], entry, kind=kind)


def _settle_open_entry(creds: dict, entry: dict, kind: str = "open_put") -> dict:
    """短轮询（约 10×0.5s）等成交，回填 avg px/权利金/状态。

    filled → open 时：卖 put（kind=open_put）自动触发 _ensure_collateral
    （逐仓保证金追加至全损担保，走 A）；卖 call（open_call）**不做全损担保**
    （上行无界，covered=现货在手），仅记 margin_note。
    """
    if not entry.get("ord_id"):
        return entry
    for _ in range(10):
        o = poll_order(creds, entry["inst_id"], entry["ord_id"])
        if o["status"] == "filled":
            px = o["avg_px"] or entry.get("px") or 0.0
            filled_sz = o["acc_fill_sz"] or entry.get("sz") or 0
            upd = update_ledger(
                lambda x: x["id"] == entry["id"], status="open",
                filled_px=px, filled_sz=filled_sz,
                premium_usd=round(px * entry["lot"] * filled_sz, 4),
                **fee_usd_fields(o.get("fee"), "sell", px=px, option=True,
                                 fee_ccy=o.get("fee_ccy")),
            )
            if upd:
                if kind == "open_put":
                    upd = _ensure_collateral(creds, upd) or upd
                else:
                    upd = update_ledger(
                        lambda x: x["id"] == entry["id"],
                        collateral_usd=None, margin_added=0.0,
                        margin_note="covered（现货在手——call 上行无界不做全损"
                                    "现金担保；isolated 仅平台 IM 冻结，暴涨接近"
                                    "强平价时需主动买回平仓或补保证金）") or upd
            return upd or entry
        if o["status"] == "cancelled":
            return update_ledger(lambda x: x["id"] == entry["id"],
                                 status="cancelled") or entry
        time.sleep(0.5)
    return entry  # 仍 pending，等页面刷新再轮询


def _position_margin(creds: dict, inst_id: str) -> tuple[float, str]:
    """该逐仓期权仓当前保证金（USDC）。margin 字段为空/0 时回退 imr。"""
    rows = okx_sdk.check(okx_sdk.account_for(creds).get_positions(
        instType="OPTION", instId=inst_id))
    r = rows[0] if isinstance(rows, list) and rows else {}
    mgn = _f(r.get("margin"))
    if mgn <= 0:
        mgn = _f(r.get("imr"))
    return mgn, r.get("mgnMode") or ""


def _ensure_collateral(creds: dict, entry: dict) -> Optional[dict]:
    """逐仓现金担保（走 A）：成交后将仓位保证金追加至全损上限。

    背景：isolated 单笔默认仅冻结约 IM（~12% 名义，99-P 实测 1.26 vs 名义
    9.9），标的大幅波动浮亏可击穿单笔保证金 → 提前强平；账户旁的自留现金
    （隔离在外）救不了这笔。把保证金追加至 strike×面值×张数 × 担保比例
    （collateral_ratio_pct，WebUI 可配，默认 100 = 全损上限）后，浮亏上限
    < 保证金 → 数学上无强平路径，可扛到到期按结算了结
    （接货/现金结算），多笔之间仍逐仓隔离互不拖累。

    追加的保证金在平仓/到期后自动释放回账户余额。追加失败不阻断——仓位已
    成交保持 open，台账记 margin_note 供页面提醒补担保。
    """
    strike = _f(entry.get("strike"))
    lot = _f(entry.get("lot"))
    filled = _f(entry.get("filled_sz")) or _f(entry.get("sz")) or 0
    ratio = collateral_ratio_pct()
    if ratio <= 0:
        # 自动担保已关闭：仅平台默认 IM 冻结（浮亏可能击穿提前强平，语义自知）
        return update_ledger(
            lambda x: x["id"] == entry["id"],
            collateral_usd=None, margin_added=None,
            margin_note="自动担保已关闭（比例 0%，仅平台 IM 冻结）")
    target = round(strike * lot * filled * ratio / 100.0, 2)
    if target <= 0:
        return None
    cur, _mgn_mode = _position_margin(creds, entry["inst_id"])
    amt = round(target - cur, 2)
    if amt <= 0.01:
        return update_ledger(
            lambda x: x["id"] == entry["id"],
            collateral_usd=target, margin_added=0.0,
            margin_note="ok（已达标）")
    try:
        okx_sdk.check(okx_sdk.account_for(creds).set_margin_balance(
            instId=entry["inst_id"], posSide=POS_SIDE, type="add",
            amt=str(amt)))
    except Exception as e:  # noqa: BLE001 —— 追加失败不阻断开仓（仓位已成交）
        return update_ledger(
            lambda x: x["id"] == entry["id"],
            collateral_usd=target, margin_added=None,
            margin_note=f"追加失败（仓位已开，担保未到位）: {e}")
    return update_ledger(
        lambda x: x["id"] == entry["id"],
        collateral_usd=target, margin_added=amt,
        margin_note="ok（现金担保已追加）")


def close_put(account: str, *, inst_id: str, sz: int,
              ord_type: str = "limit", px: Optional[float] = None) -> dict:
    """买回平仓卖 put（真实下单）。入口须先找到该 inst 的 open 台账行。"""
    return _close_option(account=account, inst_id=inst_id, sz=sz,
                         ord_type=ord_type, px=px,
                         open_kind="open_put", close_kind="close_put")


def close_call(account: str, *, inst_id: str, sz: int,
               ord_type: str = "limit", px: Optional[float] = None) -> dict:
    """买回平仓卖 call（covered call 平仓，止盈/主动落袋）——镜像 close_put。

    止盈语义：卖 call 收权利金后，权利金回落（如 30%，控制参数待自动化批）
    或主动决策时买回平仓，pnl = (开仓价 − 买回价)×每张面值×张数。
    """
    return _close_option(account=account, inst_id=inst_id, sz=sz,
                         ord_type=ord_type, px=px,
                         open_kind="open_call", close_kind="close_call")


def _close_option(account: str, *, inst_id: str, sz: int,
                  ord_type: str = "limit", px: Optional[float] = None,
                  open_kind: str = "open_put",
                  close_kind: str = "close_put") -> dict:
    """买回平仓公共路径：open_kind/close_kind 决定台账行与记录类型。"""
    a = _entry_account(account)
    open_entry = find_entry(lambda x: (x.get("kind") == open_kind
                                       and x.get("inst_id") == inst_id
                                       and x.get("status") == "open"))
    if open_entry is None:
        label = "卖 put" if open_kind == "open_put" else "卖 call"
        raise OkxSdkError(f"台账无 {inst_id} 的 open {label} 记录（无法平仓）")
    q = ticker_quote(inst_id)
    entry = add_ledger(
        kind=close_kind, account=account or a["label"], inst_id=inst_id,
        strike=open_entry.get("strike"), exp_ms=open_entry.get("exp_ms"),
        lot=open_entry.get("lot"), family=open_entry.get("family"),
        side="buy", ord_type=ord_type, px=px,
        sz=int(sz), status="pending", open_id=open_entry["id"],
        ref_bid=q["bid"], ref_ask=q["ask"],
    )
    try:
        res = _place(a["creds"], inst_id=inst_id, side="buy", sz=int(sz),
                     ord_type=ord_type, px=px, tag=TAG_CLOSE,
                     td_mode=CLOSE_TDMODE)
    except Exception as e:
        update_ledger(lambda x: x["id"] == entry["id"], status="failed",
                      note=f"下单失败: {e}")
        raise
    update_ledger(lambda x: x["id"] == entry["id"], ord_id=res["ord_id"])
    entry["ord_id"] = res["ord_id"]
    return _settle_close_entry(a["creds"], entry, open_entry)


def _settle_close_entry(creds: dict, entry: dict, open_entry: dict) -> dict:
    for _ in range(10):
        o = poll_order(creds, entry["inst_id"], entry["ord_id"])
        if o["status"] == "filled":
            px = o["avg_px"] or entry.get("px") or 0.0
            lot = entry.get("lot") or 0.0
            open_px = open_entry.get("filled_px") or open_entry.get("px") or 0.0
            pnl = (open_px - px) * lot * int(entry.get("sz") or 0)
            update_ledger(lambda x: x["id"] == entry["id"], status="closed",
                          filled_px=px, filled_sz=o["acc_fill_sz"],
                          pnl_usd=round(pnl, 4),
                          **fee_usd_fields(o.get("fee"), "buy", px=px,
                                           option=True, fee_ccy=o.get("fee_ccy")))
            update_ledger(lambda x: x["id"] == open_entry["id"], status="closed",
                          close_ts=entry["ts"], pnl_usd=round(pnl, 4))
            return update_ledger(lambda x: x["id"] == entry["id"]) or entry
        if o["status"] == "cancelled":
            return update_ledger(lambda x: x["id"] == entry["id"],
                                 status="cancelled") or entry
        time.sleep(0.5)
    return entry


def spot_cover(account: str, *, spot_inst: str,
               base_qty: Optional[float] = None,
               quote_amt: Optional[float] = None) -> dict:
    """到期 ITM 现金结算后的现货补买（手动闭环持有现货）。

    spot_inst = OKX 现货交易对（如 BTC-USDC）；二选一指定数量：
    base_qty 按基础币数量（tgtCcy=base_ccy）／quote_amt 按计价币金额（默认）。
    市价单、cash、无杠杆；金额 ≤0 拒绝（fail-closed）。
    """
    a = _entry_account(account)
    if base_qty is not None:
        if base_qty <= 0:
            raise OkxSdkError("补买数量必须 > 0")
        sz, tgt = f"{base_qty:.8f}".rstrip("0").rstrip("."), "base_ccy"
    elif quote_amt is not None:
        if quote_amt <= 0:
            raise OkxSdkError("补买金额必须 > 0")
        sz, tgt = f"{quote_amt:.2f}", "quote_ccy"
    else:
        raise OkxSdkError("补买需指定 base_qty 或 quote_amt")
    entry = add_ledger(
        kind="spot_cover", account=account or a["label"], inst_id=spot_inst,
        spot_inst=spot_inst, side="buy", ord_type="market",
        sz=sz, tgt_ccy=tgt, status="pending",
    )
    params = {
        "instId": spot_inst, "tdMode": "cash", "side": "buy",
        "ordType": "market", "sz": sz, "tgtCcy": tgt, "tag": TAG_COVER,
    }
    is_usd_pair = spot_inst.endswith("-USD")
    params = {
        "instId": spot_inst, "side": "buy",
        "ordType": "market", "sz": sz, "tgtCcy": tgt, "tag": TAG_COVER,
        # 期权子账号为 acctLv=3（Multi-currency margin）模式：OKX 官方 Jupyter
        # 教程第 8 节——multi-currency/portfolio margin 模式下现货订单须
        # tdMode='cross'（cash 仅限 Spot / Spot-and-futures 模式，传 cash 报 51000）
        "tdMode": "cross",
    }
    api = okx_sdk.trade_for(a["creds"])
    try:
        if is_usd_pair:
            # Crypto-USD（官网 币种/USDⓢ，统一 USD 订单簿）：用 USDC 交易须
            # tradeQuoteCcy=USDC（changelog 迁移场景；FAQ：多稳定币自动内部换算）。
            # python-okx set_order 未封装 tradeQuoteCcy → 经 send_request 透传。
            # 9/30 后 instId 须切 Crypto-USDC（届时默认 USDC 结算、可不传 tradeQuoteCcy）。
            params["tradeQuoteCcy"] = "USDC"
            data = okx_sdk.check(api.send_request("/api/v5/trade/order", "POST", **params))
        else:
            data = okx_sdk.check(api.set_order(**params))
    except Exception as e:
        update_ledger(lambda x: x["id"] == entry["id"], status="failed",
                      note=f"下单失败: {e}")
        raise
    row = data[0] if isinstance(data, list) and data else {}
    if row.get("sCode") not in (None, "", "0"):
        raise OkxSdkError(f"OKX {row.get('sCode')} {row.get('sMsg', '')}".strip())
    ord_id = row.get("ordId") or ""
    update_ledger(lambda x: x["id"] == entry["id"], ord_id=ord_id)
    entry["ord_id"] = ord_id
    return _settle_cover_entry(a["creds"], entry)


def _settle_cover_entry(creds: dict, entry: dict) -> dict:
    for _ in range(10):
        if not entry.get("ord_id"):
            return entry
        o = poll_order(creds, entry["inst_id"], entry["ord_id"])
        if o["status"] == "filled":
            base = (entry.get("inst_id") or "").split("-")[0]
            return update_ledger(
                lambda x: x["id"] == entry["id"], status="filled",
                filled_px=o["avg_px"], filled_sz=o["acc_fill_sz"],
                **fee_usd_fields(o.get("fee"), "buy", base=base,
                                 px=o["avg_px"], fee_ccy=o.get("fee_ccy")),
            ) or entry
        if o["status"] == "cancelled":
            return update_ledger(lambda x: x["id"] == entry["id"],
                                 status="cancelled") or entry
        time.sleep(0.5)
    return entry


# 现货交易对：默认 币种-USD（USD 对，USDC 结算）；XAU 例外——
# XAU-USD_UM 的现货标的是 XAUT（Tether Gold），OKX 现货对为 XAUT-USDT
_SPOT_PAIR = {"XAU": "XAUT-USDT"}
# 现货持仓币种（余额查询用）：XAU 家族对应的是 XAUT 而不是 XAU
_SPOT_CCY = {"XAU": "XAUT"}


def spot_pair_of(inst_id: str) -> str:
    """期权 instId → 现货交易对。

    SOL-USD_UM-260909-106-C → SOL-USD（USD 对，2026-09-23 起另有 USDC 对）
    XAU-USD_UM-…            → XAUT-USDT（黄金现货标的是 XAUT）
    """
    base = (inst_id or "").split("-")[0].upper()
    if not base:
        return ""
    return _SPOT_PAIR.get(base, base + "-USD")


def spot_exit(account: str, *, spot_inst: str,
              base_qty: Optional[float] = None,
              quote_amt: Optional[float] = None,
              ref_inst: str = "") -> dict:
    """到期 ITM（call 被行权）现金结算后的现货卖出（出货闭环，C26 批 2）。

    与 spot_cover 镜像（反向）：side=sell、市价单、tdMode=cross（acctLv=3
    multi-currency 模式，与补买同规则）、现货对 币种-USD。U 本位期权现金结算
    只赔差价、不交币——call 被行权后现货仍在账上，市价卖出 ≈ 等效按 K 出货，
    把 covered 组合闭环回无持仓状态（下一轮循环起点）。

    ref_inst = 触发本次出货的 call instId（审计留痕；页面「已出货」判定按此查
    台账 filled spot_exit 行）。数量二选一：base_qty（基础币）／quote_amt（计价币额）。
    两步确认由 handlers 层负责（判定自动、出货人工放行——与补买同规则）。
    """
    a = _entry_account(account)
    if base_qty is not None:
        if base_qty <= 0:
            raise OkxSdkError("出货数量必须 > 0")
        sz, tgt = f"{base_qty:.8f}".rstrip("0").rstrip("."), "base_ccy"
    elif quote_amt is not None:
        if quote_amt <= 0:
            raise OkxSdkError("出货金额必须 > 0")
        sz, tgt = f"{quote_amt:.2f}", "quote_ccy"
    else:
        raise OkxSdkError("出货需指定 base_qty 或 quote_amt")
    entry = add_ledger(
        kind="spot_exit", account=account or a["label"], inst_id=spot_inst,
        spot_inst=spot_inst, side="sell", ord_type="market",
        sz=sz, tgt_ccy=tgt, ref_inst=ref_inst or "", status="pending",
    )
    params = {
        "instId": spot_inst, "side": "sell",
        "ordType": "market", "sz": sz, "tgtCcy": tgt, "tag": TAG_EXIT,
        "tdMode": "cross",   # 见 spot_cover：acctLv=3 现货单须 cross（cash 报 51000）
    }
    api = okx_sdk.trade_for(a["creds"])
    try:
        if spot_inst.endswith("-USD"):
            params["tradeQuoteCcy"] = "USDC"
            data = okx_sdk.check(api.send_request("/api/v5/trade/order", "POST", **params))
        else:
            data = okx_sdk.check(api.set_order(**params))
    except Exception as e:
        update_ledger(lambda x: x["id"] == entry["id"], status="failed",
                      note=f"下单失败: {e}")
        raise
    row = data[0] if isinstance(data, list) and data else {}
    if row.get("sCode") not in (None, "", "0"):
        raise OkxSdkError(f"OKX {row.get('sCode')} {row.get('sMsg', '')}".strip())
    ord_id = row.get("ordId") or ""
    update_ledger(lambda x: x["id"] == entry["id"], ord_id=ord_id)
    entry["ord_id"] = ord_id
    return _settle_exit_entry(a["creds"], entry)


def _settle_exit_entry(creds: dict, entry: dict) -> dict:
    for _ in range(10):
        if not entry.get("ord_id"):
            return entry
        o = poll_order(creds, entry["inst_id"], entry["ord_id"])
        if o["status"] == "filled":
            return update_ledger(
                lambda x: x["id"] == entry["id"], status="filled",
                filled_px=o["avg_px"], filled_sz=o["acc_fill_sz"],
                **fee_usd_fields(o.get("fee"), "sell", px=o["avg_px"],
                                 fee_ccy=o.get("fee_ccy")),
            ) or entry
        if o["status"] == "cancelled":
            return update_ledger(lambda x: x["id"] == entry["id"],
                                 status="cancelled") or entry
        time.sleep(0.5)
    return entry


def exit_prefill_defaults(inst_id: str, sz: int, spot_px: float | None = None,
                          entry: Optional[dict] = None,
                          limits: Optional[dict] = None) -> dict:
    """出货预填（镜像 cover_prefill_defaults）：数量 = 面值(lot)×张数；
    金额 = 数量 × 现货现价（fallback：结算价 → 行权价，均可在页面修改）。

    limits（spot_limits 结果）提供时：数量取 min(面值×张数, 可用余额可卖量)
    ——补买 0.1% 手续费使到账略少于行权数量（0.1 → 0.0999），直接照抄
    行权数量会被交易所拒单（2026-09-14 实测），预填顺手按可用余额收敛。
    返回 {qty, px, px_src, amount, avail, sellable, lot_sz, min_sz, limits_err}。
    """
    lot = FAMILY_LOT.get((inst_id or "").split("-")[0], 0.0)
    if not lot:
        try:
            inst = resolve_instrument(inst_id)
            lot = float(inst.get("lot") or 0.0)
        except Exception:
            lot = 0.0
    px, src = spot_px, "spot"
    if not px and entry:
        if entry.get("settle_px"):
            px, src = entry.get("settle_px"), "settle"
        elif entry.get("strike"):
            px, src = entry.get("strike"), "strike"
    qty = round(lot * sz, 6) if lot else 0.0
    out = {"qty": qty, "px": px, "px_src": src if px else None,
           "amount": round(qty * px, 2) if px else None}
    if limits:
        avail = round(limits.get("avail") or 0.0, 10)
        sellable = limits.get("sellable") or 0.0
        out.update({"avail": avail, "sellable": sellable,
                    "lot_sz": limits.get("lot_sz") or 0.0,
                    "min_sz": limits.get("min_sz") or 0.0,
                    "limits_err": limits.get("err") or ""})
        if sellable > 0 and qty > sellable:
            out["qty"] = round(sellable, 10)
            out["qty_capped"] = True
            if px:
                out["amount"] = round(out["qty"] * px, 2)
    return out


# ── 持仓 / 到期监控（只读）──────────────────────────────────

def account_config(account: str = "") -> dict:
    """子账号账户配置（只读）：期权开通/保证金模式/结算币种/权限。

    v5 /account/config → data[0]：opAuth(0 未开通/1 已开通)、acctLv(3=跨币种)、
    posMode(net_mode/long_short_mode)、settleCcy、perm。
    """
    a = _entry_account(account)
    rows = okx_sdk.check(okx_sdk.account_for(a["creds"]).get_config())
    d = rows[0] if isinstance(rows, list) and rows else {}
    return {
        "uid": str(d.get("uid") or a["uid"] or ""),
        "acct_lv": str(d.get("acctLv") or ""),
        "pos_mode": str(d.get("posMode") or ""),
        "op_auth": int(d.get("opAuth") or 0),
        "settle_ccy": str(d.get("settleCcy") or ""),
        "perm": str(d.get("perm") or ""),
    }


def account_creds(account: str = "") -> dict:
    """子账号 API 凭证（供 broker/data source 层复用 _entry_account 的解析）。

    Broker 的查单（poll_order）需要 creds 而非账号名——此前只有私有
    _entry_account 能拿到；这里给执行层一个稳定的公开入口。
    """
    return _entry_account(account)["creds"]


def account_balance(account: str = "") -> dict:
    """子账号资产（只读）：details 按币种 + 总权益 totalEq(USD)。

    字段（v5 account/balance）：ccy/cashBal/availBal/frozenBal/eq/eqUsd。
    cashBal=现金（含挂单冻结）、availBal=可下新单、frozenBal=冻结（挂单/保证金占用）、
    eq=币种总权益。期权卖方现金担保占用反映在 availBal 减少。
    """
    a = _entry_account(account)
    rows = okx_sdk.check(okx_sdk.account_for(a["creds"]).get_balance())
    d = rows[0] if isinstance(rows, list) and rows else {}
    details = d.get("details") or []
    out = []
    for r in details if isinstance(details, list) else []:
        ccy = r.get("ccy", "")
        eq = _f(r.get("eq"))
        if eq > 0 or _f(r.get("cashBal")) > 0:
            out.append({
                "ccy": ccy,
                "cash_bal": _f(r.get("cashBal")),
                "avail_bal": _f(r.get("availBal")),
                "frozen_bal": _f(r.get("frozenBal")),
                "eq": eq,
                "eq_usd": _f(r.get("eqUsd")),
                "update_ms": int(r.get("uTime") or 0),
            })
    out.sort(key=lambda x: x["eq_usd"], reverse=True)
    return {"total_eq_usd": _f(d.get("totalEq")), "details": out,
            "account": a["name"] or a["label"], "account_uid": a["uid"]}


def covered_context(account: str, family: str) -> dict:
    """卖 call（covered call）上下文：现货对冲覆盖 + 成本锚 C 建议（只读）。

    family 如 SOL-USD_UM → 基础币 SOL。现货对冲覆盖按 99% 容差判定——
    现货补买会扣 0.1% 手续费（0.1 SOL → 0.0999），按面值整数判据会把自己
    刚补的货判成「裸卖」；且 U 本位期权为现金结算（被行权只赔现金差价、
    不交币），现货是对冲工具（浮盈对冲赔付）而非交割物。成本锚建议 = 同
    family 已 settled_itm（被行权接货）put 台账行的 max(strike)。
    """
    base = (family or "").split("-")[0]
    lot = FAMILY_LOT.get(base, 0.0)
    out = {"ok": True, "family": family, "base": base,
           "spot_avail": 0.0, "lot_coin": lot,
           "sellable_sz": 0, "spot_cov_pct": 0.0,
           "cost_hint": None, "note": ""}
    if base == "XAU":
        out["note"] = ("XAU-USD_UM 的现货标的是 XAUT（Tether Gold）——"
                       "对冲/出货按 XAUT-USDT 现货对处理")
    ccy = _SPOT_CCY.get(base, base)
    bal = account_balance(account)
    for r in bal.get("details") or []:
        if r.get("ccy") == ccy:
            out["spot_avail"] = r.get("avail_bal") or 0.0
            break
    if lot and lot > 0:
        # 每张对冲需求 = 面值；容差 1%（补买扣 fee 后 ~99.9% 覆盖即视为可卖）
        need_per = lot * 0.99
        if need_per > 0:
            out["sellable_sz"] = int(out["spot_avail"] / need_per + 1e-6)
        if out["spot_avail"] > 0:
            out["spot_cov_pct"] = round(out["spot_avail"] / lot * 100, 1)
    costs = [float(e.get("strike") or 0)
             for e in load_ledger()
             if e.get("kind") == "open_put"
             and e.get("status") == STATUS_SETTLED_ITM
             and e.get("family") == family]
    if costs:
        out["cost_hint"] = max(costs)
    return out


def _inflight_call_sz(account: str, family: str) -> int:
    """该家族已在仓的 short call 张数（covered 门累计判据用，见 §33.40.3）。

    持仓查询失败 → 抛错（fail-closed）：宁可拒绝卖出，也不能在无法核对在仓量
    时放行（覆盖可能已被重复使用）。
    """
    from .okx_options_assets import right_of_inst   # 延迟导入：assets → trade（FAMILY_LOT）避免循环

    try:
        rows = open_option_positions(account)
    except Exception as e:  # noqa: BLE001 —— 任何查询失败一律 fail-closed
        raise OkxSdkError(
            f"covered 门无法核对在仓 call（持仓查询失败，fail-closed）：{e}") from e
    n = 0
    for r in rows or []:
        inst = str(r.get("inst_id") or "")
        if str(r.get("side") or "").lower() != "short" or right_of_inst(inst) != "C":
            continue
        if inst_family_of(inst) != family:
            continue
        n += int(float(r.get("pos") or 0))
    return n


def open_option_positions(account: str = "") -> list[dict]:
    """OKX 当前期权净仓（只读）。无凭证/无仓位 → []；失败抛错由调用方处理。"""
    a = _entry_account(account)
    rows = okx_sdk.check(
        okx_sdk.account_for(a["creds"]).get_positions(instType="OPTION"))
    out = []
    for r in rows if isinstance(rows, list) else []:
        if r.get("pos") is None or _f(r.get("pos")) == 0:
            continue
        out.append(_normalize_position(r))
    return out


def _normalize_position(r: dict) -> dict:
    inst = r.get("instId", "")
    ps = r.get("posSide")
    if ps in ("long", "short"):
        side = ps
    else:
        # net_mode（跨币种保证金）下 posSide=net，空头由 pos 符号表达
        side = "short" if _f(r.get("pos")) < 0 else "long"
    return {
        "inst_id": inst,
        "side": side,
        "pos": abs(_f(r.get("pos"))),
        "avg_px": _f(r.get("avgPx")),
        "mark_px": _f(r.get("markPx")),
        "upl": _f(r.get("upl")),
        "upl_ratio": _f(r.get("uplRatio")),
        "mgn_mode": r.get("mgnMode", ""),
        "lever": r.get("lever", ""),
        # 逐仓实际冻结保证金（margin 0/空时回退 imr）；足额担保时 OKX 无强平价（--）
        "margin_usd": round(_f(r.get("margin")) or _f(r.get("imr")), 2),
        "liq_px": _norm_liq(r.get("liqPx")),
        "exp_ms": _parse_exp(inst),
        "strike": _parse_strike(inst),
    }


def _parse_strike(inst_id: str) -> Optional[float]:
    parts = inst_id.split("-")
    if len(parts) >= 4:
        try:
            return float(parts[-2])
        except ValueError:
            return None
    return None


def _parse_exp(inst_id: str) -> Optional[int]:
    # instId 格式：FAMILY-QUOTE[_UM]-YYMMDD-STRIKE-C/P（如 SOL-USD_UM-260905-99-P）
    # 日期段是倒数第 3 段（从右数：type、strike、date）——不能用固定 index，
    # family/quote 可能含连字符与 _UM 后缀导致错位（曾取 parts[1] 把
    # USD_UM 当日期解析失败 → 1970-01-01）。
    parts = inst_id.split("-")
    if len(parts) >= 4:
        dseg = parts[-3]
        try:
            y, m, d = 2000 + int(dseg[:2]), int(dseg[2:4]), int(dseg[4:6])
            return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            return None
    return None


def expiry_reminder(account: str = "", hours: int = 72) -> list[dict]:
    """到期待办提醒（C28 1c）：按台账状态分派动作，页面据此给出对应入口。

    action 语义：
      close  = 未到期但临近 → 可平仓了结（也可等现金结算）
      wait   = 已到期、交割账单未出 → 等自动判定（无需动作）
      cover  = put 被行权(ITM)、尚未补买 → 补买（两步确认）
      exit   = call 被行权(ITM)、尚未出货 → 出货（两步确认）
      review = 判定矛盾 → 人工核对台账与 OKX 账单

    闭环判定不依赖判定事件（事件是一次性快照、追溯补单后不会更新），
    直接扫台账 filled 的 spot_cover / spot_exit 行——与台账 tab 的判定口径一致。
    """
    now_ms = int(time.time() * 1000)
    rows = load_ledger()
    closed: set[tuple[str, str]] = set()
    for x in rows:
        if x.get("status") == "filled" and x.get("kind") in ("spot_cover", "spot_exit"):
            base = str(x.get("inst_id") or "").split("-")[0]
            if base:
                closed.add((base, x["kind"]))
    out = []
    for e in rows:
        kind = e.get("kind")
        if kind not in ("open_put", "open_call"):
            continue
        is_call = kind == "open_call"
        st = e.get("status")
        exp = int(e.get("exp_ms") or 0)
        item = {
            "id": e.get("id"), "inst_id": e.get("inst_id"), "account": e.get("account"),
            "strike": e.get("strike"), "exp_ms": exp, "sz": e.get("sz"),
            "premium_usd": e.get("premium_usd"), "lot": e.get("lot"),
            "opt_type": ("C" if is_call else "P"), "kind": kind, "status": st,
        }
        if st == "open":
            if not exp or exp - now_ms > hours * 3600_000:
                continue
            expired = exp <= now_ms
            item["expired"] = expired
            item["action"] = "wait" if expired else "close"
            out.append(item)
        elif st == STATUS_SETTLED_ITM:
            base = str(e.get("inst_id") or "").split("-")[0]
            if (base, "spot_exit" if is_call else "spot_cover") in closed:
                continue
            item["expired"] = True
            item["action"] = "exit" if is_call else "cover"
            out.append(item)
        elif st == STATUS_SETTLED_REVIEW:
            item["expired"] = True
            item["action"] = "review"
            out.append(item)
    _prio = {"cover": 0, "exit": 0, "review": 1, "wait": 2, "close": 3}
    out.sort(key=lambda x: (_prio.get(x["action"], 9), x.get("exp_ms") or 0))
    return out



# ── 到期结算判定（C23，2026-09-07）─────────────────────────

# OKX 账单 type/subType 官方枚举（python-okx Account.get_bills docstring 权威表；
# 用户 OKX 后台中文样本：101-P「账单主类型=交割 / 子类型=到期作废」）。对卖 put：
#   type=3 交割 + subType=172 到期作废   → OTM（结算价 ≥ strike，put 无价值，保证金释放）
#   type=3 交割 + subType=171 到期被行权 → ITM（结算价 < strike，现金赔付）
_BILL_TYPE_DELIVERY = "3"
_BILL_SUBTYPE_WORTHLESS = "172"   # 到期作废（OTM）
_BILL_SUBTYPE_EXERCISED = "171"   # 到期被行权（ITM）

STATUS_SETTLED_OTM = "settled_otm"
STATUS_SETTLED_ITM = "settled_itm"
STATUS_SETTLED_REVIEW = "settled_review"


def settle_expired_puts(now_ms=None) -> list:
    """台账已到期仍 open 的 put/call → 查 OKX 交割账单判定 作废/被行权 → 更新台账。

    判定信号（不做单信号赌博）——每笔结算行交叉校验（按 opt_type 分派）：
      put（卖方）：
        172 到期作废 且 px(结算价) ≥ strike → settled_otm
        171 到期被行权 且 px < strike        → settled_itm
      call（卖方 covered，C26 批 2）：
        172 到期作废 且 px ≤ strike → settled_otm
        171 到期被行权 且 px > strike → settled_itm
      矛盾 / 其他交割 subType（170 等） → settled_review（fail-closed）
      账单未出（OKX 结算后 ~27s 出现）/ 查不到 → 保持 open，下轮重试
    返回本次判定列表 [{id, inst_id, opt_type, status, settle_px, settle_pnl, note}]。
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    settled = []
    for e in load_ledger():
        if e.get("kind") not in ("open_put", "open_call") or e.get("status") != "open":
            continue
        exp_ms = int(e.get("exp_ms") or 0)
        if not exp_ms or exp_ms > now_ms:
            continue  # 未到期
        inst_id = e.get("inst_id") or ""
        opt_type = "C" if e.get("kind") == "open_call" else "P"
        row = _find_delivery_bill(e.get("account") or "", inst_id, exp_ms, now_ms)
        if row is None:
            continue  # 账单未出/延迟 → 保持 open，下轮重试
        sub = row.get("subType")
        px = _f(row.get("px"))          # 账单「成交价」= OKX 结算价（101-P: 105.0）
        pnl = _f(row.get("pnl"))
        strike = float(e.get("strike") or 0)
        result = _classify_settlement(sub, px, strike, opt_type)
        fields = {
            "status": result["status"],
            "settle_subtype": sub,
            "settle_px": px,
            "settle_pnl": round(pnl, 6),
            "settle_ts": _utc_now(),
        }
        # ITM：毛赔付 = 实值部分×面值×张数（净盈亏 settle_pnl 已含权利金收入）
        # put = (行权价−结算价)、call = (结算价−行权价)
        if result["status"] == STATUS_SETTLED_ITM:
            base = (inst_id or "").split("-")[0]
            lot = FAMILY_LOT.get(base)
            sz = int(e.get("sz") or 0)
            intrinsic = (px - strike) if opt_type == "C" else (strike - px)
            fields["settle_payout"] = (round(intrinsic * lot * sz, 6)
                                        if lot and sz and px is not None else None)
        if result["status"] == STATUS_SETTLED_REVIEW:
            fields["note"] = result["note"]
        if update_ledger(lambda x: x["id"] == e["id"], **fields) is not None:
            settled.append({"id": e["id"], "inst_id": inst_id,
                            "opt_type": opt_type,
                            "status": result["status"], "settle_px": px,
                            "settle_pnl": round(pnl, 6),
                            "settle_payout": fields.get("settle_payout"),
                            "note": result.get("note", "")})
    return settled


#: 语义别名（判定已覆盖 put/call；旧名保留兼容 handlers/daemon/测试）
settle_expired_options = settle_expired_puts


def backfill_settlements(now_ms=None, lookback_days: int = 90) -> dict:
    """历史遗留行赔付回填：已 settled_* 但缺 settle_px/settle_pnl 的台账行。

    背景（2026-09-14）：到期判定入口曾只跟「判定事件」走，实验期写入的
    settled_itm 行（无 settle 事件、无赔付数据）没有任何补判路径——台账行
    赔付永远显示「—」。本函数按 OKX 交割账单回填 settle_px/settle_pnl/
    settle_payout/settle_subtype，**不改状态**（仅当账单判定与现有状态冲突时
    追加 note 提示人工核对，fail-closed 不自动改状态）。

    返回 {scanned, filled: [...], skipped: [...], errors: [...]}。
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    res: dict = {"scanned": 0, "filled": [], "skipped": [], "errors": []}
    for e in load_ledger():
        kind = e.get("kind")
        st = e.get("status") or ""
        if kind not in ("open_put", "open_call"):
            continue
        if st not in (STATUS_SETTLED_ITM, STATUS_SETTLED_OTM,
                      STATUS_SETTLED_REVIEW):
            continue
        if e.get("settle_px"):          # 已有赔付数据 → 无需回填
            continue
        exp_ms = int(e.get("exp_ms") or 0)
        if not exp_ms or exp_ms > now_ms:
            continue
        res["scanned"] += 1
        inst_id = e.get("inst_id") or ""
        opt_type = "C" if kind == "open_call" else "P"
        try:
            row = _find_delivery_bill(e.get("account") or "", inst_id, exp_ms,
                                      exp_ms + lookback_days * 86400000)
        except (OkxSdkError, RuntimeError) as ex:
            res["errors"].append({"id": e.get("id"), "inst_id": inst_id,
                                  "error": str(ex)})
            continue
        if row is None:
            res["skipped"].append({
                "id": e.get("id"), "inst_id": inst_id,
                "reason": f"到期后 {lookback_days} 天内未找到交割账单（可能已超出可查范围）"})
            continue
        px = _f(row.get("px"))                 # 结算价
        pnl = _f(row.get("pnl"))               # 净盈亏（含权利金）
        sub = row.get("subType")
        strike = float(e.get("strike") or 0)
        result = _classify_settlement(sub, px, strike, opt_type)
        fields = {"settle_subtype": sub, "settle_px": px,
                  "settle_pnl": round(pnl, 6), "settle_ts": _utc_now(),
                  "settle_backfilled": True}
        if result["status"] == STATUS_SETTLED_ITM:
            base = (inst_id or "").split("-")[0]
            lot = FAMILY_LOT.get(base)
            sz = int(e.get("sz") or 0)
            intrinsic = (px - strike) if opt_type == "C" else (strike - px)
            fields["settle_payout"] = (round(intrinsic * lot * sz, 6)
                                       if lot and sz and px is not None else None)
        if result["status"] != st:
            fields["note"] = ((e.get("note") or "") + "｜" +
                              f"回填：账单判定 {result['status']} 与台账 {st} "
                              "不一致，请人工核对").strip("｜")
        if update_ledger(lambda x: x.get("id") == e.get("id"), **fields) is not None:
            res["filled"].append({"id": e.get("id"), "inst_id": inst_id,
                                  "settle_px": px, "settle_pnl": round(pnl, 6),
                                  "settle_payout": fields.get("settle_payout")})
    return res


def _find_delivery_bill(account: str, inst_id: str, begin_ms: int,
                        end_ms: int):
    """查该 put 在 [expiry, now] 窗口的交割账单行（type=3，按 instId 匹配）。"""
    a = _entry_account(account)
    rows = okx_sdk.check(okx_sdk.account_for(a["creds"]).get_bills(
        instType="OPTION", type=_BILL_TYPE_DELIVERY,
        begin=str(begin_ms), end=str(end_ms), limit="100"))
    hits = [r for r in (rows if isinstance(rows, list) else [])
            if r.get("instId") == inst_id
            and r.get("type") == _BILL_TYPE_DELIVERY]
    return hits[0] if hits else None


def _classify_settlement(sub: str, px: float, strike: float,
                         opt_type: str = "P") -> dict:
    """账单行 subType + 结算价交叉判定 OTM/ITM（矛盾 → review，不猜）。

    put：OTM = 结算价 ≥ K（puts 无价值）；ITM = 结算价 < K。
    call：OTM = 结算价 ≤ K；ITM = 结算价 > K（被行权，赔 (结算价−K)×面值）。
    """
    is_call = str(opt_type).upper() == "C"
    if sub == _BILL_SUBTYPE_WORTHLESS:            # 172 到期作废
        ok = px <= strike if is_call else px >= strike
        if ok:
            return {"status": STATUS_SETTLED_OTM}
        return {"status": STATUS_SETTLED_REVIEW,
                "note": "作废行但结算价 {} {} strike {}，账单存疑".format(
                    px, ">" if is_call else "<", strike)}
    if sub == _BILL_SUBTYPE_EXERCISED:            # 171 到期被行权
        ok = px > strike if is_call else px < strike
        if ok:
            return {"status": STATUS_SETTLED_ITM}
        return {"status": STATUS_SETTLED_REVIEW,
                "note": "被行权行但结算价 {} {} strike {}，账单存疑".format(
                    px, "≤" if is_call else "≥", strike)}
    return {"status": STATUS_SETTLED_REVIEW,
            "note": "交割账单未知 subType={}，请人工核对".format(sub)}

# ── 两步确认（内存 pending action，30s TTL）─────────────────

_pending: dict[str, dict] = {}


def stage_action(action: str, payload: dict) -> tuple[str, dict]:
    """生成一次性 tx_id（30s 过期）。同一动作并发/重放由 tx_id 一次性防住。"""
    tx_id = secrets.token_urlsafe(12)
    _pending[tx_id] = {"action": action, "payload": payload, "ts": time.time()}
    _gc_pending()
    return tx_id, {"tx_id": tx_id, "expires_in": int(_TTL), "payload": payload}


def take_action(tx_id: str) -> dict:
    """校验并取走 pending action（一次性；过期/不存在抛错）。"""
    p = _pending.pop(tx_id, None)
    if p is None:
        raise OkxSdkError("确认令牌无效或已过期（30 秒内需确认）")
    if time.time() - p["ts"] > _TTL:
        raise OkxSdkError("确认令牌已过期（30 秒），请重新发起")
    return p


def _gc_pending() -> None:
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["ts"] > _TTL]:
        _pending.pop(k, None)


def _f(v) -> float:
    if v in (None, ""):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _norm_liq(v) -> Optional[float]:
    """强平价 liqPx 归一化：空 / "--" / 非数字 → None（OKX 足额担保时无强平价）。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "--", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None
def manual_close_entry(entry_id: str, note: str = "") -> Optional[dict]:
    """台账手动关账（单步、无资金流）：把 open 卖 put/call 行标 closed_manual。

    用途：OKX 官方后台手动平仓等系统外操作收尾——仓位在交易所已消失，
    但台账状态机只认本系统成交路径，open 行会变 phantom（settle 只处理
    已到期行、该仓永远查不到交割账单）。手动关账只做台账标记留痕，不查
    OKX、不回填平仓价/盈亏（外部成交不在本系统内）。
    仅 kind=open_put/open_call 且 status=open 可关；已 settled/closed 行拒绝（幂等）。
    """
    default_note = "官方后台平仓（系统外操作），手动关账"
    e = update_ledger(
        lambda x: x.get("kind") in ("open_put", "open_call")
                  and x.get("status") == "open"
                  and x.get("id") == entry_id,
        status="closed_manual",
        close_ts=_utc_now(),
        note=note or default_note)
    return e


def reopen_entry(entry_id: str) -> Optional[dict]:
    """撤销手动关账：closed_manual → open（误关账恢复，交回到期巡检/settle 管辖）。

    仅 kind=open_put/open_call 且 status=closed_manual 可撤销；settled/closed 是
    资金流终态（到期结算/买回平仓后仓位已了结），不可逆。恢复后保留原 note 并
    追加撤销标记，close_ts 残留无碍（open 行渲染不显示）。
    """
    cur = find_entry(lambda x: x.get("kind") in ("open_put", "open_call")
                     and x.get("status") == "closed_manual" and x.get("id") == entry_id)
    if cur is None:
        return None
    note = (cur.get("note") or "") + " | 已撤销手动关账（回到 open）"
    return update_ledger(lambda x: x.get("id") == entry_id
                         and x.get("status") == "closed_manual",
                         status="open", note=note)


# ── instrument / 盘口辅助 ──────────────────────────────────────
