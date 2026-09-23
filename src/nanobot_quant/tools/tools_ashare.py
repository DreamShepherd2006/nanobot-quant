"""probe_ashare_sources —— A股 研究线数据源可达性体检（只读）。

为什么需要它（2026-09-23 的教训）
--------------------------------
「某个源可用」是**环境相关的结论、不是恒定事实**：同一个端点在两处网络下
命运完全不同 —— 上交所官网披露接口在我们这里 403，而云行情 200；东财 TCP
reset，新浪却 200。第三方资料里的「上交所官方源可用」也是**作者所处环境**
的结论，原样照搬就会在落地时翻车。所以：

* 任何 A股 取数方案落地前，先跑一遍体检；
* 换环境（新空间 / 新机房 / 换云）后重跑一遍，不要沿用旧结论。

行为约定
--------
* **fail-soft**：单个源失败不阻断其它源（逐个 try，各自计时）。
* **fail-visible**：失败必须带原始错误/状态码，绝不允许静默降级成「无数据」。
* **只读**：不读持仓、不写台账、不下单、不改任何配置（有结构性测试锁定）。

产出：结构化 ``sources`` 列表 + ``markdown``（用户偏好可直接粘贴进对话）。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

from nanobot_quant.data_sources import sse_options

# 上交所官网披露接口（第三方 VIX 复现项目的主源）：我们网络环境整域 403。
_SSE_OFFICIAL = ("http://query.sse.com.cn/commonQuery.do?isPagination=false"
                 "&expireDate=&securityId="
                 "&sqlId=SSE_ZQPZ_YSP_GGQQZSXT_XXPL_DRHY_SEARCH_L")
# 深交所官网报表接口：我们网络环境 TCP reset。
_SZSE_OFFICIAL = "http://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=ysplbrb"
# 新浪：A 股现货行情（GBK）+ 股指期货连续合约（基差数据基础）。
_SINA_QUOTE = "https://hq.sinajs.cn/list=sh510050"
_SINA_FUTURES = "https://hq.sinajs.cn/list=nf_IF0,nf_IH0,nf_IC0,nf_IM0"
# 腾讯：A 股日线（RV / 已实现波动计算用）。
_TENCENT_QUOTE = "http://qt.gtimg.cn/q=sh510050"
# 华创证券 HCVIX：长历史 IV 参照（2015-02-09 起，50ETF / 300ETF）。
_HCVIX = "https://service.hcquant.com/production/hcvix_public.php"

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _log(msg: str) -> None:
    print(f"[ASHARE-PROBE] {msg}", file=sys.stderr, flush=True)


def _http(url: str, referer: str = "", timeout: int = 15,
          encoding: str = "utf-8") -> dict:
    """GET 一次，返回 ``{status, bytes, text, ms}``；异常原样抛出给调用方记账。"""
    headers = {"User-Agent": _BROWSER_UA, "Accept": "*/*",
               "Accept-Language": "zh-CN,zh;q=0.9", "Connection": "keep-alive"}
    if referer:
        headers["Referer"] = referer
    t0 = time.time()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        status = resp.status
    return {"status": status, "bytes": len(raw), "ms": int((time.time() - t0) * 1000),
            "text": raw.decode(encoding, "replace")}


def _row(source: str, display: str, status: str, ms: int = 0, detail: str = "",
         sample: str = "", expect: str = "") -> dict:
    return {"source": source, "display": display, "status": status, "ms": ms,
            "detail": detail, "sample": sample, "expect": expect}


# ── 各源体检（每个函数只做一件事，失败抛异常由 probe 记账）──────────────

def _check_sse_yunhq(timeout: int, echo_samples: bool) -> dict:
    """上交所云行情：5 只 ETF 逐个拉一次期权链（主力来源）。"""
    t0 = time.time()
    totals = sse_options.available_underlyings(timeout=timeout)
    ok_codes = {k: v for k, v in totals.items() if v > 0}
    ms = int((time.time() - t0) * 1000)
    sample = ""
    if echo_samples and ok_codes:
        first = next(iter(ok_codes))
        df = sse_options.fetch_chain(first, timeout=timeout)
        row = df.iloc[0]
        sample = (f"{first}: {row['contractid']} {row['name']} last={row['last']} "
                  f"vol={row['volume']} · {len(df)} 个合约")
    detail = (f"{len(ok_codes)}/{len(totals)} 标的可用 · "
              + " ".join(f"{k}={v}" for k, v in totals.items()))
    status = "ok" if len(ok_codes) == len(totals) else ("fail" if not ok_codes else "partial")
    return _row("sse_yunhq", "上交所云行情（期权链 · 实时）", status, ms, detail, sample)


def _check_sse_official(timeout: int, echo_samples: bool) -> dict:
    """上交所官网披露接口 —— 已知在我们环境 403（保留检查以便发现变化）。"""
    t0 = time.time()
    try:
        res = _http(_SSE_OFFICIAL, referer="http://www.sse.com.cn/", timeout=timeout)
        return _row("sse_official", "上交所官网披露接口", "ok", int((time.time() - t0) * 1000),
                    f"HTTP {res['status']} · {res['bytes']}B（意外：我们环境原为 403，"
                    f"若已放通可重新评估）", res["text"][:120])
    except urllib.error.HTTPError as exc:
        return _row("sse_official", "上交所官网披露接口", "fail",
                    int((time.time() - t0) * 1000),
                    f"HTTP {exc.code}（预期：整域 IP 层封锁，取数走云行情）",
                    expect="预期失败")
    except Exception as exc:  # noqa: BLE001
        return _row("sse_official", "上交所官网披露接口", "fail",
                    int((time.time() - t0) * 1000),
                    f"{type(exc).__name__}: {exc}（预期：整域 IP 层封锁）", expect="预期失败")


def _check_szse_official(timeout: int, echo_samples: bool) -> dict:
    """深交所官网接口 —— 已知在我们环境 TCP reset。"""
    t0 = time.time()
    try:
        res = _http(_SZSE_OFFICIAL, referer="https://www.szse.cn/", timeout=timeout)
        return _row("szse_official", "深交所官网接口", "ok", int((time.time() - t0) * 1000),
                    f"HTTP {res['status']} · {res['bytes']}B（意外：原为 TCP reset）",
                    res["text"][:120])
    except Exception as exc:  # noqa: BLE001
        return _row("szse_official", "深交所官网接口", "fail",
                    int((time.time() - t0) * 1000),
                    f"{type(exc).__name__}: {str(exc)[:80]}（预期：数据中心 IP 断连）",
                    expect="预期失败")


def _check_sina_quote(timeout: int, echo_samples: bool) -> dict:
    """新浪现货行情（深市期权价格目前只能走它）。"""
    t0 = time.time()
    res = _http(_SINA_QUOTE, referer="https://finance.sina.com.cn",
                timeout=timeout, encoding="gbk")
    fields = res["text"].split('"')[1].split(",") if '"' in res["text"] else []
    name = fields[0] if fields else "?"
    last = fields[3] if len(fields) > 3 else "?"
    return _row("sina_quote", "新浪 A 股行情", "ok", res["ms"],
                f"HTTP {res['status']} · sh510050 {name} last={last}", res["text"][:100])


def _check_sina_kline(timeout: int, echo_samples: bool) -> dict:
    """新浪 K 线（注册表源，A 股 现货 TD / RV 用）。"""
    from nanobot_quant.data_sources import get_data_source
    t0 = time.time()
    df = get_data_source("sina").fetch_kline("510050", bar="1D", limit=60)
    ms = int((time.time() - t0) * 1000)
    if df is None or not len(df):
        raise RuntimeError("sina.fetch_kline 返回空表（不静默）")
    # 列名大小写由各源自行决定（sina 股票源实测为小写），不得假定 Close。
    cmap = {str(c).lower(): c for c in df.columns}
    close_col = cmap.get("close")
    sample = ""
    if echo_samples and close_col is not None:
        sample = f"close[-1]={df[close_col].iloc[-1]}"
    tail = "" if close_col else "（列名里无 close，采样跳过）"
    return _row("sina_kline", "新浪 A 股 K 线", "ok", ms,
                f"510050 1D {len(df)} 根 · {df.index[0]} → {df.index[-1]}{tail}", sample)


def _check_sina_futures(timeout: int, echo_samples: bool) -> dict:
    """新浪股指期货连续合约 —— 基差闸门的数据基础（C40 ①）。"""
    res = _http(_SINA_FUTURES, referer="https://finance.sina.com.cn",
                timeout=timeout, encoding="gbk")
    vals = {}
    for seg in res["text"].split(";"):
        if '"' not in seg:
            continue
        head, body = seg.split("=", 1)
        key = head.strip().split("_")[-1]
        parts = body.strip().strip('"').split(",")
        vals[key] = parts[1] if len(parts) > 1 else "?"
    if not vals:
        raise RuntimeError(f"新浪期货返回无法解析：{res['text'][:100]!r}")
    return _row("sina_futures", "新浪股指期货（基差基础）", "ok", res["ms"],
                " · ".join(f"{k}={v}" for k, v in vals.items()), res["text"][:100])


def _check_tencent_quote(timeout: int, echo_samples: bool) -> dict:
    """腾讯行情（日线；RV 计算备用源）。"""
    res = _http(_TENCENT_QUOTE, referer="https://gu.qq.com/",
                timeout=timeout, encoding="gbk")
    parts = res["text"].split('"')[1].split("~") if '"' in res["text"] else []
    name = parts[1] if len(parts) > 1 else "?"
    last = parts[3] if len(parts) > 3 else "?"
    return _row("tencent_quote", "腾讯行情（日线）", "ok", res["ms"],
                f"HTTP {res['status']} · sh510050 {name} last={last}", res["text"][:100])


def _check_hcvix(timeout: int, echo_samples: bool) -> dict:
    """华创 HCVIX —— 长历史 IV 参照（iVIX 2018-02 已停发）。"""
    # 单页 708KB、线路较慢（实测 ~19s），给足超时以免误报不可用。
    res = _http(_HCVIX, timeout=max(timeout, 30))
    hit = "HCVIX" in res["text"]
    if not hit:
        raise RuntimeError(f"页面未含 HCVIX 字样（{res['bytes']}B）")
    return _row("hcvix", "华创 HCVIX（长历史 IV）", "ok", res["ms"],
                f"HTTP {res['status']} · {res['bytes']}B · 含 HCVIX 标记", "")


_CHECKS: tuple[tuple[str, str, object], ...] = (
    ("sse_yunhq", "上交所云行情（期权链 · 实时）", _check_sse_yunhq),
    ("sina_quote", "新浪 A 股行情", _check_sina_quote),
    ("sina_kline", "新浪 A 股 K 线", _check_sina_kline),
    ("sina_futures", "新浪股指期货（基差基础）", _check_sina_futures),
    ("tencent_quote", "腾讯行情（日线）", _check_tencent_quote),
    ("hcvix", "华创 HCVIX（长历史 IV）", _check_hcvix),
    ("sse_official", "上交所官网披露接口", _check_sse_official),
    ("szse_official", "深交所官网接口", _check_szse_official),
)


def _environment() -> str:
    """当前运行环境标识（用于把结论与环境绑定，避免跨环境误用）。"""
    space = os.environ.get("SPACE_ID") or os.environ.get("HF_SPACE") or ""
    host = ""
    try:
        host = socket.gethostname()
    except Exception:  # noqa: BLE001
        pass
    return f"space={space or '—'} host={host or '—'}"


def probe_ashare_sources(echo_samples: bool = True, timeout: int = 15) -> dict:
    """A股 研究线数据源可达性体检（只读）。

    Args:
        echo_samples: 是否附带样例数据（首行/数值），False 时只报通道状态。
        timeout: 单个端点的超时秒数（默认 15）。

    Returns:
        ``{ok, total, failed, environment, sources: [...], markdown}``；
        ``sources`` 每项含 source/display/status/ms/detail/sample。
        单个源异常不影响其余源（fail-soft），但错误信息原样返回（fail-visible）。
    """
    t0 = time.time()
    rows: list[dict] = []
    for source, display, fn in _CHECKS:
        try:
            row = fn(timeout, echo_samples)
        except Exception as exc:  # noqa: BLE001 — 单源失败不得阻断体检
            # 失败行用注册表里的 source/display（而非函数名），报告可读且可定位
            row = _row(source, display, "fail", 0,
                       f"{type(exc).__name__}: {str(exc)[:160]}")
            _log(f"{source} 体检异常：{type(exc).__name__}: {exc}")
        rows.append(row)

    ok = sum(1 for r in rows if r["status"] == "ok")
    unusable = [r["source"] for r in rows if r["status"] == "fail"]
    # 「预期失败」= 我们网络环境的已知边界（上交所官网 403 / 深交所 reset）：
    # 单独列出，避免真问题淹没在预期边界里；failed 仍如实计数。
    unexpected = [r["source"] for r in rows if r["status"] == "fail"
                  and r["expect"] != "预期失败"]
    result = {
        "ok": ok,
        "total": len(rows),
        "failed": len(unusable),
        "unexpected_failures": unexpected,
        "environment": _environment(),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "sources": rows,
    }
    result["markdown"] = _render_markdown(result)
    return result


def _render_markdown(result: dict) -> str:
    """与页面/对话可直接粘贴的 markdown（与 backtest/f1 markdown 同风格）。"""
    lines = [
        "## 🧪 A股 数据源可达性体检（只读）",
        "",
        f"**{result['ok']}/{result['total']} 可用** · 耗时 {result['elapsed_ms'] / 1000:.1f}s "
        f"· 环境 `{result['environment']}`",
        "",
        "| 源 | 用途 | 状态 | 耗时 | 细节 |",
        "|:--|:--|:--:|--:|:--|",
    ]
    for r in result["sources"]:
        icon = {"ok": "✅", "partial": "⚠️", "fail": "❌"}.get(r["status"], "❔")
        lines.append(f"| `{r['source']}` | {r['display']} | {icon} {r['status']} | "
                     f"{r['ms']}ms | {r['detail']} |")
    samples = [f"- `{r['source']}`：{r['sample']}" for r in result["sources"] if r["sample"]]
    if samples:
        lines += ["", "**样例**", *samples]
    fails = [r for r in result["sources"] if r["source"] in result.get("unexpected_failures", [])]
    notes = [
        "- 标注「预期失败」的两项（上交所官网披露接口、深交所官网）是**我们网络环境的已知边界**，"
        "期权取数走云行情、深市价格走新浪。",
        "- 结论与环境绑定：换空间/换机房后请重跑本体检，勿沿用旧结论。",
    ]
    if fails:
        notes.append("- ⚠️ 出现**非预期失败**：" + "、".join(f"`{r['source']}`" for r in fails)
                     + " —— 取数方案依赖它们时应先定位。")
    lines += ["", "**说明**", *notes]
    return "\n".join(lines)


def main() -> None:
    """CLI 自测入口：``python3 -m nanobot_quant.tools.tools_ashare``。"""
    res = probe_ashare_sources()
    print(res["markdown"])
    print()
    print(json.dumps({k: v for k, v in res.items() if k != "markdown"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
