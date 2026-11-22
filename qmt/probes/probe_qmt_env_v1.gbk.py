#coding:gbk
# -*- coding: gbk -*-
"""
QMT 沙箱自检策略 v1 —— 只读探针，不含任何 passorder 下单调用

用途：一次性探明 in-QMT Python 环境的真实能力
      ① 参数面板注入  ② 文件读写  ③ 端口监听  ④ tornado  ⑤ run_time 秒级定时器
      ⑥ K线列名  ⑦ 期权合约列表  ⑧ get_option_detail_data 全字段
      9) 期权义务仓字段  10) 成交手续费  11) 委托状态码

运行步骤
  1) 新建一个 Python 策略，粘贴本文件全部代码，保存
  2) 右侧参数面板：ACCOUNT 填资金账号（若模型交易界面已注入账号可留空）
  3) 主图添加 510050.SH、周期 1 分钟
  4) 运行模式选「调试运行」或「模拟信号」（本策略不下单，两者都安全）
  5) 把日志面板里所有以 [P 开头的行整段复制回来
  6) 若启动后弹出 http://127.0.0.1:8712/probe 能打开并显示 QMT_PROBE_OK，说明端口监听可行
"""

import datetime
import os
import socket
import sys
import threading

# ======================= 参数（变量名 = 参数名） =======================
ACCOUNT = ''                 # 资金账号，留空则用模型交易界面注入的 account
ACCOUNT_TYPE = 'STOCK_OPTION'  # 期权账号类型；若查不到数据可试 'STOCK' / 'CREDIT'
OPT_UNDL = '510050.SH'       # 期权标的
OPT_MONTH = '202612'         # 到期月 YYYYMM
LOG_DIR = r'D:\qmt_probe'    # 文件探测目录（不存在会尝试创建）
HTTP_PORT = 8712             # 端口探测用
TIMER_SEC = 5                # run_time 定时器周期（秒）

# ======================= 小工具 =======================
_T0 = [datetime.datetime.now()]
_TICK = [0]
_BAKED = [False]


def P(tag, msg):
    line = '[P%s] %s | %s' % (tag, datetime.datetime.now().strftime('%H:%M:%S'), msg)
    print(line)
    try:
        sys.stdout.flush()
    except Exception:
        pass


def safe(tag, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:
        P(tag, 'EXC %s: %s' % (type(e).__name__, e))
        return None


def acct():
    a = ACCOUNT
    if not a:
        a = globals().get('account') or ''
    return a


def gv(obj, name, default='NOATTR'):
    try:
        return getattr(obj, name, default)
    except Exception as e:
        return 'EXC %s' % type(e).__name__


# ======================= P02 文件读写 =======================
def probe_file():
    dirs = []
    if LOG_DIR:
        dirs.append(LOG_DIR)
    dirs.append(os.getcwd())
    for d in dirs:
        tag = '02'
        try:
            if not os.path.isdir(d):
                os.makedirs(d)
                P(tag, 'MKDIR OK %s' % d)
        except Exception as e:
            P(tag, 'MKDIR FAIL %s %s: %s' % (d, type(e).__name__, e))
    for d in dirs:
        tag = '02'
        try:
            p = os.path.join(d, 'probe.log')
            f = open(p, 'a')
            f.write('probe %s\n' % datetime.datetime.now())
            f.close()
            f = open(p, 'a')
            f.write('append-check %s\n' % datetime.datetime.now())
            f.close()
            f = open(p, 'r')
            n = len(f.readlines())
            f.close()
            P(tag, 'FILE OK  append-read  dir=%s  lines=%d' % (d, n))
        except Exception as e:
            P(tag, 'FILE FAIL  dir=%s  %s: %s' % (d, type(e).__name__, e))
        try:
            p2 = os.path.join(d, 'probe_w.log')
            f = open(p2, 'w')
            f.write('overwrite %s\n' % datetime.datetime.now())
            f.close()
            P(tag, 'FILE OK  overwrite   dir=%s' % d)
        except Exception as e:
            P(tag, 'FILE FAIL  overwrite   dir=%s  %s: %s' % (d, type(e).__name__, e))


# ======================= P03 端口监听（socket 层） =======================
def probe_socket():
    for host in ('127.0.0.1', '0.0.0.0'):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, HTTP_PORT))
            s.listen(5)
            P('03', 'SOCKET OK   bind %s:%d listen OK' % (host, HTTP_PORT))
        except Exception as e:
            P('03', 'SOCKET FAIL bind %s:%d  %s: %s' % (host, HTTP_PORT, type(e).__name__, e))
        finally:
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass


# ======================= P04/P05 tornado + 后台 HTTP 服务 =======================
def probe_http_server():
    try:
        import tornado
        import tornado.ioloop
        import tornado.web
        P('04', 'TORNADO OK  version=%s' % getattr(tornado, 'version', '?'))
    except Exception as e:
        P('04', 'TORNADO FAIL  %s: %s' % (type(e).__name__, e))
        return

    def _run():
        try:
            class H(tornado.web.RequestHandler):
                def get(self):
                    self.write('QMT_PROBE_OK %s\n' % datetime.datetime.now())

            app = tornado.web.Application([(r'/probe', H)])
            app.listen(HTTP_PORT, address='0.0.0.0')
            P('05', 'HTTP LISTEN 0.0.0.0:%d  ->  浏览器打开 http://127.0.0.1:%d/probe' % (HTTP_PORT, HTTP_PORT))
            tornado.ioloop.IOLoop.current().start()
        except Exception as e:
            P('05', 'HTTP FAIL(in thread)  %s: %s' % (type(e).__name__, e))

    try:
        t = threading.Thread(target=_run)
        t.setDaemon(True)
        t.start()
        P('05', 'HTTP thread started (daemon)')
    except Exception as e:
        P('05', 'HTTP THREAD FAIL  %s: %s' % (type(e).__name__, e))


# ======================= P20 定时器 =======================
def probe_timer(ContextInfo):
    _TICK[0] += 1
    el = (datetime.datetime.now() - _T0[0]).total_seconds()
    P('20', 'TIMER tick=%d elapsed=%.1fs' % (_TICK[0], el))


# ======================= init =======================
def init(C):
    _T0[0] = datetime.datetime.now()
    P('00', '=== QMT 自检 v1 开始 ===')
    P('00', 'python=%s' % sys.version.replace('\n', ' ')[:60])
    P('00', 'do_back_test=%r' % gv(C, 'do_back_test'))
    P('01', 'ACCOUNT=%r  injected account=%r  injected accountType=%r' % (
        acct(), globals().get('account'), globals().get('accountType')))
    for name in ('accountid', 'accountID', 'accID', 'account', 'accounttype'):
        P('01', 'C.%s=%r' % (name, gv(C, name)))

    probe_file()
    probe_socket()
    probe_http_server()

    try:
        C.run_time('probe_timer', '%snSecond' % TIMER_SEC, '2026-01-05 09:15:00', 'SH')
        P('06', 'TIMER registered every %ss (回测模式下无效)' % TIMER_SEC)
    except Exception as e:
        P('06', 'TIMER REGISTER FAIL  %s: %s' % (type(e).__name__, e))


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
        except Exception:
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
                P('10', 'KLINE empty  type=%s value=%r' % (type(data).__name__, str(data)[:200]))
        except Exception as e:
            P('10', 'KLINE parse FAIL  %s: %s' % (type(e).__name__, e))

    # P11 期权合约列表
    lst = safe('11', C.get_option_list, OPT_UNDL, OPT_MONTH, '', True)
    if lst is None:
        lst = safe('11', C.get_option_list, OPT_UNDL, OPT_MONTH, None, True)
    if lst is not None:
        try:
            P('11', 'OPTLIST month=%s count=%d' % (OPT_MONTH, len(lst)))
            P('11', 'OPTLIST head=%s' % str(list(lst)[:8]))
        except Exception as e:
            P('11', 'OPTLIST parse FAIL  %s: %s' % (type(e).__name__, e))

    # P12 期权规格全字段
    probe_code = None
    try:
        if lst:
            probe_code = list(lst)[0]
    except Exception:
        probe_code = None
    if probe_code:
        d = safe('12', C.get_option_detail_data, probe_code)
        if isinstance(d, dict):
            P('12', 'DETAIL %s  fields=%d' % (probe_code, len(d)))
            for k in sorted(d.keys()):
                P('12', '   %s = %r' % (k, d[k]))
        else:
            P('12', 'DETAIL not dict  type=%s value=%r' % (type(d).__name__, str(d)[:200]))

    # P13 盘口
    t = safe('13', C.get_full_tick, [OPT_UNDL])
    if isinstance(t, dict):
        for k in list(t.keys())[:1]:
            v = t.get(k)
            if isinstance(v, dict):
                P('13', 'TICK code=%s keys=%s' % (k, sorted(v.keys())))
                P('13', 'TICK last=%r  bidPrice=%r  bidVol=%r  askPrice=%r  askVol=%r' % (
                    v.get('lastPrice'), v.get('bidPrice'), v.get('bidVol'),
                    v.get('askPrice'), v.get('askVol')))
            else:
                P('13', 'TICK value not dict  %r' % (str(v)[:200],))

    # P14 期权持仓（重点：义务仓字段）
    pos = safe('14', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'position')
    if pos is not None:
        try:
            P('14', 'POS count=%d  account=%r type=%s' % (len(pos), acct(), ACCOUNT_TYPE))
            for p in list(pos)[:5]:
                P('14', '  code=%s vol=%r canuse=%r open=%r cost=%r last=%r '
                       'side=%r covered=%r margin=%r royalty=%r exp=%r' % (
                           gv(p, 'm_strInstrumentID'), gv(p, 'm_nVolume'), gv(p, 'm_nCanUseVolume'),
                           gv(p, 'm_dOpenPrice'), gv(p, 'm_dPositionCost'), gv(p, 'm_dLastPrice'),
                           gv(p, 'm_eSideFlag'), gv(p, 'm_nCoveredVolume'),
                           gv(p, 'm_dRealUsedMargin'), gv(p, 'm_dRoyalty'), gv(p, 'm_strExpireDate')))
        except Exception as e:
            P('14', 'POS parse FAIL  %s: %s' % (type(e).__name__, e))

    # P15 成交（手续费口径）
    deals = safe('15', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'DEAL')
    if deals is not None:
        try:
            P('15', 'DEAL count=%d' % len(deals))
            for d in list(deals)[-5:]:
                P('15', '  code=%s dir=%r off=%r px=%r vol=%r amt=%r fee=%r time=%s' % (
                    gv(d, 'm_strInstrumentID'), gv(d, 'm_nDirection'), gv(d, 'm_nOffsetFlag'),
                    gv(d, 'm_dPrice'), gv(d, 'm_nVolume'), gv(d, 'm_dTradeAmount'),
                    gv(d, 'm_dComssion'), gv(d, 'm_strTradeTime')))
        except Exception as e:
            P('15', 'DEAL parse FAIL  %s: %s' % (type(e).__name__, e))

    # P16 委托
    orders = safe('16', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'ORDER')
    if orders is not None:
        try:
            P('16', 'ORDER count=%d' % len(orders))
            for o in list(orders)[-5:]:
                P('16', '  code=%s status=%r traded=%r total=%r px=%r frozenMargin=%r frozenFee=%r '
                       'remark=%r cancelInfo=%r' % (
                           gv(o, 'm_strInstrumentID'), gv(o, 'm_nOrderStatus'),
                           gv(o, 'm_nVolumeTraded'), gv(o, 'm_nVolumeTotalOriginal'),
                           gv(o, 'm_dLimitPrice'), gv(o, 'm_dFrozenMargin'),
                           gv(o, 'm_dFrozenCommission'), gv(o, 'm_strRemark'),
                           gv(o, 'm_strCancelInfo')))
        except Exception as e:
            P('16', 'ORDER parse FAIL  %s: %s' % (type(e).__name__, e))

    # P17 账户
    acc = safe('17', C.get_trade_detail_data, acct(), ACCOUNT_TYPE, 'account')
    if acc is not None:
        try:
            P('17', 'ACCOUNT count=%d' % len(acc))
            for a in list(acc)[:1]:
                P('17', '  balance=%r available=%r frozen=%r nav=%r profit=%r' % (
                    gv(a, 'm_dBalance'), gv(a, 'm_dAvailable'), gv(a, 'm_dFrozenCash'),
                    gv(a, 'm_dNav'), gv(a, 'm_dPositionProfit')))
        except Exception as e:
            P('17', 'ACCOUNT parse FAIL  %s: %s' % (type(e).__name__, e))

    P('99', '本轮探针结束')
