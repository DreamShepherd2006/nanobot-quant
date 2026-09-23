"""上交所云行情（yunhq）ETF 期权链 —— A股 期权研究线的官方数据源。

入口与实测（2026-09-23，容器内直连；详见 docs/quant-system.md §33.38.10）
----------------------------------------------------------------------
* **可用**：``http://yunhq.sse.com.cn:32041/v1/sho/list/tstyle/{标的}``
  上交所官方云行情，**实时**返回该标的全部在售期权（认购 + 认沽、各到期月）。
  实测合约数：510050=96 / 510300=104 / 510500=160 / 588000=170 / 588080=164；
  HF Space 容器直连 HTTP 200、0.4–0.9s（亚秒级），无需任何代理。
* **不可用（我们所在网络）**：官网披露接口
  ``query.sse.com.cn/commonQuery.do``（第三方 VIX 复现项目的主源）在容器与
  HF Space 上**整域 403**（IP 层封锁，与东财同类）；深交所官网
  ``www.szse.cn`` TCP reset —— 深市期权价格只能走新浪（``sina`` 源）。
* **字段按 ``select`` 顺序「位置」返回**（不是命名对象）。已实测可用字段：
  ``code``(数字 ID) / ``contractid`` / ``name`` / ``last`` / ``open`` /
  ``high`` / ``low`` / ``volume``(张) / ``amount``(元)。
  ``bid/ask``、持仓量的字段名**尚未探到**（探针在午间休市执行，需交易时段
  复测）—— 本模块只暴露已确认字段，**绝不猜字段名**（猜错会静默拿到 null，
  正是最坏的失败形态）。

合约码自带全部维度（无需再请求一次接口）：``510050C2609M02850`` =
标的 ``510050`` + ``C``/``P``（认购/认沽）+ ``YYMM``（26 年 9 月到期）+
``M``/``A``（M=标准档、A=调整档）+ 5 位行权价（÷1000 → 2.850）。

**不进 ``data_sources`` 注册表**：registry 的契约是「K 线 / 取价 / 盘口」，
期权链是另一种数据类型（同 OKX 期权线在 ``okx_options_data.py`` 的先例）；
本模块由 ``tools/tools_ashare.py``（只读体检）与后续 A股 研究线工具直接
import。kind=research：纯只读研究源，不参与任何交易路径。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from typing import Optional

import pandas as pd

# 上交所（SSE）有期权的 5 只 ETF —— 完整标的池，不存在选择自由度。
SSE_UNDERLYINGS: tuple[str, ...] = ("510050", "510300", "510500", "588000", "588080")

YUNHQ_LIST_URL = "http://yunhq.sse.com.cn:32041/v1/sho/list/tstyle/"

# 接口要求浏览器 UA + 上交所 Referer（缺 Referer 大概率被拒）。
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "http://www.sse.com.cn/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

# 只取「已实测确认」的字段（见模块 docstring）。
_SELECT = "code,contractid,name,last,open,high,low,volume,amount"

_COLUMNS = ("underlying", "contractid", "code", "name", "right", "expiry", "strike",
            "last", "open", "high", "low", "volume", "amount")

_CONTRACT_RE = re.compile(r"^(?P<underlying>\d{6})(?P<right>[CP])(?P<expiry>\d{4})"
                          r"(?P<flag>[A-Z]?)(?P<strike>\d{5})$")


def _log(msg: str) -> None:
    """诊断一律走 stderr（stdout 归 MCP JSON-RPC；静默失败不可接受）。"""
    print(f"[SSE-OPT] {msg}", file=sys.stderr, flush=True)


def _get_json(url: str, timeout: int = 20) -> dict:
    """GET 一个 JSON 端点。HTTP/解析错误一律抛出，不做静默兜底。"""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RuntimeError(f"上交所云行情返回非 JSON（{exc}）：{text[:120]!r}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"上交所云行情返回类型异常：{type(data).__name__}")
    return data


def parse_contract(contractid: str) -> Optional[dict]:
    """``510050C2609M02850`` → {underlying, right, expiry, flag, strike}。

    无法解析返回 ``None``（调用方计数并留痕，不阻断整链）。
    """
    m = _CONTRACT_RE.match((contractid or "").strip())
    if not m:
        return None
    g = m.groupdict()
    return {
        "underlying": g["underlying"],
        "right": g["right"],           # C=认购 / P=认沽
        "expiry": g["expiry"],         # YYMM，如 "2609"
        "flag": g["flag"] or "M",      # M=标准档 / A=调整档
        "strike": int(g["strike"]) / 1000.0,
    }


def fetch_chain(underlying: str = "510050", timeout: int = 20) -> pd.DataFrame:
    """拉取某标的的**全部在售期权合约**（认购 + 认沽，实时快照）。

    返回列：``_COLUMNS``（``last/open/high/low/volume/amount`` 已转数值，
    缺失为 NaN）。取不到数据一律 ``RuntimeError``（fail-closed），错误里带
    原始片段，绝不用空表冒充「无数据」。
    """
    code = str(underlying).strip()
    url = YUNHQ_LIST_URL + code + "?" + urllib.parse.urlencode(
        {"select": _SELECT, "begin": 0, "end": 500})
    data = _get_json(url, timeout=timeout)

    rows = data.get("list")
    if rows is None:
        raise RuntimeError(f"上交所云行情响应缺 list 字段（{code}）：{str(data)[:160]}")
    if not rows:
        raise RuntimeError(
            f"上交所云行情返回 0 个合约（{code}）—— 该标的是否有期权？"
            f"上交所可用标的：{'/'.join(SSE_UNDERLYINGS)}（深市标的不在此接口）")

    recs, bad = [], 0
    names = _SELECT.split(",")
    for row in rows:
        cells = list(row) + [None] * (len(names) - len(row))
        item = dict(zip(names, cells))
        meta = parse_contract(str(item.get("contractid") or ""))
        if not meta:
            bad += 1
            if bad <= 3:
                _log(f"合约码无法解析、已跳过：{item.get('contractid')!r}")
            continue
        recs.append({**meta, **{k: item.get(k) for k in names}})
    if not recs:
        raise RuntimeError(f"上交所云行情 {len(rows)} 行全部无法解析合约码（{code}）")
    if bad:
        _log(f"{code}：{bad}/{len(rows)} 行合约码无法解析、已跳过")

    df = pd.DataFrame(recs)
    for col in _COLUMNS:
        if col not in df.columns:
            df[col] = None
    for col in ("strike", "last", "open", "high", "low", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.loc[:, list(_COLUMNS)].sort_values(
        ["expiry", "strike", "right"], kind="mergesort").reset_index(drop=True)
    _log(f"{code} 合约 {len(df)} 个（到期月 {sorted(df['expiry'].unique())}）")
    return df


def chain_summary(underlying: str = "510050", timeout: int = 20) -> dict:
    """期权链概览：合约数 / 认购认沽数 / 各到期月 / 成交量与成交额合计。"""
    df = fetch_chain(underlying, timeout=timeout)
    by_expiry = {str(k): int(v) for k, v in df.groupby("expiry").size().items()}
    return {
        "underlying": str(underlying),
        "total": int(len(df)),
        "calls": int((df["right"] == "C").sum()),
        "puts": int((df["right"] == "P").sum()),
        "expiries": by_expiry,
        "strike_min": float(df["strike"].min()),
        "strike_max": float(df["strike"].max()),
        "volume": float(df["volume"].fillna(0).sum()),
        "amount": float(df["amount"].fillna(0).sum()),
    }


def put_call_ratio(underlying: str = "510050", timeout: int = 20) -> dict:
    """P/C（成交量口径的认沽认购比）。

    持仓量口径暂不可得（该字段名未探到），只能给成交量口径 —— 返回里
    ``volume_based_note`` 显式标注，避免被当成长久期情绪指标误用。
    认购成交量为 0 时 ``pcr`` 为 ``None``（不做除零猜测）。
    """
    df = fetch_chain(underlying, timeout=timeout)
    call_v = float(df.loc[df["right"] == "C", "volume"].fillna(0).sum())
    put_v = float(df.loc[df["right"] == "P", "volume"].fillna(0).sum())
    return {
        "underlying": str(underlying),
        "call_volume": call_v,
        "put_volume": put_v,
        "pcr": (put_v / call_v) if call_v else None,
        "basis": "volume",
        "volume_based_note": "成交量口径（持仓量字段名未探到，暂不可得）",
    }


def available_underlyings(timeout: int = 20) -> dict:
    """逐个标的探一次，返回 ``{标的: 合约数}``；失败记 ``-1`` 且留痕。"""
    out: dict[str, int] = {}
    for code in SSE_UNDERLYINGS:
        try:
            out[code] = int(len(fetch_chain(code, timeout=timeout)))
        except Exception as exc:  # noqa: BLE001 — 单标的失败不阻断其余标的
            _log(f"{code} 探测失败：{type(exc).__name__}: {exc}")
            out[code] = -1
    return out
