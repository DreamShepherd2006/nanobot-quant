#coding:gbk
# -*- coding: gbk -*-
"""
QMT 沙箱自检 v3（紧凑版）—— 只读探针，不下单，输出压到 20 行以内
用法：新建 Python 策略粘贴本文件 → 主图 510050.SH / 1 分钟 → 调试运行
回贴：日志里 [P 开头的全部行（大约 20 行就完了）
"""

ACCOUNT = ''
ACCOUNT_TYPE = 'STOCK_OPTION'
OPT_UNDL = '510050.SH'
OPT_MONTH = '202612'
LOG_DIR = r'D:\qmt_probe'
TIMER_SEC = 5

_MODS = {}


def imp(n):
    try:
        return __import__(n)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        _MODS[n] = 'FAIL(%s)' % type(e).__name__
        return None


os = imp('os')
sys = imp('sys')
time = imp('time')
socket = imp('socket')
threading = imp('threading')
tornado = imp('tornado')
urllib = imp('urllib')

_T0 = [0.0]
_TICK = [0]


def P(tag, msg):
    try:
        print('[P%s] %s' % (tag, msg))
    except BaseException:
        pass


def safe(tag, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        P(tag, 'EXC %s: %s' % (type(e).__name__, e))
        return None


def gv(o, n, d='NOATTR'):
    try:
        return getattr(o, n, d)
    except BaseException as e:
        return 'EXC(%s)' % type(e).__name__


def acct():
    return ACCOUNT or (globals().get('account') or '')


def probe_mods():
    names = ['os', 'sys', 'time', 'socket', 'threading', 'tornado', 'urllib']
    parts = []
    for n in names:
        r = _MODS.get(n)
        parts.append('%s=%s' % (n, r if r else 'OK'))
    P('01', 'MODS ' + '  '.join(parts))


def probe_file():
    for d in [LOG_DIR]:
        r1 = 'n/a'
        try:
            if not os.path.isdir(d):
                os.makedirs(d)
            f = open(d + '\\probe.log', 'a')
            f.write('x\n')
            f.close()
            f = open(d + '\\probe.log', 'r')
            n = len(f.readlines())
            f.close()
            r1 = 'append+read OK(%d lines)' % n
        except BaseException as e:
            r1 = 'FAIL %s' % type(e).__name__
        r2 = 'n/a'
        try:
            f = open(d + '\\probe_w.log', 'w')
            f.write('x\n')
            f.close()
            r2 = 'overwrite OK'
        except BaseException as e:
            r2 = 'FAIL %s' % type(e).__name__
        P('02', 'FILE %s | %s | %s' % (d, r1, r2))


def probe_socket():
    if socket is None:
        P('03', 'SOCKET skip(模块不可用) -> 服务路线不成立')
        return
    res = []
    for host in ('127.0.0.1', '0.0.0.0'):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind((host, 8712))
            s.listen(2)
            res.append('%s=OK' % host)
        except BaseException as e:
            res.append('%s=FAIL(%s)' % (host, type(e).__name__))
        finally:
            try:
                if s is not None:
                    s.close()
            except BaseException:
                pass
    P('03', 'SOCKET bind ' + '  '.join(res))


def probe_timer(ContextInfo):
    _TICK[0] += 1
    el = (time.time() - _T0[0]) if time else 0
    P('20', 'TIMER tick=%d elapsed=%.0fs' % (_TICK[0], el))


def init(C):
    _T0[0] = time.time() if time else 0
    P('00', 'v3 start | backtest=%r | acct=%r | injected=%r / %r' % (
        gv(C, 'do_back_test'), acct(), globals().get('account'), globals().get('accountType')))
    probe_mods()
    probe_file()
    probe_socket()
    try:
        C.run_time('probe_timer', '%snSecond' % TIMER_SEC, '2026-01-05 09:15:00', 'SH')
        P('06', 'TIMER registered %ss（回测下无效）' % TIMER_SEC)
    except BaseException as e:
        P('06', 'TIMER REGISTER FAIL %s' % type(e).__name__)


_BAKED = [False]
_KEYS = ['OptExercisePrice', 'ExpireDate', 'OptUnit', 'VolumeMultiple',
         'PriceTick', 'ShortMarginRatio', 'MinLimitOrderVolume', 'OptUndlCode']


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

    data = safe('10', C.get_market_data_ex, ['close'], [OPT_UNDL], '1m', 5)
    if data is not None:
        try:
            df = data.get(OPT_UNDL)
            P('10', 'KLINE shape=%s cols=%s idx_tail=%s' % (
                df.shape, list(df.columns), str(list(df.index[-1:]))))
        except BaseException as e:
            P('10', 'KLINE parse %s' % type(e).__name__)

    lst = safe('11', C.get_option_list, OPT_UNDL, OPT_MONTH, '', True)
    if lst is None:
        lst = safe('11', C.get_option_list, OPT_UNDL, OPT_MONTH, None, True)
    if lst is not None:
        try:
            P('11', 'OPTLIST %s count=%d head=%s' % (OPT_MONTH, len(lst), str(list(lst)[:3])))
        except BaseException:
            P('11', 'OPTLIST parse FAIL')

    if lst:
        code = list(lst)[0]
        d = safe('12', C.get_option_detail_data, code)
        if isinstance(d, dict):
            kv = ['%s=%r' % (k, d.get(k)) for k in _KEYS]
            P('12', 'DETAIL %s | %s' % (code, '  '.join(kv)))
        else:
            P('12', 'DETAIL not dict %s' % type(d).__name__)

    t = safe('13', C.get_full_tick, [OPT_UNDL])
    if isinstance(t, dict) and t:
        v = t.get(list(t.keys())[0]) or {}
        if isinstance(v, dict):
            b = v.get('bidPrice')
            a = v.get('askPrice')
            P('13', 'TICK keys=%d last=%r bid1=%r ask1=%r' % (
                len(v), v.get('lastPrice'),
                b[0] if isinstance(b, list) and b else b,
                a[0] if isinstance(a, list) and a else a))

    pos = safe('14', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'position')
    if pos is not None:
        P('14', 'POS count=%d' % len(pos))
        for p in list(pos)[:2]:
            P('14', '  %s vol=%r canuse=%r open=%r side=%r margin=%r cov=%r' % (
                gv(p, 'm_strInstrumentID'), gv(p, 'm_nVolume'), gv(p, 'm_nCanUseVolume'),
                gv(p, 'm_dOpenPrice'), gv(p, 'm_eSideFlag'), gv(p, 'm_dRealUsedMargin'),
                gv(p, 'm_nCoveredVolume')))

    dl = safe('15', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'DEAL')
    if dl is not None:
        P('15', 'DEAL count=%d' % len(dl))
        for d in list(dl)[-2:]:
            P('15', '  %s px=%r vol=%r amt=%r fee=%r' % (
                gv(d, 'm_strInstrumentID'), gv(d, 'm_dPrice'), gv(d, 'm_nVolume'),
                gv(d, 'm_dTradeAmount'), gv(d, 'm_dComssion')))

    od = safe('16', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'ORDER')
    if od is not None:
        P('16', 'ORDER count=%d' % len(od))
        for o in list(od)[-2:]:
            P('16', '  %s status=%r traded=%r/%r px=%r remark=%r' % (
                gv(o, 'm_strInstrumentID'), gv(o, 'm_nOrderStatus'), gv(o, 'm_nVolumeTraded'),
                gv(o, 'm_nVolumeTotalOriginal'), gv(o, 'm_dLimitPrice'), gv(o, 'm_strRemark')))

    acc = safe('17', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'account')
    if acc is not None and len(acc) > 0:
        a = list(acc)[0]
        P('17', 'ACCOUNT balance=%r avail=%r frozen=%r' % (
            gv(a, 'm_dBalance'), gv(a, 'm_dAvailable'), gv(a, 'm_dFrozenCash')))

    P('99', 'done')
