# -*- coding: gbk -*-
# ============================================================================
# 回测模式探针 probe_bt_mode_v1   BUILD 20261122-1330
# 只打印：不交易、不读写文件、不 import 业务模块。
#
# 四件事：1) init 期 do_back_test 的值+类型 与 handlebar 期是否一致
#         2) 一次点回测起了几个实例（init 计数 + id(ContextInfo)）
#         3) set_universe / accountID / capital 是否存在、能否设
#         4) 注册 run_time 是否影响回测形态
#
# 用法：粘进【模型研究】策略 -> 点回测 -> 贴回 [PROBE] 开头的行（约 12 行）
# 对照轮次（改完必须 停策略 -> 再启动）：
#   第1轮 P_UNIVERSE=1 P_ACCTID=1 P_RUNTIME=0（默认，对齐官方 demo）
#   第2轮 P_UNIVERSE=0 P_ACCTID=0            （这两项是不是必要条件）
#   第3轮 P_RUNTIME=1                        （run_time 是否破坏回测）
#
# 判读：
#   init=False 而 HB=True        -> init 阶段属性未就绪（我们 BT_ONLY 自锁的根因）
#   init=True  且 HB=True        -> 属性正常，去查实例数/其它
#   出现「第2次 init」且 id 不同  -> 一次点击起两个实例（模块级状态会互相踩）
#   do_back_test 类型是 str      -> bool('0') 为 True，必须显式比 '1'
# ============================================================================

BUILD = '20261122-1330'
P_UNIVERSE = 1      # 1 = init 里 set_universe(['510050.SH','510300.SH'])
P_ACCTID = 1        # 1 = init 里 ContextInfo.accountID = 'test'
P_RUNTIME = 0       # 1 = init 里注册 run_time 5 秒

_SEQ = []           # 每次 init 追加 id（故意模块级共享，验证是否同进程）
_HB = {}


def _p(C, n):
    """安全取值：'repr(type)'，缺属性/读异常如实显示"""
    try:
        v = getattr(C, n)
    except BaseException as e:
        return '<读取异常 %s>' % type(e).__name__
    try:
        return '%r(%s)' % (v, type(v).__name__)
    except BaseException:
        return '<repr 异常>'


def _s(t, m):
    try:
        print('[PROBE][%s] %s' % (t, m))
    except BaseException:
        pass


def init(C):
    _SEQ.append(hex(id(C)))
    _HB[id(C)] = {'n': 0, 'bt': None}
    _s('INIT', 'build=%s 第%d次init id=%s 全部实例=%s' % (
        BUILD, len(_SEQ), hex(id(C)), _SEQ))
    _s('INIT', 'do_back_test(init期)=%s' % _p(C, 'do_back_test'))
    _s('INIT', 'capital=%s hasattr=%s' % (_p(C, 'capital'), hasattr(C, 'capital')))
    _s('INIT', 'accountID(设前)=%s' % _p(C, 'accountID'))
    try:
        _s('INIT', 'get_universe()=%r' % (C.get_universe(),))
    except BaseException as e:
        _s('INIT', 'get_universe 异常 %s' % type(e).__name__)
    if P_UNIVERSE:
        try:
            C.set_universe(['510050.SH', '510300.SH'])
            _s('INIT', 'set_universe OK -> %r' % (C.get_universe(),))
        except BaseException as e:
            _s('INIT', 'set_universe 异常 %s' % type(e).__name__)
    if P_ACCTID:
        try:
            C.accountID = 'test'
            _s('INIT', 'accountID(设后)=%s' % _p(C, 'accountID'))
        except BaseException as e:
            _s('INIT', 'accountID 赋值异常 %s' % type(e).__name__)
    _s('INIT', 'global account=%r type=%r 主图=%r/%r/%r' % (
        globals().get('account'), globals().get('accountType'),
        getattr(C, 'stockcode', '?'), getattr(C, 'market', '?'),
        getattr(C, 'period', '?')))
    if P_RUNTIME:
        try:
            C.run_time('probe5s', '5nSecond', '2026-01-05 09:15:00', 'SH')
            _s('INIT', 'run_time 已注册')
        except BaseException as e:
            _s('INIT', 'run_time 异常 %s' % type(e).__name__)


def probe5s(C):
    _s('TICK', 'run_time 触发 id=%s do_back_test=%s' % (
        hex(id(C)), _p(C, 'do_back_test')))


def handlebar(C):
    st = _HB.setdefault(id(C), {'n': 0, 'bt': None})
    st['n'] += 1
    n = st['n']
    bt = bool(getattr(C, 'do_back_test', False))
    if not (n <= 3 or n % 500 == 0 or st['bt'] != bt):
        return
    st['bt'] = bt
    try:
        tg = timetag_to_datetime(C.get_bar_timetag(C.barpos), '%Y%m%d %H:%M')
    except BaseException:
        tg = '?'
    _s('HB', 'id=%s #%d barpos=%r tt=%s do_back_test=%s' % (
        hex(id(C)), n, getattr(C, 'barpos', '?'), tg, _p(C, 'do_back_test')))
