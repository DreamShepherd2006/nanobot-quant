#coding:gbk
# -*- coding: gbk -*-
"""
QMT 回测「期权记账能力」探针 v2   build=20261121-2355
v1 -> v2 改动：
  1) 触发条件由 (d==FIRE_DATE and hhmm>=FIRE_HHMM) 放宽为 d>=FIRE_DATE
     —— v1 在日线下 hhmm=0 永不满足，导致永远不下单；
  2) 加尾盘兜底：最后一根 bar 强制查账并打印；
  3) init 增加周期体检：period != '1m' 时告警（日线回测无分钟级撮合意义）。

目的：只回答一个问题 —— 平台内置回测引擎在回测态下，能不能按期权方式记账？
     卖开 1 张后，是否记期权持仓 / 是否扣保证金 / 是否扣手续费。

安全：
  1) 所有下单都套 `if not C.do_back_test: return`，实盘形态下绝不下单；
  2) 只做 1 张最小卖开，区间内不再加仓；
  3) 不读写任何文件。

用法：
  QMT → 新建【模型研究】策略（必须是模型研究，模型交易恒 do_back_test=False）
       → 主图 510050.SH / 1m → 整份粘贴本文件（GBK）→ 点【回测】
  → 把控制台所有 [PROBE] 行贴回来。
"""
import datetime

BUILD = '20261121-2355-PROBE2'
BT_START = 20261113
BT_END = 20261126          # 覆盖 11-25 到期日；数据不足会自然提前结束
FIRE_DATE = 20261113
CODE = '10012448.SHO'      # 510050 202611 put 2.9
QTY = 1
QUERY_EVERY = 60           # 每 60 根 bar 查一次平台账

_STATE = {'fired': False, 'n': 0, 'last': None, 'ended': False}


def _bt(C):
    try:
        return bool(C.do_back_test)
    except Exception:
        return False


def _now(C):
    try:
        if _bt(C):
            return datetime.datetime.fromtimestamp(C.get_bar_timetag(C.barpos) / 1000.0)
    except Exception:
        pass
    try:
        return datetime.datetime.now()
    except Exception:
        return datetime.datetime(2000, 1, 1)


def _st(dt):
    return dt.strftime('%Y%m%d %H%M')


def _acct(C):
    for k in ('accountid', 'accountID', 'account_id'):
        try:
            v = getattr(C, k, '') or ''
            if v:
                return str(v)
        except Exception:
            pass
    return ''


def _attrs(C, words):
    out = []
    for k in dir(C):
        kl = k.lower()
        if any(w in kl for w in words):
            try:
                v = getattr(C, k)
                if not callable(v):
                    out.append('%s=%r' % (k, v))
            except Exception:
                pass
    return ', '.join(out) or '(none)'


def _fld(obj, names):
    d = {}
    for n in names:
        try:
            v = getattr(obj, n, None)
            if v is not None:
                d[n] = v
        except Exception:
            pass
    return d or repr(obj)


POS_F = ['m_strInstrumentID', 'm_nVolume', 'm_nCanUseVolume', 'm_dOpenPrice',
         'm_dMarketValue', 'm_dFloatProfit', 'm_strOpenDate']
ACC_F = ['m_dBalance', 'm_dAvailable', 'm_dInstrumentValue', 'm_dFrozenCash',
         'm_dTotalFund']


def _query(C, tag):
    acct = _acct(C)
    for typ in ('POSITION', 'ACCOUNT'):
        try:
            rows = get_trade_detail_data(acct, 'STOCK_OPTION', typ)
            n = len(rows) if rows else 0
            print('[PROBE] %s QUERY %s acct=%r n=%d' % (tag, typ, acct, n))
            if n:
                show = rows[:6] if typ == 'POSITION' else rows[:1]
                for r in show:
                    print('[PROBE]   %s' % (_fld(r, POS_F if typ == 'POSITION' else ACC_F),))
        except Exception as e:
            print('[PROBE] %s QUERY %s 异常: %r' % (tag, typ, e))
    # 顺带看一眼普通股票账，判断是不是被记到股票账上去了
    try:
        rows = get_trade_detail_data(acct, 'STOCK', 'POSITION')
        print('[PROBE] %s QUERY STOCK/ POSITION n=%d' % (tag, len(rows) if rows else 0))
    except Exception as e:
        print('[PROBE] %s QUERY STOCK 异常: %r' % (tag, e))


def _fire(C):
    if not _bt(C):
        print('[PROBE] 非回测形态，拒绝下单（防误报单）')
        return
    acct = _acct(C)
    ok = False
    try:
        passorder(52, 1101, acct, CODE, 5, -1, QTY, C)
        ok = True
        print('[PROBE] passorder(短签名) opType=52 卖开 %s qty=%d acct=%r' % (CODE, QTY, acct))
    except Exception as e1:
        print('[PROBE] passorder 短签名失败: %r' % (e1,))
        try:
            passorder(52, 1101, acct, CODE, 5, -1, QTY, 'probe', 1, '', '', C)
            ok = True
            print('[PROBE] passorder(长签名) opType=52 卖开 %s qty=%d acct=%r' % (CODE, QTY, acct))
        except Exception as e2:
            print('[PROBE] passorder 长签名失败: %r' % (e2,))
    if ok:
        _query(C, 'after-order')


def init(C):
    sym, mk = '', ''
    for k in ('stockcode', 'stock_code'):
        try:
            v = getattr(C, k, '') or ''
            if v:
                sym = str(v)
                break
        except Exception:
            pass
    try:
        mk = str(getattr(C, 'market', '') or '')
    except Exception:
        pass
    per = ''
    try:
        per = str(getattr(C, 'period', '?') or '')
    except Exception:
        pass
    print('[PROBE] ===== build=%s =====' % BUILD)
    print('[PROBE] INIT backtest=%s do_bt=%r sym=%s.%s period=%r' % (
        _bt(C), _bt(C), sym, mk, per))
    if per.lower() not in ('1m', '1min', 'minute'):
        print('[PROBE] WARN 主图周期=%r 不是 1m —— 分钟级撮合无意义，请把主图改为 510050.SH / 1m' % per)
    print('[PROBE] INIT acct=%r accountType=%r' % (_acct(C), getattr(C, 'accountType', '?')))
    print('[PROBE] INIT 账号相关属性: %s' % _attrs(C, ['account', 'acct']))
    print('[PROBE] INIT 费用相关属性: %s' % _attrs(C, ['commission', 'fee', 'cost', 'tax']))
    print('[PROBE] INIT 区间 %d ~ %d | 标的合约 %s' % (BT_START, BT_END, CODE))
    _query(C, 'init')


def handlebar(C):
    if not _bt(C):
        return
    dt = _now(C)
    d, hhmm = int(dt.strftime('%Y%m%d')), int(dt.strftime('%H%M'))
    _STATE['n'] += 1
    if _STATE['n'] <= 3 or _STATE['n'] % 200 == 0:
        print('[PROBE] bar n=%d dt=%s' % (_STATE['n'], _st(dt)))
    if d < BT_START:
        return

    # 尾盘兜底：最后一根 bar 必查
    try:
        last = bool(C.is_last_bar())
    except Exception:
        last = False
    if last and not _STATE['ended']:
        _STATE['ended'] = True
        print('[PROBE] %s LAST-BAR bar=%d 区间结束查账' % (_st(dt), _STATE['n']))
        _query(C, 'last')
        return

    if not _STATE['fired'] and d >= FIRE_DATE:
        _STATE['fired'] = True
        px = None
        try:
            bars = C.get_market_data_ex([], [CODE], period='1m',
                                        start_time='', end_time=dt.strftime('%Y%m%d%H%M%S'),
                                        count=1, dividend_type='none', fill_data=False)
            df = bars.get(CODE)
            if df is not None and len(df):
                px = float(df['close'].iloc[-1])
        except Exception as e:
            print('[PROBE] 取价失败: %r' % (e,))
        print('[PROBE] %s FIRE 触发 bar=%d %s close=%r' % (_st(dt), _STATE['n'], CODE, px))
        _query(C, 'before-order')
        _fire(C)
    elif _STATE['fired'] and _STATE['n'] % QUERY_EVERY == 0:
        _query(C, '%s bar=%d' % (_st(dt), _STATE['n']))


def after_init(C):
    print('[PROBE] AFTER_INIT 完成')
