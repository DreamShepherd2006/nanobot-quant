#coding:gbk
# -*- coding: gbk -*-
"""
QMT 沙箱自检策略 v2 —— 只读探针，不含任何 passorder 下单调用
v2 变更：所有 import 改为逐项探测（v1 的顶层 import socket 被拦后整个策略直接崩溃）

运行步骤
  1) 新建 Python 策略，粘贴本文件全部代码，保存
  2) 参数面板：ACCOUNT 填资金账号（可留空，代码会用界面注入的 account）
  3) 主图添加 510050.SH、周期 1 分钟
  4) 运行模式「调试运行」或「模拟信号」均可（不下单）
  5) 把日志里所有 [P 开头的行整段复制回来
"""

# ======================= 参数（变量名 = 参数名） =======================
ACCOUNT = ''
ACCOUNT_TYPE = 'STOCK_OPTION'
OPT_UNDL = '510050.SH'
OPT_MONTH = '202612'
LOG_DIR = r'D:\qmt_probe'
HTTP_PORT = 8712
TIMER_SEC = 5

# ======================= 模块探测（每个独立 try，绝不连坐） =======================
_MODS = {}


def imp(name):
    """尝试 import，成功返回 module，失败返回 None 并记账。单项失败不影响其他项。"""
    try:
        m = __import__(name)
        _MODS[name] = 'OK'
        return m
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        try:
            _MODS[name] = 'FAIL %s: %s' % (type(e).__name__, e)
        except BaseException:
            _MODS[name] = 'FAIL 无法描述'
        return None


os = imp('os')
sys = imp('sys')
datetime = imp('datetime')
time = imp('time')
json = imp('json')
math = imp('math')
socket = imp('socket')
threading = imp('threading')
tornado = imp('tornado')
httpserver = imp('http.server')
urllib = imp('urllib')

_T0 = [0.0]
_TICK = [0]
_BAKED = [False]


def now_str():
    try:
        if datetime is not None:
            return datetime.datetime.now().strftime('%H:%M:%S')
    except BaseException:
        pass
    try:
        if time is not None:
            return time.strftime('%H:%M:%S')
    except BaseException:
        pass
    return '--:--:--'


def mono():
    try:
        if time is not None:
            return time.time()
    except BaseException:
        pass
    return 0.0


def P(tag, msg):
    try:
        line = '[P%s] %s | %s' % (tag, now_str(), msg)
        print(line)
    except BaseException as e:
        try:
            print('[P%s] PRINT-FAIL %s' % (tag, type(e).__name__))
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


def acct():
    a = ACCOUNT
    if not a:
        try:
            a = globals().get('account') or ''
        except BaseException:
            a = ''
    return a


def gv(obj, name, default='NOATTR'):
    try:
        return getattr(obj, name, default)
    except BaseException as e:
        return 'EXC %s' % type(e).__name__


# ======================= P01 模块矩阵 =======================
def probe_modules():
    for n in ('os', 'sys', 'datetime', 'time', 'json', 'math',
              'socket', 'threading', 'tornado', 'http.server', 'urllib'):
        P('01', 'MOD %-12s -> %s' % (n, _MODS.get(n, '未探测')))
    if sys is not None:
        try:
            P('01', 'PYTHON %s' % sys.version.replace('\n', ' ')[:60])
        except BaseException:
            pass


# ======================= P02 文件读写 =======================
def probe_file():
    dirs = []
    if LOG_DIR:
        dirs.append(LOG_DIR)
    try:
        if os is not None:
            dirs.append(os.getcwd())
    except BaseException as e:
        P('02', 'GETCWD FAIL %s' % type(e).__name__)
    for d in dirs:
        try:
            if not os.path.isdir(d):
                os.makedirs(d)
                P('02', 'MKDIR OK %s' % d)
        except BaseException as e:
            P('02', 'MKDIR FAIL %s %s: %s' % (d, type(e).__name__, e))
    for d in dirs:
        p = d + '\\probe.log'
        try:
            f = open(p, 'a')
            f.write('probe %s\n' % now_str())
            f.close()
            f = open(p, 'a')
            f.write('append-check\n')
            f.close()
            f = open(p, 'r')
            n = len(f.readlines())
            f.close()
            P('02', 'FILE OK append+read  %s  lines=%d' % (p, n))
        except BaseException as e:
            P('02', 'FILE FAIL append  %s  %s: %s' % (p, type(e).__name__, e))
        try:
            f = open(d + '\\probe_w.log', 'w')
            f.write('overwrite\n')
            f.close()
            P('02', 'FILE OK overwrite  %s' % d)
        except BaseException as e:
            P('02', 'FILE FAIL overwrite  %s  %s: %s' % (d, type(e).__name__, e))


# ======================= P03 端口监听（socket 层） =======================
def probe_socket():
    if socket is None:
        P('03', 'SOCKET SKIP（模块不可用）')
        return
    for host in ('127.0.0.1', '0.0.0.0'):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, HTTP_PORT))
            s.listen(5)
            P('03', 'SOCKET OK bind %s:%d' % (host, HTTP_PORT))
        except BaseException as e:
            P('03', 'SOCKET FAIL bind %s:%d  %s: %s' % (host, HTTP_PORT, type(e).__name__, e))
        finally:
            try:
                if s is not None:
                    s.close()
            except BaseException:
                pass


# ======================= P04/P05 后台 HTTP（tornado 或标准库） =======================
def probe_http():
    if threading is None:
        P('05', 'HTTP SKIP（threading 不可用）')
        return

    # 优先 tornado
    if tornado is not None:
        def run_tornado():
            try:
                ioloop = __import__('tornado.ioloop').ioloop
                web = __import__('tornado.web').web

                class H(web.RequestHandler):
                    def get(self):
                        self.write('QMT_PROBE_OK %s\n' % now_str())

                app = web.Application([(r'/probe', H)])
                app.listen(HTTP_PORT, address='0.0.0.0')
                P('05', 'HTTP LISTEN(tornado) 0.0.0.0:%d -> http://127.0.0.1:%d/probe' % (HTTP_PORT, HTTP_PORT))
                ioloop.IOLoop.current().start()
            except BaseException as e:
                P('05', 'HTTP FAIL tornado %s: %s' % (type(e).__name__, e))

        try:
            t = threading.Thread(target=run_tornado)
            t.setDaemon(True)
            t.start()
            P('05', 'HTTP thread started (tornado)')
            return
        except BaseException as e:
            P('05', 'HTTP THREAD FAIL %s: %s' % (type(e).__name__, e))

    # 退路：标准库 http.server
    if httpserver is None:
        P('05', 'HTTP SKIP（无 tornado / http.server）')
        return

    def run_std():
        try:
            srv = httpserver.HTTPServer(('0.0.0.0', HTTP_PORT), httpserver.BaseHTTPRequestHandler)
            P('05', 'HTTP LISTEN(std) 0.0.0.0:%d' % HTTP_PORT)
            srv.serve_forever()
        except BaseException as e:
            P('05', 'HTTP FAIL std %s: %s' % (type(e).__name__, e))

    try:
        t = threading.Thread(target=run_std)
        t.setDaemon(True)
        t.start()
        P('05', 'HTTP thread started (stdlib)')
    except BaseException as e:
        P('05', 'HTTP THREAD FAIL std %s: %s' % (type(e).__name__, e))


# ======================= P20 定时器 =======================
def probe_timer(ContextInfo):
    _TICK[0] += 1
    el = mono() - _T0[0]
    P('20', 'TIMER tick=%d elapsed=%.1fs' % (_TICK[0], el))


# ======================= init =======================
def init(C):
    _T0[0] = mono()
    P('00', '=== QMT 自检 v2 开始（v1 因 import socket 被拦而整体崩溃）===')
    P('00', 'do_back_test=%r' % gv(C, 'do_back_test'))
    P('01', 'ACCOUNT=%r  injected account=%r  injected accountType=%r' % (
        acct(), globals().get('account'), globals().get('accountType')))
    for name in ('accountid', 'accountID', 'accID', 'account', 'accounttype'):
        P('01', 'C.%s=%r' % (name, gv(C, name)))

    probe_modules()
    probe_file()
    probe_socket()
    probe_http()

    try:
        C.run_time('probe_timer', '%snSecond' % TIMER_SEC, '2026-01-05 09:15:00', 'SH')
        P('06', 'TIMER registered every %ss（回测模式下无效）' % TIMER_SEC)
    except BaseException as e:
        P('06', 'TIMER REGISTER FAIL %s: %s' % (type(e).__name__, e))

    P('00', '=== init 结束，下面进入 handlebar ===')


# ======================= handlebar =======================
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

    P('10', '--- handlebar 探针 ---')

    # P10 K 线
    data = safe('10', C.get_market_data_ex, ['open', 'high', 'low', 'close', 'volume'],
                [OPT_UNDL], '1m', 5)
    if data is not None:
        try:
            df = data.get(OPT_UNDL)
            if df is not None and hasattr(df, 'shape'):
                P('10', 'KLINE shape=%s cols=%s' % (df.shape, list(df.columns)))
                P('10', 'KLINE index_tail=%s' % str(list(df.index[-2:])))
                P('10', 'KLINE tail:\n%s' % df.tail(3).to_string()[:800])
            else:
                P('10', 'KLINE empty type=%s value=%r' % (type(data).__name__, str(data)[:200]))
        except BaseException as e:
            P('10', 'KLINE parse FAIL %s: %s' % (type(e).__name__, e))

    # P11 期权合约列表
    lst = safe('11', C.get_option_list, OPT_UNDL, OPT_MONTH, '', True)
    if lst is None:
        lst = safe('11', C.get_option_list, OPT_UNDL, OPT_MONTH, None, True)
    if lst is not None:
        try:
            P('11', 'OPTLIST month=%s count=%d' % (OPT_MONTH, len(lst)))
            P('11', 'OPTLIST head=%s' % str(list(lst)[:8]))
        except BaseException as e:
            P('11', 'OPTLIST parse FAIL %s: %s' % (type(e).__name__, e))

    # P12 期权规格全字段
    probe_code = None
    try:
        if lst:
            probe_code = list(lst)[0]
    except BaseException:
        probe_code = None
    if probe_code:
        d = safe('12', C.get_option_detail_data, probe_code)
        if isinstance(d, dict):
            P('12', 'DETAIL %s fields=%d' % (probe_code, len(d)))
            for k in sorted(d.keys()):
                P('12', '   %s = %r' % (k, d[k]))
        else:
            P('12', 'DETAIL not dict type=%s value=%r' % (type(d).__name__, str(d)[:200]))

    # P13 盘口
    t = safe('13', C.get_full_tick, [OPT_UNDL])
    if isinstance(t, dict):
        for k in list(t.keys())[:1]:
            v = t.get(k)
            if isinstance(v, dict):
                P('13', 'TICK code=%s keys=%s' % (k, sorted(v.keys())))
                P('13', 'TICK last=%r bid=%r bidVol=%r ask=%r askVol=%r' % (
                    v.get('lastPrice'), v.get('bidPrice'), v.get('bidVol'),
                    v.get('askPrice'), v.get('askVol')))
            else:
                P('13', 'TICK value not dict %r' % (str(v)[:200],))

    # P14 期权持仓
    pos = safe('14', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'position')
    if pos is not None:
        try:
            P('14', 'POS count=%d account=%r type=%s' % (len(pos), acct(), ACCOUNT_TYPE))
            for p in list(pos)[:5]:
                P('14', '  code=%s vol=%r canuse=%r open=%r cost=%r last=%r side=%r '
                       'covered=%r margin=%r royalty=%r exp=%r' % (
                           gv(p, 'm_strInstrumentID'), gv(p, 'm_nVolume'), gv(p, 'm_nCanUseVolume'),
                           gv(p, 'm_dOpenPrice'), gv(p, 'm_dPositionCost'), gv(p, 'm_dLastPrice'),
                           gv(p, 'm_eSideFlag'), gv(p, 'm_nCoveredVolume'),
                           gv(p, 'm_dRealUsedMargin'), gv(p, 'm_dRoyalty'),
                           gv(p, 'm_strExpireDate')))
        except BaseException as e:
            P('14', 'POS parse FAIL %s: %s' % (type(e).__name__, e))

    # P15 成交（手续费）
    deals = safe('15', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'DEAL')
    if deals is not None:
        try:
            P('15', 'DEAL count=%d' % len(deals))
            for d in list(deals)[-5:]:
                P('15', '  code=%s dir=%r off=%r px=%r vol=%r amt=%r fee=%r time=%s' % (
                    gv(d, 'm_strInstrumentID'), gv(d, 'm_nDirection'), gv(d, 'm_nOffsetFlag'),
                    gv(d, 'm_dPrice'), gv(d, 'm_nVolume'), gv(d, 'm_dTradeAmount'),
                    gv(d, 'm_dComssion'), gv(d, 'm_strTradeTime')))
        except BaseException as e:
            P('15', 'DEAL parse FAIL %s: %s' % (type(e).__name__, e))

    # P16 委托
    orders = safe('16', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'ORDER')
    if orders is not None:
        try:
            P('16', 'ORDER count=%d' % len(orders))
            for o in list(orders)[-5:]:
                P('16', '  code=%s status=%r traded=%r total=%r px=%r fMargin=%r fFee=%r '
                       'remark=%r cancelInfo=%r' % (
                           gv(o, 'm_strInstrumentID'), gv(o, 'm_nOrderStatus'),
                           gv(o, 'm_nVolumeTraded'), gv(o, 'm_nVolumeTotalOriginal'),
                           gv(o, 'm_dLimitPrice'), gv(o, 'm_dFrozenMargin'),
                           gv(o, 'm_dFrozenCommission'), gv(o, 'm_strRemark'),
                           gv(o, 'm_strCancelInfo')))
        except BaseException as e:
            P('16', 'ORDER parse FAIL %s: %s' % (type(e).__name__, e))

    # P17 账户
    acc = safe('17', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'account')
    if acc is not None:
        try:
            P('17', 'ACCOUNT count=%d' % len(acc))
            for a in list(acc)[:1]:
                P('17', '  balance=%r available=%r frozen=%r nav=%r profit=%r' % (
                    gv(a, 'm_dBalance'), gv(a, 'm_dAvailable'), gv(a, 'm_dFrozenCash'),
                    gv(a, 'm_dNav'), gv(a, 'm_dPositionProfit')))
        except BaseException as e:
            P('17', 'ACCOUNT parse FAIL %s: %s' % (type(e).__name__, e))

    P('99', '本轮探针结束')
