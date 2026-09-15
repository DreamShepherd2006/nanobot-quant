"""买9 vs cd13 卖 put 信号对比（SOL 1H，近 ~400 天）
事件：setup_buy 首达 9（买9）；cd_buy 首达 13（cd13，含经 setup9 的完整衰竭）
评估：未来 3/7 天最大回撤 + 期末收盘（卖 −3%/−5% strike 到期被行权率代理）
"""
import json, time, urllib.request, importlib.util
import numpy as np, pandas as pd

_spec = importlib.util.spec_from_file_location('tdseq','src/nanobot_quant/strategies/td_sequential.py')
tdseq = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(tdseq)
from nanobot_quant.td_params import DEFAULT_TD_PARAMS

def fetch(pair, interval, n):
    out, to = [], ""
    while len(out) < n:
        url=f'https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={pair}&interval={interval}&limit=1000'+(f'&to={to}' if to else '')
        b=json.load(urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'nb'}),timeout=25))
        if not b: break
        out+=b
        if len(b)<1000: break
        to=str(int(b[0][0])-1); time.sleep(0.2)
    raw=pd.DataFrame(out,columns=['ts','qv','close','high','low','open','bv','closed'])
    raw['ts']=raw['ts'].astype(int)
    raw=raw.drop_duplicates('ts').sort_values('ts')
    df=pd.DataFrame({'Open':raw['open'].astype(float).values,'High':raw['high'].astype(float).values,
        'Low':raw['low'].astype(float).values,'Close':raw['close'].astype(float).values,'Volume':raw['bv'].astype(float).values},
        index=pd.to_datetime(raw['ts'],unit='s',utc=True))
    return df.tail(n)

df=fetch('SOL_USDT','1h',9000)
e=tdseq._DeMarkEngine(df,DEFAULT_TD_PARAMS); e.run_all(0); d=e.df
sb=d['buy_setup_count'].astype(int).values; cb=d['buy_countdown_count'].astype(int).values
close=d['Close'].values; n=len(d)
buy9=[i for i in range(1,n) if sb[i]==9 and sb[i-1]<9]
cd13=[i for i in range(1,n) if cb[i]==13 and cb[i-1]<13]
print(f'SOL 1H {n} 根（{d.index[0]:%Y-%m-%d} → {d.index[-1]:%Y-%m-%d}）')
print(f'买9 事件 {len(buy9)} 个 | cd13 事件 {len(cd13)} 个（cd13 中独立于买9 尾部确认的后续衰竭）\n')

def stat(events, label):
    spans=np.diff([d.index[i].value for i in events])/86400e9
    print(f'── {label}（{len(events)} 事件，均间隔 {spans.mean():.0f} 天）──')
    for fwd in (72, 168):
        dd,up,se=[],[],[]
        for i in events:
            j=min(i+fwd,n-1)
            seg=close[i+1:j+1] if j>i else np.array([])
            if len(seg)==0: continue
            p0=close[i]
            dd.append((seg.min()-p0)/p0); up.append((seg.max()-p0)/p0); se.append((close[j]-p0)/p0)
        dd,up,se=np.array(dd),np.array(up),np.array(se)
        days=fwd/24
        print(f'  {days:.0f}天: 创新低 {(dd<0).mean()*100:.0f}% | 最深中位 {np.median(dd)*100:.1f}% | 期末收盘<-3% {(se<-0.03).mean()*100:.0f}% | <-5% {(se<-0.05).mean()*100:.0f}% | 反弹≥2% {(up>0.02).mean()*100:.0f}%')
    print()

stat(buy9,'买9（setup_buy 首达 9）')
stat(cd13,'cd13（cd_buy 首达 13）')
# cd13 去掉与买9 重叠样本后（cd13 前 24h 内无买9 的独立样本）
overlap=sum(1 for i in cd13 if any(i-j<24 and i>j for j in buy9))
print(f'cd13 中前 24h 内无买9 的独立样本: {len(cd13)-overlap}/{len(cd13)}')
