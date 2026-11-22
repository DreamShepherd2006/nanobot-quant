#coding:gbk
# -*- coding: gbk -*-
"""
QMT 沙箱自检 v4 —— 修正 v3 的三处调用错误，只测还没拿到真值的项
已确认（不用再测）：account/accountType 注入 OK；socket 被拦；文件不能追加；
                   run_time 5s 定时器 OK；get_option_list('YYYYMM') OK；detail 字段 OK
本版目标：① get_market_data_ex 正确签名 ② get_trade_detail_data 全局调用
          ③ 文件只读/覆盖 ④ 期权自身盘口 ⑤ 期权 IV / 标的期权列表
输出 ≤ 15 行
"""

ACCOUNT = ''
ACCOUNT_TYPE = 'STOCK_OPTION'
OPT_UNDL = '510050.SH'
OPT_MONTH = '202612'
LOG_DIR = r'D:\qmt_probe'


def P(tag, msg):
    try:
        print('[Q%s] %s' % (tag, msg))
    except BaseException:
        pass


def safe(tag, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        P(tag, 'EXC %s: %s' % (type(e).__name__, str(e)[:160]))
        return None


def gv(o, n, d='NOATTR'):
    try:
        return getattr(o, n, d)
    except BaseException as e:
        return 'EXC(%s)' % type(e).__name__


def acct():
    return ACCOUNT or (globals().get('account') or '')


# ---------- 文件：覆盖 / 读 / 目录 ----------
def probe_file():
    p = LOG_DIR + '\\probe_v4.log'
    r = []
    try:
        f = open(p, 'w')
        f.write('hello-v4\n')
        f.close()
        r.append('w=OK')
    except BaseException as e:
        r.append('w=%s' % type(e).__name__)
    try:
        f = open(p, 'r')
        txt = f.read().strip()
        f.close()
        r.append('r=OK(%r)' % txt)
    except BaseException as e:
        r.append('r=%s' % type(e).__name__)
    try:
        import os
        r.append('exists=%s' % os.path.exists(p))
        r.append('listdir=%d' % len(os.listdir(LOG_DIR)))
    except BaseException as e:
        r.append('os=%s' % type(e).__name__)
    P('02', 'FILE v4 ' + '  '.join(r))


# ---------- 交易明细：全局函数 ----------
def tdetail(C, kind):
    f = globals().get('get_trade_detail_data')
    src = 'global'
    if f is None:
        f = getattr(C, 'get_trade_detail_data', None)
        src = 'C.attr'
    if f is None:
        P('14', 'get_trade_detail_data 不存在（global/C 都没有）')
        return None
    try:
        return f(acct(), ACCOUNT_TYPE, kind)
    except BaseException as e:
        P('14', 'TDETAIL(%s,%s) %s: %s' % (src, kind, type(e).__name__, str(e)[:120]))
        return None


# ---------- K 线：多种签名挨个试 ----------
def probe_kline(C, code):
    forms = [
        ('9pos', lambda: C.get_market_data_ex(
            ['open', 'high', 'low', 'close', 'volume'], [code], '1m', '', '', 5, 'none', True, True)),
        ('8pos', lambda: C.get_market_data_ex(
            ['close'], [code], '1m', '', '', 5, 'none', True)),
        ('kw', lambda: C.get_market_data_ex(
            fields=['open', 'high', 'low', 'close', 'volume'], stock_list=[code],
            period='1m', count=5, dividend_type='none', fill_data=True)),
        ('4pos', lambda: C.get_market_data_ex(['close'], [code], '1m', 5)),
    ]
    for name, fn in forms:
        try:
            d = fn()
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            P('10', 'KLINE %s FAIL %s' % (name, type(e).__name__))
            continue
        try:
            df = d.get(code) if isinstance(d, dict) else None
            if df is None:
                P('10', 'KLINE %s OK 但取不到 %s  type=%s' % (name, code, type(d).__name__))
                return
            P('10', 'KLINE %s OK  shape=%s cols=%s idx_tail=%s close_last=%r' % (
                name, df.shape, list(df.columns), str(list(df.index[-1:])),
                float(df['close'].iloc[-1]) if 'close' in df.columns else None))
            return
        except BaseException as e:
            P('10', 'KLINE %s 解析失败 %s' % (name, type(e).__name__))


def init(C):
    P('00', 'v4 start | backtest=%r | acct=%r' % (gv(C, 'do_back_test'), acct()))
    P('00', 'global fn: get_trade_detail_data=%r passorder=%r get_market_data_ex=%r' % (
        'get_trade_detail_data' in globals(),
        'passorder' in globals(),
        hasattr(C, 'get_market_data_ex')))
    probe_file()


_BAKED = [False]


def handlebar(C):
    if gv(C, 'do_back_test') is True:
        if _BAKED[0]:
            return
        _BAKED[0] = True
    else:
        try:
            if not C.is_last_bar():
                return
        except BaseException:
            pass

    # 合约列表
    lst = safe('11', C.get_option_list, OPT_UNDL, OPT_MONTH, '', True)
    if lst is None:
        lst = safe('11', C.get_option_list, OPT_UNDL, OPT_MONTH, None, True)
    code = None
    try:
        if lst:
            code = list(lst)[0]
            P('11', 'OPTLIST count=%d first=%s' % (len(lst), code))
    except BaseException:
        pass

    # P10 K 线（标的现货）
    probe_kline(C, OPT_UNDL)

    # P13 盘口：标的 + 期权合约
    for icode, tag in [(OPT_UNDL, 'UNDL'), (code, 'OPT')]:
        if not icode:
            continue
        t = safe('13', C.get_full_tick, [icode])
        if isinstance(t, dict) and t:
            v = t.get(list(t.keys())[0]) or {}
            if isinstance(v, dict):
                b = v.get('bidPrice')
                a = v.get('askPrice')
                P('13', 'TICK %s %s keys=%d last=%r bid1=%r ask1=%r' % (
                    tag, icode, len(v), v.get('lastPrice'),
                    b[0] if isinstance(b, list) and b else b,
                    a[0] if isinstance(a, list) and a else a))

    # P18 期权 IV / 标的期权品种
    if code:
        iv = safe('18', C.get_option_iv, code)
        P('18', 'OPT_IV %s = %r' % (code, iv))
    ud = safe('18', C.get_option_undl_data, OPT_UNDL)
    try:
        P('18', 'UNDL_DATA count=%s head=%s' % (len(ud), str(list(ud)[:3])))
    except BaseException:
        pass

    # P14-P17 交易数据（全局函数）
    pos = tdetail(C, 'position')
    if pos is not None:
        P('14', 'POS count=%d' % len(pos))
        for p in list(pos)[:2]:
            P('14', '  %s vol=%r canuse=%r open=%r side=%r margin=%r cov=%r' % (
                gv(p, 'm_strInstrumentID'), gv(p, 'm_nVolume'), gv(p, 'm_nCanUseVolume'),
                gv(p, 'm_dOpenPrice'), gv(p, 'm_eSideFlag'), gv(p, 'm_dRealUsedMargin'),
                gv(p, 'm_nCoveredVolume')))

    dl = tdetail(C, 'DEAL')
    if dl is not None:
        P('15', 'DEAL count=%d' % len(dl))
        for d in list(dl)[-2:]:
            P('15', '  %s px=%r vol=%r amt=%r fee=%r' % (
                gv(d, 'm_strInstrumentID'), gv(d, 'm_dPrice'), gv(d, 'm_nVolume'),
                gv(d, 'm_dTradeAmount'), gv(d, 'm_dComssion')))

    od = tdetail(C, 'ORDER')
    if od is not None:
        P('16', 'ORDER count=%d' % len(od))

    acc = tdetail(C, 'account')
    if acc is not None and len(acc) > 0:
        a = list(acc)[0]
        P('17', 'ACCOUNT balance=%r avail=%r frozen=%r' % (
            gv(a, 'm_dBalance'), gv(a, 'm_dAvailable'), gv(a, 'm_dFrozenCash')))

    P('99', 'done')
