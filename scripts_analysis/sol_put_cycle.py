"""买9 后卖 put 循环损益模拟（基于真实 SOL 4H 历史 62 个买9 事件 + 当前真实期权定价）

简化假设（无历史期权链数据，权利金用当前市场 bid 水平代理）：
- 4H 买9 信号 → 立即卖 6 天到期 put（≈9/11 档），strike = P0×strike_pct
- 卖价 = bid（当前市场同 OTM 水平 bid/名义）
- 持有到期：期末（事件后 ~6 天，36×4h 根尾收盘）> strike → 全收；< strike → 亏 (strike-期末)
- 不计手续费/资金成本；每张名义 P0×0.1
输出：毛权利金、被行权率、平均单循环净收益、年化（按 14.7 天/循环）
"""
import importlib.util, json, time, urllib.request
import numpy as np, pandas as pd

_spec = importlib.util.spec_from_file_location(
    "td_sequential_mod", "src/nanobot_quant/strategies/td_sequential.py")
tdseq = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(tdseq)
from nanobot_quant.td_params import DEFAULT_TD_PARAMS

def fetch_gate(pair, interval, max_rows):
    out, to = [], ""
    while len(out) < max_rows:
        url = (f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={pair}"
               f"&interval={interval}&limit=1000" + (f"&to={to}" if to else ""))
        req = urllib.request.Request(url, headers={"User-Agent": "nanobot"})
        b = json.load(urllib.request.urlopen(req, timeout=25))
        if not b: break
        out += b
        if len(b) < 1000: break
        to = str(int(b[0][0]) - 1); time.sleep(0.25)
    raw = pd.DataFrame(out, columns=["ts","qv","close","high","low","open","bv","closed"])
    raw["ts"] = raw["ts"].astype(int)
    raw = raw.drop_duplicates("ts").sort_values("ts")
    df = pd.DataFrame({
        "Open": raw["open"].astype(float).values, "High": raw["high"].astype(float).values,
        "Low": raw["low"].astype(float).values, "Close": raw["close"].astype(float).values,
        "Volume": raw["bv"].astype(float).values},
        index=pd.to_datetime(raw["ts"], unit="s", utc=True))
    return df.tail(max_rows)

df = fetch_gate("SOL_USDT", "4h", 5500)
eng = tdseq._DeMarkEngine(df, DEFAULT_TD_PARAMS); eng.run_all(0)
d = eng.df
sb = d["buy_setup_count"].astype(int).values
close = d["Close"].values; n = len(d)
events = [i for i in range(1, n) if sb[i] == 9 and sb[i-1] < 9]
print(f"买9 事件 {len(events)} 个（近 915 天）\n")

# 当前市场 bid 权利金档（%/名义，SOL 6DTE 链实测 2026-09-05）：strike_pct → bid_pct
bid_scale = {0.97: 0.90, 0.95: 0.50, 1.00: 1.55}   # 6DTE bid/名义%（ATM≈1.55%、-3%≈0.90%、-5%≈0.50%）
fwd_bars = 36   # ~6 天（36×4h）

for sp, prem in bid_scale.items():
    net, exercised, worst = [], 0, 0
    for i in events:
        j = min(i + fwd_bars, n - 1)
        if j <= i: continue
        p0 = close[i]
        se = close[j]                      # 期末收盘（≈到期结算价代理）
        strike = p0 * sp
        got = prem / 100                   # 已收权利金（名义比例）
        if se >= strike:                   # 到期 OTM 全收
            pnl = got
        else:                              # 到期 ITM 被行权
            exercised += 1
            pnl = got - (strike - se) / p0 # 结算亏（名义比例）
        net.append(pnl)
        worst = min(worst, pnl)
    net = np.array(net)
    print(f"===== 卖 {sp*100:.0f}% strike（~{sp*100-100:+.0f}% OTM）6DTE，bid 收 {prem:.2f}%/名义 =====")
    print(f"  被行权率（期末<strike）: {exercised}/{len(net)} = {exercised/len(net)*100:.0f}%")
    print(f"  单循环净收益: 平均 {net.mean()*100:+.3f}% 名义 | 中位 {np.median(net)*100:+.3f}% | 最坏 {worst*100:+.2f}%")
    print(f"  年化估算（按 {14.7:.1f} 天/循环 ≈ {365/14.7:.0f} 循环）: {net.mean()*365/14.7*100:+.1f}%/年")
    print(f"  收益分布: >0 {(net>0).mean()*100:.0f}% | >+0.5% {(net>0.005).mean()*100:.0f}% | <-1% {(net<-0.01).mean()*100:.0f}%")
    print()
