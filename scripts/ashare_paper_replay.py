"""A 股 ETF 期权模拟账本 —— 真实历史数据的离线回放（自测工具，不在生产路径上）。

用途
----
PR 1 只交付记账核心（``ashare/paper_ledger.py``）与规则纯函数
（``ashare/paper_strategy.py``），没有 runner。要证明「账本能算对」，
用 QMT 导出的真实 1m 数据逐 bar 回放一遍是最直接的证据：

- 现金、担保占用、持仓、接货量逐笔可核；
- 净值曲线与成交明细肉眼能对；
- 同一段数据反复跑结果必须逐位一致（确定性，无随机、无网络）。

它**不是**回测引擎
------------------
- 不追求与 QMT 回测脚本的净值逐位一致：成交价口径不同（这里 = bar 收盘
  ± 1 tick，QMT 回测用 bar close），且这里按 100% 全损担保占资金、QMT 回测
  按平台保证金 —— 两个口径不可直接相减；
- 不做参数扫描、不产出策略结论（那是期权回测页的职责）；
- 数据路径全部由参数传入，脚本内不写死任何 workspace 路径。

用法::

    python3 scripts/ashare_paper_replay.py \\
        --etf-csv   data/qmt-etf-1m/run_20261120_020223/510050_SH_1m.csv \\
        --opt-dir   data/qmt-option-1m/run_20261120_012436 \\
        --contracts data/qmt-option-1m/meta_run_20261120_012836/contracts_all.csv \\
        --underlying 510050.SH --initial 500000

数据依赖（QMT 导出，见 skill ``qmt-option-data``）：
- ETF 1m：``stime,close,high,low,open,time,...``（时间列 CST）
- 期权 1m：``ts,code,open,high,low,close,volume,amount``（多合约同分片）
- 合约清单：``undl,code,month,type,strike,expire,mult,tick,...``
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from collections import defaultdict
from datetime import date, datetime

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nanobot_quant.ashare.paper_ledger import PaperLedger          # noqa: E402
from nanobot_quant.ashare.paper_strategy import (                  # noqa: E402
    PaperParams,
    can_open,
    days_to_expiry,
    pick_put,
    should_close,
    should_close_high9,
    ARM_A,
    ARM_B,
    ARM_LABELS,
)
from nanobot_quant.ashare.td_panel import setup_by_session         # noqa: E402


# ── 数据载入 ──────────────────────────────────────────────


def load_etf(path: str) -> pd.DataFrame:
    d = pd.read_csv(path, encoding="utf-8-sig")
    d = d.loc[:, ~d.columns.duplicated()]
    d["dt"] = pd.to_datetime(d["stime"].astype(str), format="%Y%m%d%H%M%S")
    # ``setup_by_session`` 按 ``date`` 分组（隔夜从 0 重数），且引擎需要 O/H/L/C/V
    d["date"] = d["dt"].dt.date
    for c in ("open", "volume"):
        if c not in d.columns:
            d[c] = 0.0
    return (d[["dt", "date", "open", "high", "low", "close", "volume"]]
            .sort_values("dt").reset_index(drop=True))


def load_contracts(path: str, underlying: str) -> dict[str, dict]:
    d = pd.read_csv(path, encoding="utf-8-sig")
    d = d[d["undl"] == underlying]
    meta: dict[str, dict] = {}
    for _, r in d.iterrows():
        meta[str(r["code"])] = {
            "code": str(r["code"]),
            "underlying": str(r["undl"]),
            "type": str(r["type"]).upper(),
            "strike": float(r["strike"]),
            "expiry": str(int(r["expire"])),
            "mult": int(r["mult"]),
        }
    return meta


def load_option_closes(opt_dir: str, underlying: str, codes: set[str]) -> dict[str, pd.Series]:
    """把分片里的 1m close 汇成 ``code → Series(index=dt, close)``（已 ffill）。"""
    prefix = underlying.replace(".", "")
    files = sorted(glob.glob(os.path.join(opt_dir, f"opt1m_{prefix}_*.csv")))
    if not files:
        raise SystemExit(f"未找到期权分片：{opt_dir}/opt1m_{prefix}_*.csv")
    frames = []
    for f in files:
        d = pd.read_csv(f, encoding="utf-8-sig", usecols=["ts", "code", "close"])
        d = d[d["code"].isin(codes)]
        frames.append(d)
    if not frames:
        return {}
    d = pd.concat(frames, ignore_index=True)
    d["dt"] = pd.to_datetime(d["ts"].astype(str), format="%Y%m%d%H%M%S")
    out: dict[str, pd.Series] = {}
    for code, g in d.groupby("code", sort=False):
        s = g.set_index("dt")["close"].sort_index()
        s = s[~s.index.duplicated(keep="last")].ffill()
        out[str(code)] = s
    return out


# ── 回放 ──────────────────────────────────────────────────


def run(etf: pd.DataFrame, contracts: dict[str, dict],
        closes: dict[str, pd.Series], *, underlying: str,
        initial: float, params: PaperParams, arm: str = "A") -> dict:
    led = PaperLedger(initial_cash=initial, mult=params.mult,
                      fee_open=params.fee_open)
    puts = {c: m for c, m in contracts.items() if m["type"] == "P" and c in closes}
    if not puts:
        raise SystemExit("合约表里没有可用的 put（检查 contracts 的 type 列与分片覆盖）")

    etf = setup_by_session(etf, cmp_length=4)
    etf["prev_buy"] = etf["buy_setup_count"].shift(1).fillna(0)
    etf["signal"] = (etf["buy_setup_count"] >= 9) & (etf["prev_buy"] < 9)

    # 「一浪」= 该标的从空仓到再次空仓；平仓后清位，允许下一浪再开
    wave_done: set[str] = set()
    stats = defaultdict(int)
    daily: list[dict] = []

    n = len(etf)
    for i in range(1, n):
        row = etf.iloc[i]
        t: datetime = row["dt"]
        spot = float(row["close"])
        today: date = t.date()
        ts = t.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        last_of_day = (i == n - 1) or (etf.iloc[i + 1]["dt"].date() != today)

        # 1) 到期结算（当日最后一根 bar，用收盘价作为结算参考）
        for code in list(led.shorts):
            sp = led.shorts[code]
            if today >= datetime.strptime(sp.expiry, "%Y%m%d").date() and last_of_day:
                r = led.settle_expiry(code=code, settle_spot=spot, ts=ts)
                if r["ok"]:
                    stats[r["fill"]["action"]] += 1
                    wave_done.discard(sp.underlying)
                continue

        # 2) 更新 mark（义务仓按期权现价、现货按 ETF 现价）
        for code in list(led.shorts):
            s = closes.get(code)
            if s is not None:
                px = s.get(t)
                if pd.notna(px) and px > 0:
                    led.mark_short(code, float(px))
        led.mark_etf(underlying, spot)

        # 3) 出场
        for code in list(led.shorts):
            sp = led.shorts[code]
            mark = sp.mark_px or sp.entry_px
            if arm == ARM_B:
                ok, why = should_close_high9(sp.to_dict(), mark, today, params,
                                             sell_setup=int(row.get("sell_setup_count") or 0))
            else:
                ok, why = should_close(sp.to_dict(), mark, today, params)
            if not ok:
                continue
            r = led.buy_close(code=code, bar_close=mark, ts=ts, reason=why)
            if r["ok"]:
                stats["buy_close"] += 1
                if why.startswith("take_profit"):
                    stats["tp"] += 1
                elif why.startswith("high9_profit"):
                    stats["high9"] += 1
                else:
                    stats["dte_force"] += 1
                wave_done.discard(sp.underlying)

        # 4) 信号 → 选档 → 开仓
        if bool(row["signal"]):
            stats["signal"] += 1
            undl_open = [s for s in led.shorts.values() if s.underlying == underlying]
            ok_cnt, _ = can_open([s.to_dict() for s in led.shorts.values()],
                                 underlying, params)
            if not ok_cnt:
                stats["block_quota"] += 1
            elif params.max_per_wave and underlying in wave_done:
                stats["block_wave"] += 1
            else:
                chain = []
                for code, m in puts.items():
                    s = closes.get(code)
                    px = s.get(t) if s is not None else None
                    if px is None or pd.isna(px) or px <= 0:
                        continue
                    chain.append({"code": code, "underlying": m["underlying"],
                                  "strike": m["strike"], "expiry": m["expiry"],
                                  "bid": float(px)})
                pick = pick_put(chain, spot, today, params)
                if pick is None:
                    stats["block_no_candidate"] += 1
                else:
                    r = led.sell_open(code=pick["code"], underlying=underlying,
                                      strike=pick["strike"], expiry=pick["expiry"],
                                      qty=1, bar_close=float(pick["bid"]), ts=ts,
                                      reason=f"buy9 setup={int(row['buy_setup_count'])}")
                    if r["ok"]:
                        stats["sell_open"] += 1
                        wave_done.add(underlying)
                    else:
                        why = r.get("reason", "")
                        stats["block_price" if "成交价" in why else "block_funds"] += 1

        if last_of_day:
            snap = led.snapshot()
            daily.append({"date": today.isoformat(), "equity": snap["equity"],
                          "cash": snap["cash"], "open": len(led.shorts)})

    return {"ledger": led, "stats": dict(stats), "daily": daily}


def _summary(res: dict, params: PaperParams, arm: str = ARM_A) -> str:
    led: PaperLedger = res["ledger"]
    st = res["stats"]
    snap = led.snapshot()
    cons = led.check_consistency()

    closes = [f for f in led.fills if f["action"] == "buy_close"]
    wins = [f for f in closes if (f.get("realized") or 0) > 0]
    assigns = [f for f in led.fills if f["action"] == "assign"]

    lines = [
        "# 模拟账本离线回放（自测）",
        "",
        f"- 出场臂：**{ARM_LABELS.get(arm, arm)}**",
        f"- 初始资金：{snap['initial_cash']:,.2f} 元",
        f"- 期末净值：{snap['equity']:,.2f} 元（{snap['return_pct']:+.4f}%）",
        f"- 已实现：{snap['realized']:,.2f} 元 · 未实现：{snap['unrealized']:,.2f} 元",
        f"- 担保占用：{snap['collateral_locked']:,.2f} 元 · 可用现金：{snap['available_cash']:,.2f} 元",
        f"- 净值一致性自检：{'✅ 通过' if cons['ok'] else '❌ 不符'}"
        f"（{cons['equity_minus_initial']:+.2f} vs {cons['realized_plus_unrealized']:+.2f}）",
        "",
        "## 动作统计",
        "",
        f"| 项 | 次数 |",
        f"|:--|--:|",
        f"| TD 买9 信号 | {st.get('signal', 0)} |",
        f"| 卖出开仓 | {st.get('sell_open', 0)} |",
        f"| 买入平仓 | {st.get('buy_close', 0)} |",
        (f"| ↳ 止盈 | {st.get('tp', 0)} |" if arm == ARM_A
         else f"| ↳ 高9 + 浮盈 | {st.get('high9', 0)} |"),
        f"| ↳ DTE 强平 | {st.get('dte_force', 0)} |",
        f"| 行权接货 | {st.get('assign', 0)} |",
        f"| 到期作废 | {st.get('settle_otm', 0)} |",
        f"| 拦：额度 | {st.get('block_quota', 0)} |",
        f"| 拦：同浪 | {st.get('block_wave', 0)} |",
        f"| 拦：无合适合约 | {st.get('block_no_candidate', 0)} |",
        f"| 拦：资金不足 | {st.get('block_funds', 0)} |",
        f"| 拦：成交价非正 | {st.get('block_price', 0)} |",
        "",
    ]
    if closes:
        avg = sum((f.get("realized") or 0) for f in closes) / len(closes)
        lines += [
            "## 平仓统计",
            "",
            f"- 平仓笔数：{len(closes)}（胜 {len(wins)} / 负 {len(closes) - len(wins)}，"
            f"胜率 {len(wins) / len(closes) * 100:.1f}%）",
            f"- 每笔已实现均值：{avg:+.2f} 元/张",
            "",
        ]
    if assigns:
        lines.append(f"- 接货合计：{sum(f['assign_cost'] for f in assigns):,.2f} 元的 ETF")
        lines.append("")
    lines += ["## 成交明细（前 20 条）", "",
              "| 时间 | 动作 | 合约 | 行权价 | 张数 | 价 | 已实现 | 原因 |",
              "|:--|:--|:--|--:|--:|--:|--:|:--|"]
    for f in led.fills[:20]:
        lines.append(
            f"| {f['ts'][:16]} | {f['action']} | {f['code']} | "
            f"{f.get('strike', '')} | {f.get('qty', '')} | {f.get('px', '')} | "
            f"{f.get('realized', '')} | {f.get('reason') or f.get('note', '')} |")
    lines += ["", f"（共 {len(led.fills)} 条成交，此处截取前 20 条）", ""]
    if res["daily"]:
        lines += ["## 按日净值（末 10 个交易日）", "",
                  "| 日期 | 净值 | 现金 | 在仓 |", "|:--|--:|--:|--:|"]
        for d in res["daily"][-10:]:
            lines.append(f"| {d['date']} | {d['equity']:,.2f} | {d['cash']:,.2f} | {d['open']} |")
    lines += [
        "",
        "## 口径与数据限制（读数前必看）",
        "",
        "- **bid 用 1m close 代理**：免费通道没有历史盘口，期权分钟线又是「无成交沿用上一价」，",
        "  流动性差的深虚值合约报价会偏乐观；实盘下单前必须看真盘口。",
        "- **到期窗口过滤是主要拦截源**：A 股 ETF 期权同时只有 4 个月份，",
        "  「拦：无合适合约」多数来自所选到期档不在 [min_dte, max_dte] 窗口内，",
        "  不是选档 bug —— 查看该时段实际在售月份的 DTE 即可确认。",
        "- **接货路径本窗口无样本**：导出数据止于 2026-11-19、最近到期日为 11-25，",
        "  所以「行权接货 / 到期作废」次数为 0 属预期，这两条路由单测覆盖。",
        "- **本报告不是策略结论**：只验证账本记账与规则链路，不做参数裁决。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="A 股 ETF 期权模拟账本离线回放")
    ap.add_argument("--etf-csv", required=True)
    ap.add_argument("--opt-dir", required=True)
    ap.add_argument("--contracts", required=True)
    ap.add_argument("--underlying", default="510050.SH")
    ap.add_argument("--initial", type=float, default=500_000.0)
    ap.add_argument("--tp", type=float, default=PaperParams.take_profit_pct)
    ap.add_argument("--force-dte", type=int, default=PaperParams.force_close_dte)
    ap.add_argument("--min-dte", type=int, default=PaperParams.min_dte)
    ap.add_argument("--max-dte", type=int, default=PaperParams.max_dte)
    ap.add_argument("--out", default="", help="写 markdown 到该路径（默认打印）")
    ap.add_argument("--arm", choices=["a", "b", "both"], default="a",
                    help="出场臂：a=止盈 50%%（默认）、b=D 方案（高9+浮盈）、both=两臂对照")
    a = ap.parse_args()

    for p in (a.etf_csv, a.opt_dir, a.contracts):
        if not os.path.exists(p):
            raise SystemExit(f"路径不存在：{p}")

    params = PaperParams(take_profit_pct=a.tp, force_close_dte=a.force_dte,
                         min_dte=a.min_dte, max_dte=a.max_dte)
    etf = load_etf(a.etf_csv)
    contracts = load_contracts(a.contracts, a.underlying)
    codes = {c for c, m in contracts.items() if m["type"] == "P"}
    closes = load_option_closes(a.opt_dir, a.underlying, codes)
    print(f"[replay] ETF bars={len(etf)} 合约={len(contracts)} put 有行情="
          f"{len([c for c in codes if c in closes])}", file=sys.stderr)

    if a.arm == "both":
        arms = [ARM_A, ARM_B]
        parts: list[str] = []
        rows: list[tuple] = []
        for arm in arms:
            res = run(etf, contracts, closes, underlying=a.underlying,
                      initial=a.initial, params=params, arm=arm)
            parts.append(_summary(res, params, arm))
            snap = res["ledger"].snapshot()
            closes_all = [f for f in res["ledger"].fills if f["action"] == "buy_close"]
            wins = [f for f in closes_all if (f.get("realized") or 0) > 0]
            rows.append((arm, snap, len(res["ledger"].fills), len(closes_all), len(wins)))
        head = ["# 模拟账本两臂对照", "",
                "两臂共用同一份 ETF 1m 与期权行情、同一套入场与选档规则，**只差止盈条件**：",
                f"- A = {ARM_LABELS[ARM_A]}", f"- B = {ARM_LABELS[ARM_B]}", "",
                "| 臂 | 净值 | 收益率 | 已实现 | 未实现 | 成交笔数 | 平仓笔数 | 胜 | 胜率 |",
                "|:--|--:|--:|--:|--:|--:|--:|--:|--:|"]
        for arm, snap, nfill, nclose, nwin in rows:
            wr = (nwin / nclose * 100) if nclose else 0.0
            head.append(f"| {arm} | {snap['equity']:,.2f} | {snap['return_pct']:+.4f}% | "
                        f"{snap['realized']:,.2f} | {snap['unrealized']:,.2f} | {nfill} | "
                        f"{nclose} | {nwin} | {wr:.1f}% |")
        head += ["", "---", ""]
        md = "\n".join(head) + "\n\n".join(parts)
    else:
        arm = ARM_B if a.arm == "b" else ARM_A
        res = run(etf, contracts, closes, underlying=a.underlying,
                  initial=a.initial, params=params, arm=arm)
        md = _summary(res, params, arm)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"[replay] 已写入 {a.out}", file=sys.stderr)
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
