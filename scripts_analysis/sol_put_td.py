"""SOL 卖 put × TD 买9 循环策略评估（实证第一层：信号频率 + 买9 后回撤分布）
数据：Gate SOL_USDT 4h/1D；信号：原版 TD（setup_period=9, compare_length=4）
买9 = buy_setup_count 首达 9（9 后累加不重复计，重置后再计）
"""
import json, time, urllib.request
import importlib.util
import numpy as np, pandas as pd

# 直接按文件加载（strategies/__init__ 拉 lumibot，Nightly 容器未装——绕开）
_spec = importlib.util.spec_from_file_location(
    "td_sequential_mod", "src/nanobot_quant/strategies/td_sequential.py")
tdseq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tdseq)
from nanobot_quant.td_params import DEFAULT_TD_PARAMS

def fetch_gate(pair, interval, max_rows):
    out, to = [], ""
    while len(out) < max_rows:
        url = (f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={pair}"
               f"&interval={interval}&limit=1000" + (f"&to={to}" if to else ""))
        req = urllib.request.Request(url, headers={"User-Agent": "nanobot"})
        b = json.load(urllib.request.urlopen(req, timeout=25))
        if not b:
            break
        out += b
        if len(b) < 1000:
            break
        to = str(int(b[0][0]) - 1)
        time.sleep(0.25)
    raw = pd.DataFrame(out, columns=["ts", "qv", "close", "high", "low", "open", "bv", "closed"])
    raw["ts"] = raw["ts"].astype(int)
    raw = raw.drop_duplicates("ts").sort_values("ts")
    df = pd.DataFrame({
        "Open": raw["open"].astype(float).values,
        "High": raw["high"].astype(float).values,
        "Low": raw["low"].astype(float).values,
        "Close": raw["close"].astype(float).values,
        "Volume": raw["bv"].astype(float).values,
    }, index=pd.to_datetime(raw["ts"], unit="s", utc=True))
    return df.tail(max_rows)

def analyze(df, label, fwd_bars_list, hours_per_bar):
    eng = tdseq._DeMarkEngine(df, DEFAULT_TD_PARAMS)
    eng.run_all(0)
    d = eng.df
    sb = d["buy_setup_count"].astype(int).values
    close = d["Close"].values
    n = len(d)
    events = [i for i in range(1, n) if sb[i] == 9 and sb[i-1] < 9]
    print(f"\n===== {label}（{n} 根，{d.index[0]:%Y-%m-%d} → {d.index[-1]:%Y-%m-%d}）=====")
    print(f"买9 事件数: {len(events)}")
    if len(events) >= 2:
        spans = np.diff([d.index[i].value for i in events]) / 86400e9
        print(f"事件平均间隔: {spans.mean():.1f} 天（中位 {np.median(spans):.0f} 天 | 最短 {spans.min():.0f} 天 | 最长 {spans.max():.0f} 天）")
    for fwd in fwd_bars_list:
        dd, up = [], []
        for i in events:
            j = min(i + fwd, n - 1)
            seg = close[i+1:j+1] if j > i else np.array([])
            if len(seg) == 0:
                continue
            p0 = close[i]
            dd.append((seg.min() - p0) / p0)
            up.append((seg.max() - p0) / p0)
        dd, up = np.array(dd), np.array(up)
        days = fwd * hours_per_bar / 24
        dname = f"{days:g} 天" if days >= 1 else f"{days*24:g} 小时"
        print(f"\n买9 后 {dname}（{fwd} 根，{len(dd)} 样本）:")
        if len(dd):
            print(f"  最大回撤: 中位 {np.median(dd)*100:.2f}% | 25分位 {np.percentile(dd,25)*100:.2f}% | 10分位 {np.percentile(dd,10)*100:.2f}%")
            for lev in (0.0, 0.02, 0.05, 0.10):
                print(f"  跌破 {lev*100:.0f}%: {(dd < -lev).mean()*100:.1f}%")
            print(f"  反弹 ≥+2%: {(up > 0.02).mean()*100:.1f}% | ≥+5%: {(up > 0.05).mean()*100:.1f}%")
    return events, d

if __name__ == "__main__":
    df4 = fetch_gate("SOL_USDT", "4h", 5500)      # ~915 天
    df1 = fetch_gate("SOL_USDT", "1d", 1400)      # ~1400 天
    analyze(df4, "SOL 4H（近 915 天）", (6, 18, 42), 4)
    analyze(df1, "SOL 1D（近 1400 天）", (3, 7), 24)
    opt = df4[df4.index >= pd.Timestamp("2026-05-18", tz="UTC")]
    analyze(opt, "SOL 4H·期权上市以来（2026-05-18→ 仅 3.7 个月，样本小）", (18, 42), 4)
