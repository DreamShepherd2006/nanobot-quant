#coding:gbk
# -*- coding: gbk -*-
"""
================================================================================
A股 ETF 期权卖 put 策略 v1（QMT 内置 Python，单文件）
================================================================================
定位：标的 ETF 现货算 TD（低9）→ 卖虚值 put → 权利金回落止盈买平 → 到期自然行权
约束：Python 3.6.8 / 无文件 I/O / 无网络 / 单线程 / gbk
状态：持仓即台账（券商合并持仓，每合约最多 1 张），跨日重启自动恢复

三种运行形态
  1) 模型研究（不加载账号）：只算信号与选档，打印决策，不下单
  2) 模型交易 + 模拟信号：走完整链路，passorder 只记「策略信号」，不成交
  3) 模型交易 + 实盘开关：真实下单（需 LIVE_CONFIRM=1 双保险）

参数面板（变量名即参数名；改完需停止策略再运行才生效）
================================================================================
"""

# ====================== 0. 参数（面板） ======================
ACCOUNT_FALLBACK = ''          # 账号兜底，正常走界面注入 account
ACCOUNT_TYPE = 'STOCK_OPTION'  # 期权账号类型
LIVE_CONFIRM = 0               # 代码内第二道实盘锁：0=绝不发单（默认）
UNDL_LIST = '510050.SH,510300.SH,510500.SH,588000.SH'
SIGNAL_PERIOD = '1m'           # 信号周期 1m/5m/15m/30m
TD_SETUP_PERIOD = 9            # TD setup 阈值
TD_CMP_LEN = 4                 # TD 比较长度
USE_LAST_BAR = 0               # 0=丢弃最后一根（可能未收盘），1=使用
MIN_DIST_PCT = '510050:5,510300:6,510500:7,588000:8'   # 距现价最小虚值%
MIN_ENTRY_DTE = 2              # 剩余到期天数 < 此值不再开新仓
TP_PCT_PUT = 50                # 止盈：权利金回落达开仓价此百分比 → 买平
FORCE_CLOSE_DTE = 0            # 1=到期前强平（默认 0 = 让它自然行权/作废）
FORCE_CLOSE_DTE_N = 3          # 强平阈值 DTE <= N
MAX_PER_CONTRACT = 1           # 同合约最多张数（券商合并持仓，不可拆）
MAX_PER_UNDL = 3               # 单标的在仓上限（张）
MAX_TOTAL = 6                  # 全池在仓上限（张）
MAX_ENTRIES_PER_DAY = 4        # 每日开仓次数上限
MARGIN_BUDGET_PCT = 30         # 保证金占用上限（占可用资金 %）
FEE_SELL = 1.2                 # 卖开手续费（元/张）
FEE_BUY = 1.7                  # 买平手续费（元/张）
ORDER_TIMEOUT_S = 20           # 挂单超时撤单（秒）
MAX_ORDER_FAIL = 3             # 连续废单熔断
STRAT_TAG = 'QOPT'             # 委托备注前缀（对账用）
PASSORDER_QUICK = 1            # passorder 的 quickTrade：1=立即下单，0=下一 bar 首分笔
TICK_SECONDS = 5               # 秒级巡检间隔
CLOSE_COOLDOWN_S = 60          # 同一合约平仓后冷却（防持仓快照延迟重复平仓）
LOG_VERBOSE = 1                # 1=每轮打印摘要，0=只在动作时打印
BT_HALF_SPREAD_PCT = 1.0       # 回测模拟半价差(%)：卖开吃 bid=close×(1-x)，买平付 ask=close×(1+x)
# 回测区间（验证期写死：QMT 回测面板的起止时间不传给自定义策略，只能自己卡）
# 格式 YYYYMMDD；置空串 = 不限。两头均含。
BT_START = '20261113'
BT_END = '20261120'
# 运行形态锁（验证期）：1=只允许回测形态运行，模拟信号/实盘启动后直接静默退出
# 0=跟随 QMT（原来的三形态行为）。验证完回测后改回 0 即可实盘。
BT_ONLY = 1
# 构建标识：每次改代码就改这里，日志首行会打出来 —— 用来确认 QMT 里跑的是哪一版
BUILD = '20261121-2205'
SIG_LEVEL = 0     # 回测信号日志粒度：0=仅归零+首穿(默认) 1=含临近阈值 2=含 th-2
LOG_DIR = r'D:\qmt_export'   # 回测日志落盘根目录（''=不落盘）；目录+文件名均自动建唯一体


# ====================== 1. 基础工具（零依赖） ======================
# 回测虚拟时钟：回测里所有「今天」「几点了」都取自当前 bar，而不是墙钟
_BT = {'on': False, 'ymd': 0, 'hm': 0, 't': '', 'closed': 0}


def _now():
    import time
    return time.localtime()


def _ts():
    import time
    return time.time()


def _ymd_now():
    if _BT['on'] and _BT['ymd'] > 0:
        return _BT['ymd']
    t = _now()
    return t.tm_year * 10000 + t.tm_mon * 100 + t.tm_mday


def _hhmm_now():
    if _BT['on']:
        return _BT['hm']
    t = _now()
    return t.tm_hour * 100 + t.tm_min


def _in_session():
    hm = _hhmm_now()
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def _days_from_civil(y, m, d):
    """公历日期 -> 天序号（纯算术，不依赖 datetime）"""
    y -= 1 if m <= 2 else 0
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _dte(exp_ymd, today_ymd):
    """到期日(YYYYMMDD) 距今日的自然日数"""
    try:
        ey, em, ed = int(exp_ymd) // 10000, (int(exp_ymd) // 100) % 100, int(exp_ymd) % 100
        ty, tm, td = today_ymd // 10000, (today_ymd // 100) % 100, today_ymd % 100
        return _days_from_civil(ey, em, ed) - _days_from_civil(ty, tm, td)
    except BaseException:
        return -1


def _f(x, d=0.0):
    try:
        return float(x)
    except BaseException:
        return d


def _i(x, d=0):
    try:
        return int(float(x))
    except BaseException:
        return d


def _gv(obj, name, default=None):
    try:
        return getattr(obj, name, default)
    except BaseException:
        return default


def _g(name, default=None):
    """读面板参数（面板注入到全局），缺失用代码默认"""
    v = globals().get(name)
    return default if v is None else v


_PRN = {}


def _open_log():
    """新建回测日志文件；写不了则返回 None（只输出到窗口）。

    客户端 FileIO 白名单只放行【新建】，覆盖/追加已存在文件一律 PermissionError；
    所以文件名必须唯一（BUILD + 墙钟时间戳 + 序号），绝不复用旧文件。
    """
    import os
    import time
    if not LOG_DIR:
        return None
    stamp = time.strftime('%Y%m%d_%H%M%S')
    tag = BUILD.replace('-', '')
    bases = [LOG_DIR]
    for k in ('USERPROFILE', 'TEMP', 'TMP'):
        v = os.environ.get(k, '')
        if v:
            bases.append(os.path.join(v, 'qmt_export'))
    bases.append('C:\\Windows\\Temp\\qmt_export')
    for d in bases:
        try:
            os.makedirs(d, exist_ok=True)
        except BaseException:
            pass
        for i in range(1, 30):
            name = 'qmt_bt_%s_%s%s.log' % (tag, stamp, '' if i == 1 else '_%d' % i)
            p = os.path.join(d, name)
            try:
                if os.path.exists(p):      # 已存在则不能写（白名单），换下一个序号
                    continue
            except BaseException:
                pass
            for enc in ('utf-8-sig', 'gbk', None):
                try:
                    f = open(p, 'w', encoding=enc) if enc else open(p, 'w')
                    f.write('===== QMT 回测日志 %s BUILD=%s =====\n' % (_ymd_now(), BUILD))
                    f.flush()
                    print('[%s][LOG] 日志落盘 -> %s' % (STRAT_TAG, p))
                    return f
                except BaseException:
                    pass
    print('[%s][LOG] 所有候选目录都写不了 → 仅输出到窗口' % STRAT_TAG)
    return None


def _log(tag, msg, key=None, force=False):
    """日志：同一 key 内容不变则不重复打印（防刷屏）

    回测期间 tag 统一加 BT. 前缀，与模拟信号/实盘实例的日志彻底分开。
    若日志文件可用（S.logf），同时写盘（时间戳用回测虚拟时钟，与 bar 对齐）。
    """
    if _BT['on'] and not tag.startswith('BT'):
        tag = 'BT.' + tag
    if key is not None and not force:
        if _PRN.get(key) == msg:
            return
        _PRN[key] = msg
    try:
        print('[%s][%s] %s' % (STRAT_TAG, tag, msg))
    except BaseException:
        pass
    if S.logf is not None:
        try:
            S.logf.write('[%d %04d] [%s][%s] %s\n' % (
                _ymd_now(), _hhmm_now(), STRAT_TAG, tag, msg))
            S.logf.flush()
        except BaseException:
            pass


def _safe(tag, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        _log(tag, 'EXC %s: %s' % (type(e).__name__, str(e)[:150]), key=tag + str(e)[:20])
        return None


# ====================== 2. 状态 ======================
class _St(object):
    def __init__(self):
        self.acct = ''
        self.acct_type = ACCOUNT_TYPE
        self.day = 0
        self.entries_today = 0
        self.fail_streak = 0
        self.cycle = {}       # undl -> {'bought':bool,'prev':int,'reset':bool}
        self.pending = {}     # code -> {'kind','ref','ts','px','qty','remark'}
        self.chain_cache = {}  # (undl, month) -> (ts, [detail dict])
        self.code_undl = {}   # 合约代码 -> 标的
        self.done_bar = {}    # undl -> 已处理信号 bar 的 time 字符串
        self.dry_skip = {}    # code -> 1  DRY 模式下已提示过，防重复刷
        self.closed_ts = {}   # code -> 最近一次平仓成交时间（防持仓快照延迟导致重复平仓）
        self.skip_note = {}   # undl -> 1  已提示过「K线不足」，防每 bar 刷屏
        self.bars_dbg = 0     # 回测取数对齐诊断只打一次
        self.blocked = 0      # BT_ONLY 已拦截并提示（非回测形态静默退出）
        self.logf = None      # 日志文件句柄（客户端禁用文件 IO 时为 None）
        self.sig_prev = {}    # undl -> 上一次打印过的 setup 计数（只在变化时打）
        self.bt_first = 0     # 回测区间内首根 bar 已打标志
        self.hb_calls = 0     # handlebar 被调用次数（回测诊断：判断主图是否驱动）
        self.bh_day = {}      # 回测持仓日志：code -> 已打印过的交易日（每交易日一行）
        self.bh_near = {}     # 回测持仓日志：code -> 已提示过「接近止盈线」
        self.in_bt = 0        # 已进入回测区间（用于「区间结束」收尾总账）
        self.sim = {}         # 回测内存持仓：code -> {code,undl,strike,open,qty,unit,margin,exp,bar,prem}
        self.bt_sig = None    # 回测汇总去重签名
        self.bt = {'bar': 0, 'opens': 0, 'closed': 0, 'tp': 0, 'force': 0,
                   'itm': 0, 'otm': 0, 'prem': 0.0, 'pnl': 0.0,
                   'win': 0, 'loss': 0, 'hold': [], 'stock_qty': 0, 'stock_cost': 0.0}


S = _St()


# ====================== 3. 数据层 ======================
def _bars(C, code, period, count):
    """返回 [{'t':str14,'o','h','l','c','v'}] 升序；失败返回 []

    回测必须限定 end_time：QMT 在 start/end 都为空时返回「到最新为止」的
    最近 count 根，回测里等于未来数据（2017 年 bar 会拿到 2026 年的价）。
    实盘（非回测）仍传空串，行为与改动前一致。
    """
    end_t = ''
    cur12 = ''
    if _BT['on'] and _BT['ymd'] > 0:
        cur12 = '%08d%04d' % (_BT['ymd'], _BT['hm'])
        end_t = cur12 + '00'
    d = _safe('BARS', C.get_market_data_ex,
              ['open', 'high', 'low', 'close', 'volume'], [code], period,
              '', end_t, count, 'none', True, True)
    if not isinstance(d, dict):
        return []
    df = d.get(code)
    if df is None:
        return []
    try:
        idx = list(df.index)
        cols = list(df.columns)
        ci = cols.index('close')
        oi = cols.index('open') if 'open' in cols else ci
        hi = cols.index('high') if 'high' in cols else ci
        li = cols.index('low') if 'low' in cols else ci
        vi = cols.index('volume') if 'volume' in cols else -1
        vals_c = list(df.values)
        out = []
        for k in range(len(idx)):
            row = vals_c[k]
            out.append({
                't': str(idx[k]),
                'o': _f(row[oi]), 'h': _f(row[hi]), 'l': _f(row[li]), 'c': _f(row[ci]),
                'v': _f(row[vi]) if vi >= 0 else 0.0,
            })
        if cur12:
            keep = []
            for b in out:
                t = str(b.get('t') or '')
                if len(t) >= 12 and t[:12].isdigit() and t[:12] > cur12:
                    continue          # 双保险：丢弃晚于当前回测 bar 的 K 线
                keep.append(b)
            out = keep
            if not S.bars_dbg:
                S.bars_dbg = 1
                _log('BARS', '回测取数对齐 bar=%s 末根=%s 根数=%d' % (
                    cur12, (str(out[-1]['t'])[:12] if out else '-'), len(out)), force=True)
        return out
    except BaseException as e:
        _log('BARS', 'parse fail %s' % type(e).__name__)
        return []


def _ticks(C, codes):
    """批量盘口 -> {code: {'last','bid','ask','bidv','askv'}}"""
    out = {}
    if not codes:
        return out
    t = _safe('TICK', C.get_full_tick, list(codes))
    if not isinstance(t, dict):
        return out
    for code, v in t.items():
        if not isinstance(v, dict):
            continue
        b = v.get('bidPrice')
        a = v.get('askPrice')
        bv = v.get('bidVol')
        av = v.get('askVol')
        out[code] = {
            'last': _f(v.get('lastPrice')),
            'bid': _f(b[0]) if isinstance(b, (list, tuple)) and len(b) else _f(b),
            'ask': _f(a[0]) if isinstance(a, (list, tuple)) and len(a) else _f(a),
            'bidv': _i(bv[0]) if isinstance(bv, (list, tuple)) and len(bv) else _i(bv),
            'askv': _i(av[0]) if isinstance(av, (list, tuple)) and len(av) else _i(av),
            'keys': len(v),
        }
    return out


def _months(C, undl, n=3):
    """候选到期月：当月起的 n 个月（YYYYMM 字符串）"""
    t = _now()
    y, m = t.tm_year, t.tm_mon
    out = []
    for k in range(n):
        mm = m + k
        yy = y + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        out.append('%04d%02d' % (yy, mm))
    return out


def _chain(C, undl, month, ttl=300):
    """期权链（仅 put）：[(code, detail_dict)]，带 TTL 缓存

    认沽识别用「P 列表 vs C 列表差集」：分别取 opttype='P' 与 'C'，
    若两列表完全相同 => 客户端不支持按方向过滤 => fail-closed 返回空
    （宁可不开仓，绝不误卖 call）。
    """
    key = (undl, month)
    c = S.chain_cache.get(key)
    if c and (_ts() - c[0]) < ttl:
        return c[1]
    lp = _safe('CHAIN', C.get_option_list, undl, month, 'P', True)
    lc = _safe('CHAIN', C.get_option_list, undl, month, 'C', True)
    sp = set([str(x) for x in (lp or [])])
    sc = set([str(x) for x in (lc or [])])
    if not sp:
        S.chain_cache[key] = (_ts(), [])
        return []
    if sp == sc:
        _log('CHAIN', 'FAIL-CLOSED %s %s opttype 过滤无效（P==C 共 %d）' % (
            undl, month, len(sp)), key='CF' + undl, force=True)
        S.chain_cache[key] = (_ts(), [])
        return []
    out = []
    for code in list(sp):
        d = _safe('DETAIL', C.get_option_detail_data, code)
        if not isinstance(d, dict):
            continue
        if _opt_right(d) == 'C':
            continue
        S.code_undl[code] = undl
        out.append((code, d))
    S.chain_cache[key] = (_ts(), out)
    return out


def _opt_right(d):
    """从 detail 判认购/认沽：返回 'P'/'C'/''"""
    for k in ('OptType', 'optType', 'CallOrPut', 'callOrPut'):
        v = d.get(k)
        if isinstance(v, str) and v:
            s = v.upper()
            if s.startswith('P') or s.startswith('PUT'):
                return 'P'
            if s.startswith('C') or s.startswith('CALL'):
                return 'C'
    return ''


def _positions(C):
    """期权义务仓 -> [{code,qty,avail,open,margin}]"""
    out = []
    lst = _safe('POS', _tdetail, C, 'position')
    if not lst:
        return out
    for p in lst:
        if str(_gv(p, 'm_eSideFlag', '')) != '1':   # '1' = 义务仓（卖方）
            continue
        out.append({
            'code': str(_gv(p, 'm_strInstrumentID', '')),
            'qty': _i(_gv(p, 'm_nVolume', 0)),
            'avail': _i(_gv(p, 'm_nCanUseVolume', 0)),
            'open': _f(_gv(p, 'm_dOpenPrice', 0)),
            'margin': _f(_gv(p, 'm_dRealUsedMargin', 0)),
        })
    return out


def _orders(C):
    return _safe('ORD', _tdetail, C, 'ORDER') or []


def _deals(C):
    return _safe('DEAL', _tdetail, C, 'DEAL') or []


def _account(C):
    """返回 {'avail','balance','margin'}；不同券商资金在不同账号类型下，两种都试"""
    best = {'avail': 0.0, 'balance': 0.0, 'margin': 0.0}
    for t in (ACCOUNT_TYPE, 'STOCK'):
        lst = _safe('ACC', _tdetail, C, 'account', t)
        if not lst:
            continue
        a = list(lst)[0]
        cur = {
            'avail': _f(_gv(a, 'm_dAvailable', 0)),
            'balance': _f(_gv(a, 'm_dBalance', 0)),
            'margin': _f(_gv(a, 'm_dInstrumentValue', 0)),
        }
        if cur['avail'] > best['avail'] or cur['balance'] > best['balance']:
            best = cur
    return best


def _tdetail(C, kind, acct_type=None):
    """get_trade_detail_data 是全局函数（不是 ContextInfo 方法）"""
    f = globals().get('get_trade_detail_data')
    if f is None:
        f = getattr(C, 'get_trade_detail_data', None)
    if f is None:
        _log('TD', 'get_trade_detail_data 不存在')
        return []
    return f(S.acct, acct_type or S.acct_type, kind)


# ====================== 4. 计算层（纯函数，可云端单测） ======================
def setup_counts(closes, cmp_len=4):
    """TD Buy Setup 计数：close < close[i-cmp_len] 连续累加，否则归零"""
    n = len(closes)
    out = [0] * n
    if cmp_len <= 0:
        return out
    for i in range(cmp_len, n):
        if closes[i] < closes[i - cmp_len]:
            out[i] = out[i - 1] + 1
        else:
            out[i] = 0
    return out


def sell_setup_counts(closes, cmp_len=4):
    """TD Sell Setup 计数：close > close[i-cmp_len] 连续累加（预留）"""
    n = len(closes)
    out = [0] * n
    if cmp_len <= 0:
        return out
    for i in range(cmp_len, n):
        if closes[i] > closes[i - cmp_len]:
            out[i] = out[i - 1] + 1
        else:
            out[i] = 0
    return out


def is_first_cross(counts, th):
    """本根 >= th 且上一根 < th"""
    if len(counts) < 2:
        return len(counts) == 1 and counts[0] >= th
    return counts[-1] >= th and counts[-2] < th


def pick_put(cands, spot, min_dist_pct, quotes, cfg_fee_sell):
    """选档：虚值够远 + bid 有效 + 净权利金收益率（净权利金/保证金）最高
    cands: [{'code','strike','exp','unit','mr'}]
    quotes: {code: {'bid':..,'ask':..}}
    """
    gate = spot * (1.0 - min_dist_pct / 100.0)
    best = None
    for c in cands:
        if c['strike'] > gate:
            continue
        q = quotes.get(c['code']) or {}
        bid = _f(q.get('bid'))
        if bid <= 0:
            continue
        premium = bid * c['unit']
        net = premium - cfg_fee_sell
        if net <= 0:
            continue
        margin = c['strike'] * c['unit'] * c['mr'] / 100.0
        if margin <= 0:
            continue
        c['bid'] = bid
        c['net'] = net
        c['margin'] = margin
        c['yield'] = net / margin * 100.0
        if best is None or c['yield'] > best['yield']:
            best = c
    return best


def decide_exit(pos, q, cfg):
    """返回 (action, reason, px)；action in ('','close')"""
    bid = _f(q.get('bid'))
    open_px = _f(pos.get('open'))
    if open_px <= 0 or bid <= 0:
        return '', '', 0.0
    fall = (open_px - bid) / open_px * 100.0
    if fall >= cfg['tp_pct']:
        return 'close', 'tp%.0f%%(fall%.1f%%)' % (cfg['tp_pct'], fall), _f(q.get('ask')) or bid
    return '', '', 0.0


# ====================== 5. 执行层 ======================
def _can_trade():
    """三重防呆：模型研究无账号 / 模拟信号模式不成交 / LIVE_CONFIRM 代码锁"""
    return _i(_g('LIVE_CONFIRM', LIVE_CONFIRM)) == 1


def _ref_ok(ref):
    """passorder 返回单号才算真下单：None / 负数 / 0 视为失败"""
    if ref is None:
        return False
    try:
        return int(ref) > 0
    except BaseException:
        return True


def _passorder(op, code, qty, px, remark, C):
    fn = globals().get('passorder')
    if fn is None:
        _log('ORDER', 'passorder 不存在（模型研究/模拟模式？）', force=True)
        return None
    return _safe('ORDER', fn, op, 1101, S.acct, code, 11, round(px, 4), int(qty),
                 STRAT_TAG, _i(_g('PASSORDER_QUICK', PASSORDER_QUICK)), remark, C)


def sell_open(C, code, qty, px, remark):
    if not _can_trade():
        _log('DRY', '本应 卖开 %s x%d @%.4f | %s' % (code, qty, px, remark), force=True)
        return None
    ref = _passorder(52, code, qty, px, remark, C)
    _log('SELL', '卖开 %s x%d @%.4f ref=%r | %s' % (code, qty, px, ref, remark), force=True)
    return ref


def buy_close(C, code, qty, px, remark):
    if not _can_trade():
        _log('DRY', '本应 买平 %s x%d @%.4f | %s' % (code, qty, px, remark), force=True)
        return None
    ref = _passorder(53, code, qty, px, remark, C)
    _log('COVER', '买平 %s x%d @%.4f ref=%r | %s' % (code, qty, px, ref, remark), force=True)
    return ref


def _cancel(C, ref):
    fn = globals().get('cancel')
    if fn is None:
        return
    try:
        fn(ref, S.acct, S.acct_type, C)
        _log('CANCEL', '撤单 ref=%r' % ref, force=True)
    except BaseException as e:
        _log('CANCEL', '撤单失败 %s' % type(e).__name__)


# ====================== 5.5 回测模拟撮合（仅 do_back_test） ======================
# 模型研究/回测里没有账号、拿不到持仓、也取不到历史盘口，
# 因此用「期权 1m bar 收盘价 ± 半价差」模拟 bid/ask，内存记账完成一轮开平，
# 输出可核对统计（止盈/强平/到期 ITM、OTM/盈亏）。
# 边界：不碰实盘路径（实盘走 _positions/_do_exits），也不是参数裁决工具。


def _bt_sync(C):
    """虚拟时钟对齐当前 bar：优先 get_bar_timetag，兜底用 K 线时间字符串"""
    if not _BT['on']:
        return
    ms = None
    try:
        bp = _gv(C, 'barpos', None)
        if bp is not None:
            ms = C.get_bar_timetag(bp)
    except BaseException:
        ms = None
    ts = _f(ms) / 1000.0
    if ts > 0:
        try:
            import time
            t = time.localtime(ts)
            _BT['ymd'] = t.tm_year * 10000 + t.tm_mon * 100 + t.tm_mday
            _BT['hm'] = t.tm_hour * 100 + t.tm_min
            return
        except BaseException:
            pass
    ul = _undl_list()
    if not ul:
        return
    bars = _bars(C, ul[0], str(_g('SIGNAL_PERIOD', SIGNAL_PERIOD)), 3)
    if not bars:
        return
    s = str(bars[-1].get('t') or '')
    if len(s) >= 8 and s[:8].isdigit():
        _log('BARS', 'get_bar_timetag 不可用，回测时钟回退到最新K线 %s（可能不是当前 bar）'
             % s[:12], force=True)
        _BT['ymd'] = int(s[:8])
        if len(s) >= 12 and s[8:12].isdigit():
            _BT['hm'] = int(s[8:12])


def _bt_in_range():
    """回测区间闸：QMT 回测面板的起止时间不会传给自定义策略，只能自己卡。
    时钟未建立（ymd=0）时不拦——宁可多跑，也不要整轮回测空白。
    """
    if not BT_START and not BT_END:
        return True
    if _BT['ymd'] <= 0:
        return True
    d = _ymd_now()
    if BT_START and d < _i(BT_START):
        return False
    if BT_END and d > _i(BT_END):
        return False
    return True


def _bt_last_px(C, code):
    """回测当前权利金 = 该合约最近一根已收盘 bar 的收盘价"""
    bars = _bars(C, code, str(_g('SIGNAL_PERIOD', SIGNAL_PERIOD)), 6)
    if not bars:
        return 0.0
    return _f(bars[-1]['c'])


def _bt_quotes(C, codes):
    """回测盘口：close ± 半价差（无历史盘口可得，只能这样建模）"""
    half = _f(_g('BT_HALF_SPREAD_PCT', BT_HALF_SPREAD_PCT)) / 100.0
    out = {}
    for code in codes:
        px = _bt_last_px(C, code)
        if px <= 0:
            continue
        out[code] = {
            'last': px,
            'bid': round(px * (1.0 - half), 6),
            'ask': round(px * (1.0 + half), 6),
        }
    return out


def _bt_open(pick, undl, cfg):
    """回测开仓：记入内存持仓"""
    code = pick['code']
    if code in S.sim:
        return
    p = {
        'code': code,
        'undl': undl,
        'strike': pick['strike'],
        'open': pick['bid'],
        'qty': 1,
        'unit': pick['unit'],
        'margin': pick['margin'],
        'exp': pick.get('exp'),
        'bar': S.bt['bar'],
    }
    p['prem'] = p['open'] * p['unit'] - cfg['fee_sell']
    S.sim[code] = p
    S.bt['opens'] += 1
    S.bt['prem'] += p['prem']


def _bt_positions():
    """内存持仓 -> 与 _positions() 同形状，供 _try_entry 上限/去重复用"""
    out = []
    for code in list(S.sim.keys()):
        p = S.sim[code]
        out.append({
            'code': code,
            'qty': p['qty'],
            'avail': p['qty'],
            'open': p['open'],
            'margin': p['margin'],
        })
    return out


def _bt_finish(p, pnl):
    S.bt['pnl'] += pnl
    S.bt['closed'] += 1
    S.bt['hold'].append(max(0, S.bt['bar'] - p['bar']))
    if pnl >= 0:
        S.bt['win'] += 1
    else:
        S.bt['loss'] += 1
    S.sim.pop(p['code'], None)
    S.bh_day.pop(p['code'], None)
    S.bh_near.pop(p['code'], None)


def _bt_close(p, px, reason, cfg):
    """止盈 / DTE 强平：付 ask 买平"""
    cost = px * p['unit'] + cfg['fee_buy']
    pnl = p['prem'] - cost
    if reason.startswith('tp'):
        S.bt['tp'] += 1
    else:
        S.bt['force'] += 1
    _log('BTCLOSE', '%s %s 买平@%.4f 开仓%.4f 权利金%.2f 平仓成本%.2f 盈亏%+.2f 持有%d根' % (
        p['undl'], p['code'], px, p['open'], p['prem'], cost, pnl,
        S.bt['bar'] - p['bar']), force=True)
    _bt_finish(p, pnl)


def _bt_settle(C, p, cfg):
    """到期结算：标的 >= strike 作废全收；否则行权接货，赔 (strike-标的)×合约单位"""
    bars = _bars(C, p['undl'], str(_g('SIGNAL_PERIOD', SIGNAL_PERIOD)), 6)
    settle = _f(bars[-1]['c']) if bars else 0.0
    if settle <= 0:
        _log('BTSET', '%s 到期但取不到标的价，暂不结算' % p['code'], key='BS' + p['code'])
        return
    unit = p['unit']
    strike = p['strike']
    if settle >= strike:
        payout = 0.0
        S.bt['otm'] += 1
        tag = 'OTM作废'
    else:
        payout = (strike - settle) * unit
        S.bt['itm'] += 1
        S.bt['stock_qty'] += unit
        S.bt['stock_cost'] += strike * unit
        tag = 'ITM接货 %d份@%.4f' % (unit, strike)
    pnl = p['prem'] - payout
    _log('BTSET', '%s 到期 标的=%.4f strike=%.4f %s 权利金%.2f 赔付%.2f 盈亏%+.2f 持有%d根' % (
        p['code'], settle, strike, tag, p['prem'], payout, pnl,
        S.bt['bar'] - p['bar']), force=True)
    _bt_finish(p, pnl)


def _bt_exits(C, cfg):
    """回测出场：到期结算 -> DTE 强平 -> 止盈（止盈复用 decide_exit，同一份规则）"""
    if not S.sim:
        return
    half = _f(_g('BT_HALF_SPREAD_PCT', BT_HALF_SPREAD_PCT)) / 100.0
    for code in list(S.sim.keys()):
        p = S.sim.get(code)
        if p is None:
            continue
        dte = _dte(p['exp'], _ymd_now()) if p.get('exp') else 999
        if dte < 0:
            _bt_settle(C, p, cfg)
            continue
        px = _bt_last_px(C, code)
        if px <= 0:
            _log('BT', '%s 无期权行情，跳过出场判定' % code, key='BX' + code)
            continue
        q = {'last': px,
             'bid': round(px * (1.0 - half), 6),
             'ask': round(px * (1.0 + half), 6)}
        p['px'] = px          # 供总账算未实现盈亏（期末快照）
        pos = {'code': code, 'open': p['open'], 'qty': p['qty'], 'avail': p['qty']}
        act, reason, fill = decide_exit(pos, q, cfg)
        if cfg['force_dte'] == 1 and 0 <= dte <= cfg['force_n']:
            act, reason, fill = 'close', 'dte%d' % dte, q['ask']
        if act != 'close':
            # 降噪：每交易日每持仓只打一行；另外首次逼近止盈线（px ≤ open×60%）时补一行
            today = _ymd_now()
            near = px <= p['open'] * 0.6
            if S.bh_day.get(code) != today or (near and not S.bh_near.get(code)):
                _log('BTHOLD', '%s open=%.4f px=%.4f dte=%d%s' % (
                    code, p['open'], px, dte, ' ← 接近止盈线' if near else ''),
                    force=True)
                S.bh_day[code] = today
            if near:
                S.bh_near[code] = 1
            continue
        _bt_close(p, fill, reason, cfg)


def _bt_summary(force=False):
    """回测汇总：交易变动时打印，另每 400 根 bar 打一次心跳（末行即总账）"""
    b = S.bt
    sig = (b['opens'], b['closed'], round(b['pnl'], 2), len(S.sim))
    if not force and sig == S.bt_sig and b['bar'] % 400 != 0:
        return
    S.bt_sig = sig
    n = len(b['hold'])
    avg = (sum(b['hold']) / n) if n else 0.0
    unreal = 0.0
    for p in S.sim.values():
        cur = p.get('px')
        if cur is None:
            continue
        unreal += (p['open'] - cur) * p['unit']
    s = ('bar=%d 开=%d 平=%d(止盈%d/强平%d) 到期=%d(OTM%d/ITM%d) '
         '权利金=%.2f 已实现盈亏=%+.2f 胜=%d 负=%d 平均持有=%.0f根 在仓=%d张' % (
             b['bar'], b['opens'], b['tp'] + b['force'], b['tp'], b['force'],
             b['otm'] + b['itm'], b['otm'], b['itm'], b['prem'], b['pnl'],
             b['win'], b['loss'], avg, len(S.sim)))
    if b['stock_qty'] > 0:
        s += ' | 接货=%d份 成本=%.2f' % (_i(b['stock_qty']), b['stock_cost'])
    if S.sim:
        s += ' | 未实现=%+.2f 净=%+.2f' % (unreal, b['pnl'] + unreal)
    _log('BTSUM', s, force=True)


# ====================== 6. 编排 ======================
def _cfg():
    return {
        'tp_pct': _f(_g('TP_PCT_PUT', TP_PCT_PUT)),
        'min_dte': _i(_g('MIN_ENTRY_DTE', MIN_ENTRY_DTE)),
        'force_dte': _i(_g('FORCE_CLOSE_DTE', FORCE_CLOSE_DTE)),
        'force_n': _i(_g('FORCE_CLOSE_DTE_N', FORCE_CLOSE_DTE_N)),
        'max_undl': _i(_g('MAX_PER_UNDL', MAX_PER_UNDL)),
        'max_total': _i(_g('MAX_TOTAL', MAX_TOTAL)),
        'max_day': _i(_g('MAX_ENTRIES_PER_DAY', MAX_ENTRIES_PER_DAY)),
        'budget_pct': _f(_g('MARGIN_BUDGET_PCT', MARGIN_BUDGET_PCT)),
        'fee_sell': _f(_g('FEE_SELL', FEE_SELL)),
        'fee_buy': _f(_g('FEE_BUY', FEE_BUY)),
        'timeout': _i(_g('ORDER_TIMEOUT_S', ORDER_TIMEOUT_S)),
    }


def _undl_list():
    s = str(_g('UNDL_LIST', UNDL_LIST))
    return [x.strip() for x in s.replace('，', ',').split(',') if x.strip()]


def _min_dist(undl):
    s = str(_g('MIN_DIST_PCT', MIN_DIST_PCT))
    base = undl.split('.')[0]
    for part in s.replace('，', ',').split(','):
        if ':' in part:
            k, v = part.split(':', 1)
            if k.strip() == base:
                return _f(v, 5.0)
    return 5.0


def init(C):
    # 账号：模型交易界面注入 account；模型研究模式不会注入
    S.blocked = 0      # 每次 init 重置：QMT 点回测时会先起一个模拟信号实例再起回测实例，
    #                    两者共享模块级 S —— 不清零会让回测实例被上一个实例的拦截标记带走
    S.hb_calls = 0
    S.bt_first = 0
    S.sig_prev = {}
    S.bh_day = {}
    S.bh_near = {}
    S.in_bt = 0
    try:
        if S.logf is not None:
            S.logf.close()
    except BaseException:
        pass
    S.logf = None
    S.acct = str(_g('ACCOUNT_FALLBACK', '') or globals().get('account') or '')
    S.acct_type = str(globals().get('accountType') or ACCOUNT_TYPE)
    _BT['on'] = bool(_gv(C, 'do_back_test', False))
    if _BT['on']:
        S.logf = _open_log()     # 回测才落盘；实盘输出交给 QMT 自身日志
    _BT['ymd'] = 0
    _BT['hm'] = 0
    _BT['closed'] = 0
    S.day = 0 if _BT['on'] else _ymd_now()
    _log('INIT', '=' * 60, force=True)
    _log('INIT', 'v1 start | build=%s | backtest=%r | acct=%r | type=%s | live_confirm=%r' % (
        BUILD, _gv(C, 'do_back_test', False), S.acct, S.acct_type, _g('LIVE_CONFIRM', 0)), force=True)
    if BT_ONLY and not _BT['on']:
        S.blocked = 1
        _log('INIT', 'BT_ONLY=1（验证期）：本实例不是回测形态 → handlebar 将全部跳过'
             '（不计算、不下单、无日志）。要恢复实盘请把文件顶部 BT_ONLY 改成 0', force=True)
        try:
            _raw = repr(getattr(C, 'do_back_test', '<属性不存在>'))
        except BaseException:
            _raw = '<读取失败>'
        _log('INIT', 'do_back_test 原始值=%s -> 该策略条目是「模型交易」类型（回测按钮也会跑模拟信号）。'
             '请到「模型研究」分类下新建策略再排入本代码' % _raw, force=True)
    _log('INIT', 'UNDL=%s | period=%s | setup=%s | tp=%s%% | min_dte=%s' % (
        _undl_list(), _g('SIGNAL_PERIOD', SIGNAL_PERIOD),
        _g('TD_SETUP_PERIOD', TD_SETUP_PERIOD), _g('TP_PCT_PUT', TP_PCT_PUT),
        _g('MIN_ENTRY_DTE', MIN_ENTRY_DTE)), force=True)
    if bool(_gv(C, 'do_back_test', False)):
        _log('INIT', '回测区间（代码写死，面板起止对本策略无效）%s ~ %s'
             % (BT_START or '不限', BT_END or '不限'), force=True)
        _log('INIT', '主图=%s.%s period=%r' % (
            getattr(C, 'stockcode', '?'), getattr(C, 'market', '?'),
            getattr(C, 'period', '?')), force=True)
    _log('INIT', 'global fn: get_trade_detail_data=%r passorder=%r cancel=%r' % (
        'get_trade_detail_data' in globals(), 'passorder' in globals(),
        'cancel' in globals()), force=True)
    if _BT['on']:
        _log('INIT', '回测模拟撮合开启：期权 bar 收盘价 ± %.2f%% 半价差模拟盘口，内存记账'
             '（不读持仓/不发单，总账看 [BTSUM]）' % (
                 _f(_g('BT_HALF_SPREAD_PCT', BT_HALF_SPREAD_PCT))), force=True)

    # 秒级巡检：撤单 / 止盈 / 日切（回测下无效，回测走 handlebar）
    try:
        C.run_time('tick5s', '%snSecond' % _i(_g('TICK_SECONDS', TICK_SECONDS)),
                   '2026-01-05 09:15:00', 'SH')
        _log('INIT', 'run_time 秒级巡检已注册', force=True)
    except BaseException as e:
        _log('INIT', 'run_time 注册失败 %s（回测/老版本）' % type(e).__name__, force=True)


def _day_roll():
    d = _ymd_now()
    if S.day == 0:            # 回测首根：只记日期，不当作日切
        S.day = d
        return
    if d != S.day:
        # 回测下空跑日不打（否则 2017→2026 刷上万行）；实盘每天打一行无妨
        if S.entries_today or not _BT['on']:
            _log('DAY', '日切 %s -> %s | 昨日开仓 %d 次' % (S.day, d, S.entries_today), force=True)
        S.day = d
        S.entries_today = 0
        S.fail_streak = 0
        S.done_bar = {}


def _poll_orders(C):
    """回收委托：成交 / 废单 / 超时撤单"""
    now = _ts()
    if not S.pending:
        return
    live = {}
    for o in _orders(C):
        oid = _gv(o, 'm_nOrderID', None)
        if oid is None:
            oid = _gv(o, 'm_strOrderSysID', None)
        live[str(oid)] = (_i(_gv(o, 'm_nOrderStatus', 0)),
                          _i(_gv(o, 'm_nVolumeTraded', 0)),
                          _i(_gv(o, 'm_nVolumeTotalOriginal', 0)),
                          str(_gv(o, 'm_strCancelInfo', '')))
    for code in list(S.pending.keys()):
        p = S.pending[code]
        rec = live.get(str(p.get('ref')))
        done = False
        if rec is not None:
            st, traded, total, cinfo = rec
            if st == 56 or (total > 0 and traded >= total):
                _log('FILL', '%s %s 成交 %d/%d' % (p['kind'], code, traded, total), force=True)
                done = True
                S.fail_streak = 0
                if p['kind'] == 'close':
                    S.closed_ts[code] = _ts()
            elif st in (54, 57):
                _log('REJECT', '%s %s status=%d cancel=%r' % (
                    p['kind'], code, st, cinfo), force=True)
                done = True
                S.fail_streak += 1
        if not done and (now - p.get('ts', now)) > _cfg()['timeout']:
            _cancel(C, p.get('ref'))
            done = True
        if done:
            S.pending.pop(code, None)
            if p['kind'] == 'open':
                S.entries_today = max(0, S.entries_today - 1)
                cy = S.cycle.get(p.get('undl', ''))
                if cy:
                    cy['bought'] = False
                    cy['reset'] = True
    if S.fail_streak >= _i(_g('MAX_ORDER_FAIL', MAX_ORDER_FAIL)):
        _log('FUSE', '连续废单 %d 次 -> 停止开仓（人工检查后重启策略）' % S.fail_streak, force=True)


def _do_exits(C, positions):
    """止盈 / DTE 强平：对每个义务仓看权利金回落"""
    cfg = _cfg()
    if not positions:
        return
    qs = _ticks(C, [p['code'] for p in positions])
    for p in positions:
        code = p['code']
        if code in S.pending:          # 已有在途委托，不重复下
            continue
        if S.dry_skip.get(code) and not _can_trade():
            continue
        if (_ts() - S.closed_ts.get(code, 0)) < _i(_g('CLOSE_COOLDOWN_S', CLOSE_COOLDOWN_S)):
            continue
        q = qs.get(code) or {}
        act, reason, px = decide_exit(p, q, cfg)
        if cfg['force_dte'] == 1:
            d = _detail_dte(C, code)
            if 0 <= d <= cfg['force_n']:
                act, reason, px = 'close', 'dte%d' % d, _f(q.get('ask')) or _f(q.get('bid'))
        if act != 'close':
            _log('HOLD', '%s open=%.4f bid=%.4f' % (code, p['open'], _f(q.get('bid'))),
                 key='H' + code)
            continue
        qty = p['avail'] if p['avail'] > 0 else p['qty']
        if qty <= 0 or px <= 0:
            _log('EXIT', '%s 跳过 qty=%d px=%.4f' % (code, qty, px), key='E' + code)
            continue
        ref = buy_close(C, code, qty, px, '%s|close|%s' % (STRAT_TAG, reason))
        if not _can_trade():
            S.dry_skip[code] = 1
            continue
        if not _ref_ok(ref):
            _log('COVER', '%s 下单失败 ref=%r（本轮不重试）' % (code, ref), force=True)
            S.fail_streak += 1
            S.dry_skip[code] = 1
            continue
        S.pending[code] = {'kind': 'close', 'ref': ref, 'ts': _ts(),
                           'px': px, 'qty': qty, 'remark': reason}


def _detail_dte(C, code):
    d = _safe('DETAIL', C.get_option_detail_data, code)
    if not isinstance(d, dict):
        return -1
    return _dte(d.get('ExpireDate'), _ymd_now())


def _try_entry(C, undl, positions, cfg):
    """单标的开仓判定"""
    if len(S.pending) > 0:
        return
    period = str(_g('SIGNAL_PERIOD', SIGNAL_PERIOD))
    use_last = _i(_g('USE_LAST_BAR', USE_LAST_BAR))
    if _BT['on']:
        use_last = 1          # 回测里每根都是已收盘 bar，不需要丢最后一根
    th = _i(_g('TD_SETUP_PERIOD', TD_SETUP_PERIOD))
    cmp_len = _i(_g('TD_CMP_LEN', TD_CMP_LEN))

    bars = _bars(C, undl, period, 80)
    need = cmp_len + th + 2
    if len(bars) < need:
        if not S.skip_note.get(undl):      # 只提示一次，避免每 bar 刷屏
            S.skip_note[undl] = 1
            _log('SKIP', '%s K线不足（当前 %d 根，需 %d）— 不足不再重复提示' % (
                undl, len(bars), need), force=True)
        return
    S.skip_note.pop(undl, None)
    if not use_last:
        bars = bars[:-1]
    closes = [b['c'] for b in bars]
    counts = setup_counts(closes, cmp_len)
    last_t = bars[-1]['t']
    if S.done_bar.get(undl) == last_t:
        return
    S.done_bar[undl] = last_t

    if undl not in S.cycle:
        # 重启后已有该标的义务仓 → 视为本周期已建仓（保守，不追同一周期）
        has = 0
        for p0 in positions:
            if _undl_of(C, p0['code']) == undl:
                has = 1
                break
        S.cycle[undl] = {'bought': bool(has), 'prev': 0, 'reset': not bool(has)}
        if has:
            _log('INIT', '%s 已持义务仓 → 视为本周期已建仓（等计数回落后的新周期）' % undl,
                 force=True)
    cy = S.cycle[undl]
    cur = counts[-1]
    if cur < cy['prev']:
        cy['reset'] = True
    cy['prev'] = cur

    if not is_first_cross(counts, th):
        if _BT['on']:
            # 回测降噪：临近阈值才打，超过阈值后的累加期（10/11/12…22）一律静默；
            # SIG_LEVEL: 0=仅归零 1=临近 th-1/th（默认） 2=含 th-2
            _lv = _i(_g('SIG_LEVEL', SIG_LEVEL))
            if _lv >= 2:
                _hit = th - 2 <= cur <= th
            elif _lv == 1:
                _hit = th - 1 <= cur <= th
            else:
                _hit = False
            if (_hit or cur == 0) and S.sig_prev.get(undl) != cur:
                S.sig_prev[undl] = cur
                _log('SIG', '%s setup=%d/%d close=%.4f' % (
                    undl, cur, th, closes[-1]), key='S' + undl)
        elif _i(_g('LOG_VERBOSE', LOG_VERBOSE)) == 1:
            _log('SIG', '%s setup=%d/%d close=%.4f' % (undl, cur, th, closes[-1]), key='S' + undl)
        return
    _log('SIG', '*** %s 低9 首穿 setup=%d close=%.4f ***' % (undl, cur, closes[-1]), force=True)

    if cy['bought'] and not cy['reset']:
        _log('GATE', '%s 同周期已建仓（setup 未重置）' % undl, key='G' + undl)
        return

    # 上限
    total = sum(p['qty'] for p in positions)
    per_undl = sum(p['qty'] for p in positions if _undl_of(C, p['code']) == undl)
    if per_undl >= cfg['max_undl'] or total >= cfg['max_total']:
        _log('GATE', '%s 达张数上限 undl=%d/%d total=%d/%d' % (
            undl, per_undl, cfg['max_undl'], total, cfg['max_total']), key='L' + undl)
        return
    if S.entries_today >= cfg['max_day']:
        _log('GATE', '每日开仓上限 %d 已达' % cfg['max_day'], key='D')
        return

    # 选档
    cands = _build_cands(C, undl, cfg)
    if not cands:
        _log('GATE', '%s 无可用候选（到期月/DTE）' % undl, key='C' + undl)
        return
    held = set([p['code'] for p in positions])
    cands = [c for c in cands if c['code'] not in held]   # 同合约已有持仓 → 不再开（券商合并持仓）
    if not cands:
        _log('GATE', '%s 候选均已持仓（同合约不可重复开）' % undl, key='X' + undl)
        return
    spot = _spot(C, undl, bars)
    if spot <= 0:
        _log('GATE', '%s 取不到标的价' % undl, key='P' + undl)
        return
    if _BT['on']:
        # 回测：先按虚值门粗筛，少读一堆期权行情；盘口用 bar 收盘价 ± 半价差
        gate = spot * (1.0 - _min_dist(undl) / 100.0)
        cands = [c for c in cands if c['strike'] <= gate]
        if not cands:
            _log('GATE', '%s 无虚值达标候选 spot=%.4f' % (undl, spot), key='Q' + undl)
            return
        qs = _bt_quotes(C, [c['code'] for c in cands])
    else:
        qs = _ticks(C, [c['code'] for c in cands])
    pick = pick_put(cands, spot, _min_dist(undl), qs, cfg['fee_sell'])
    if pick is None:
        _log('GATE', '%s 候选均不满足虚值/权利金条件 spot=%.4f' % (undl, spot), key='Q' + undl)
        return

    # 资金门
    acc = _account(C)
    need = pick['margin'] * 1.2
    if acc['avail'] > 0 and need > acc['avail']:
        _log('GATE', '%s 资金不足 需%.0f 可用%.0f' % (undl, need, acc['avail']), key='M' + undl)
        return
    if acc['avail'] > 0 and pick['margin'] > acc['avail'] * cfg['budget_pct'] / 100.0:
        _log('GATE', '%s 保证金预算超限 margin=%.0f 预算=%.0f' % (
            undl, pick['margin'], acc['avail'] * cfg['budget_pct'] / 100.0), key='B' + undl)
        return

    if _BT['on']:
        # 回测：内存模拟建仓，不发单、不建在途委托
        _bt_open(pick, undl, cfg)
        S.entries_today += 1
        cy['bought'] = True
        cy['reset'] = False
        _log('ENTRY', '[模拟] %s -> %s strike=%.4f bid=%.4f 权利金=%.1f margin=%.0f yield=%.2f%%' % (
            undl, pick['code'], pick['strike'], pick['bid'],
            pick['bid'] * pick['unit'] - cfg['fee_sell'], pick['margin'], pick['yield']), force=True)
        return

    remark = '%s|%s|s%d|%.2f' % (STRAT_TAG, undl.split('.')[0], cur, spot)
    ref = sell_open(C, pick['code'], 1, pick['bid'], remark[:24])
    if not _can_trade():
        # DRY：按「已模拟建仓」记账，便于观察后续逻辑，不建在途委托
        S.entries_today += 1
        cy['bought'] = True
        cy['reset'] = False
    elif not _ref_ok(ref):
        _log('ENTRY', '%s 下单失败 ref=%r（本周期不再重试）' % (undl, ref), force=True)
        S.fail_streak += 1
        cy['bought'] = True
        cy['reset'] = False
    else:
        S.entries_today += 1
        cy['bought'] = True
        cy['reset'] = False
        S.pending[pick['code']] = {'kind': 'open', 'ref': ref, 'ts': _ts(),
                                   'px': pick['bid'], 'qty': 1, 'undl': undl,
                                   'remark': remark}
    _log('ENTRY', '%s -> %s strike=%.4f bid=%.4f net=%.1f margin=%.0f yield=%.2f%%' % (
        undl, pick['code'], pick['strike'], pick['bid'], pick['net'], pick['margin'],
        pick['yield']), force=True)


def _undl_of(C, code):
    """由合约反查标的"""
    u = S.code_undl.get(code)
    if u:
        return u
    d = _safe('DETAIL', C.get_option_detail_data, code)
    if isinstance(d, dict):
        u = str(d.get('OptUndlCode', '') or '')
        if u:
            if '.' not in u:
                u = u + ('.SH' if u.startswith('5') else '.SZ')
            S.code_undl[code] = u
            return u
    return ''


def _same_undl(d, undl):
    """契约校验：detail 的 OptUndlCode 必须属于当前标的（防跨标的串仓）"""
    u = str(d.get('OptUndlCode', '') or '')
    if not u:
        return True
    base = undl.split('.')[0]
    return u.split('.')[0] == base


def _build_cands(C, undl, cfg):
    """最近可用到期月的 put 候选"""
    today = _ymd_now()
    for month in _months(C, undl, 3):
        out = []
        for code, d in _chain(C, undl, month):
            rt = _opt_right(d)
            if rt == 'C':
                continue
            exp = d.get('ExpireDate')
            dte = _dte(exp, today)
            if dte < cfg['min_dte']:
                continue
            if not _same_undl(d, undl):
                continue
            unit = _i(d.get('OptUnit', 0))
            if unit <= 0:
                continue
            mr = _f(d.get('ShortMarginRatio', 0))
            if mr <= 0:
                mr = 7.0
            S.code_undl[code] = undl
            out.append({
                'code': code,
                'strike': _f(d.get('OptExercisePrice', 0)),
                'exp': exp,
                'dte': dte,
                'unit': unit,
                'mr': mr,
            })
        if out:
            _log('CHAIN', '%s month=%s put=%d dte_range=%s' % (
                undl, month, len(out), sorted(set([c['dte'] for c in out]))), key='H' + undl)
            return out
    return []


def _spot(C, undl, bars):
    # 回测里 get_full_tick 给的是实时快照（不是历史），必须用 bar 收盘价
    if _BT['on']:
        return _f(bars[-1]['c']) if bars else 0.0
    q = _ticks(C, [undl]).get(undl) or {}
    px = _f(q.get('last'))
    if px > 0:
        return px
    if bars:
        return _f(bars[-1]['c'])
    return 0.0


def tick5s(C):
    """秒级巡检：日切 → 委托回收 → 止盈/强平"""
    if S.blocked or (BT_ONLY and not bool(_gv(C, 'do_back_test', False))):
        return
    _day_roll()
    if not _in_session():
        return
    _poll_orders(C)
    positions = _positions(C)
    _do_exits(C, positions)


def handlebar(C):
    """主循环：每根主图 bar 一次（回测下逐根重放）"""
    is_bt = bool(_gv(C, 'do_back_test', False))
    S.hb_calls += 1
    if is_bt and S.hb_calls <= 3:
        try:
            tg = C.get_bar_timetag(C.barpos)
        except BaseException as e:
            tg = 'ERR:%s' % type(e).__name__
        _log('BARS', 'handlebar #%d barpos=%r timetag=%r do_bt=%r' % (
            S.hb_calls, getattr(C, 'barpos', '?'), tg, is_bt), force=True)
    if BT_ONLY and not is_bt:
        if not S.blocked:
            S.blocked = 1
            _log('INIT', 'BT_ONLY=1（验证期）：本实例不是回测形态 → handlebar 全部跳过'
                 '（不计算、不下单、无日志）。要恢复实盘请把文件顶部 BT_ONLY 改成 0', force=True)
        return
    if S.blocked:
        return
    if is_bt:
        _BT['on'] = True
        _bt_sync(C)
        if not _BT.get('closed'):
            _last = False
            try:
                _last = bool(C.is_last_bar())
            except BaseException:
                _last = False
            if _last or (str(_BT['ymd']) >= BT_END and _BT['hm'] >= 1455):
                _BT['closed'] = 1
                S.in_bt = 0
                _log('BTSUM', '回测区间结束（%s~%s，最后交易日收尾）============' % (
                    BT_START, BT_END), force=True)
                _bt_summary(force=True)
                return
        if not _bt_in_range():
            if S.in_bt:
                S.in_bt = 0
                _log('BTSUM', '回测区间结束（已走出 %s~%s）' % (BT_START, BT_END), force=True)
                _bt_summary(force=True)
            return                    # 区间外（如 2017-2025）静默跳过，不计算
        S.in_bt = 1
        S.bt['bar'] += 1
        if not S.bt_first:
            S.bt_first = 1
            _log('BARS', '回测区间生效 %s~%s 首根=%08d%04d' % (
                BT_START, BT_END, _BT['ymd'], _BT['hm']), force=True)
    _day_roll()
    if not is_bt:
        try:
            if not C.is_last_bar():
                return
            if not _in_session():
                return
        except BaseException:
            pass

    _poll_orders(C)
    if is_bt:
        _bt_exits(C, _cfg())          # 内存持仓：到期结算 / 强平 / 止盈
        positions = _bt_positions()
    else:
        positions = _positions(C)
        _do_exits(C, positions)

    if S.fail_streak >= _i(_g('MAX_ORDER_FAIL', MAX_ORDER_FAIL)):
        return

    for undl in _undl_list():
        try:
            _try_entry(C, undl, positions, _cfg())
        except BaseException as e:
            _log('ERR', '%s 开仓判定异常 %s: %s' % (undl, type(e).__name__, str(e)[:120]),
                 force=True)

    if is_bt:
        _bt_summary()
    elif _i(_g('LOG_VERBOSE', LOG_VERBOSE)) == 1:
        _log('SUM', '持仓=%d 张 今日开仓=%d 待确认=%d' % (
            sum(p['qty'] for p in positions), S.entries_today, len(S.pending)), key='SUM')
