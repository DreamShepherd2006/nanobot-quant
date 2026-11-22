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
MAX_ENTRIES_PER_DAY = 0        # 每日开仓次数上限（0 = 不限制）
# 2026-11-22 由 4 改为 0：敞口已由张数上限控制，日限会把「同一天多个标的买9」
# 的样本大量吞掉（11-13 实测 12 个信号只成交 4 笔）。需要收紧时填正整数，INIT 会打生效值。
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
# 过程日志粒度（只影响带 key 的"进度/拦截"类日志，动作类 force 行不受影响）：
#   2 = 内容变了就打（旧行为，最啰嗦）
#   1 = 每个 key 每天最多 1 行，重复次数进 [NOISE] 汇总（默认）
#   0 = 过程行一条不打，只在日切/收尾的 [NOISE] 行给出折叠条数
LOG_LEVEL = 1
BT_HALF_SPREAD_PCT = 1.0       # 已废弃：2026-11-22 起回测走平台记账、撮合价=bar close；仅留兼容
# 回测区间（验证期写死：QMT 回测面板的起止时间不传给自定义策略，只能自己卡）
# 格式 YYYYMMDD；置空串 = 不限。两头均含。
BT_START = '20261113'
BT_END = '20261120'
# 运行形态锁（验证期）：1=只允许回测形态运行，模拟信号/实盘启动后直接静默退出
# 0=跟随 QMT（原来的三形态行为）。验证完回测后改回 0 即可实盘。
BT_ONLY = 1
# 构建标识：每次改代码就改这里，日志首行会打出来 —— 用来确认 QMT 里跑的是哪一版
BUILD = '20261122-0358'
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


def _norm_code(code, exch=''):
    """合约代码统一带市场后缀（2026-11-22 探针实测结论）

    纯数字代码（如 10012448）调 get_market_data_ex 不报错、
    但静默返回 shape=(0,12) 的空表 —— 止盈因此恒 hold。
    10012448      -> 10012448.SHO
    10012448.SHO  -> 原样
    只对 8 位纯数字期权代码猜后缀（沪 1 开头 SHO / 深 9 开头 SZO），
    其他形态（如已带 .SH 的 ETF 标的）一律原样返回，不做猜测。
    """
    c = str(code or '').strip()
    if not c:
        return ''
    if '.' in c:
        return c
    if not (c.isdigit() and len(c) == 8):
        return c
    e = str(exch or '').strip().upper()
    if not e:
        e = 'SZO' if c.startswith('9') else 'SHO'
    return c + '.' + e


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
    """日志：带 key 的过程行按 LOG_LEVEL 折叠（防刷屏），force 行照常打

    折叠规则（只作用于 key 类过程日志）：
      LOG_LEVEL=2 内容变了就打；=1 每 key 每天最多 1 行；=0 一行不打
    被折叠掉的条数不清零丢弃，由 _noise_flush() 在日切/回测收尾汇总成
    [NOISE] 一行 —— 降噪不等于静默（保留可观测性）。

    回测期间 tag 统一加 BT. 前缀，与模拟信号/实盘实例的日志彻底分开。
    若日志文件可用（S.logf），同时写盘（时间戳用回测虚拟时钟，与 bar 对齐）。
    """
    if _BT['on'] and not tag.startswith('BT'):
        tag = 'BT.' + tag
    if key is not None and not force:
        lv = _i(_g('LOG_LEVEL', LOG_LEVEL))
        d = _ymd_now()
        rec = _PRN.get(key)
        if lv >= 2:                      # 旧行为：内容变化才打
            if rec is not None and rec[0] == d and rec[1] == msg:
                return
            _PRN[key] = [d, msg, 0]
        else:
            if rec is not None and rec[0] == d:   # 今天已经打过了 -> 只计数
                rec[2] += 1
                return
            _PRN[key] = [d, '' if lv == 0 else msg, 0]
            if lv == 0:                  # 连首条也不打，只保留计数
                _PRN[key][2] = 1
                return
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


def _noise_flush(tag='NOISE'):
    """把本轮折叠掉的过程日志条数汇总成一行（避免"静默降噪"）

    在日切与回测收尾各调一次：条数清零，key 保留（当天仍只打首条）。
    """
    n = 0
    parts = []
    for k in list(_PRN.keys()):
        rec = _PRN[k]
        c = rec[2] if len(rec) > 2 else 0
        if c > 0:
            n += c
            if len(parts) < 6:
                parts.append('%s x%d' % (k, c))
            rec[2] = 0
    if n > 0:
        _log(tag, '过程日志折叠 %d 条 | %s%s'
             % (n, ' '.join(parts), ' ...' if len(parts) >= 6 else ''), force=True)


def _safe(tag, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        _log(tag, 'EXC %s: %s' % (type(e).__name__, str(e)[:150]), key=tag + str(e)[:20])
        return None


# ====================== 2. 状态 ======================
def _new_bt():
    """回测账本初值（每次 init 重置）。
    real = 已实现盈亏：逐笔 (卖开价 - 买平价) x 份数 x 张数 - 双边手续费
    pos  = 未平仓义务仓 {code: {...}}，浮盈亏按当根收盘重算
    """
    return {'bar': 0, 'opens': 0, 'closed': 0, 'tp': 0, 'force': 0,
            'itm': 0, 'otm': 0, 'prem': 0.0, 'pnl': 0.0,
            'win': 0, 'loss': 0, 'hold': [], 'stock_qty': 0, 'stock_cost': 0.0,
            # 报告层成本修正（平台撮合按 bar close 成交 = 零价差，须自行扣）
            'spr': 0.0, 'fee': 0.0, 'init_bal': None,
            'real': 0.0, 'pos': {}}


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
        self.pd_raw = {}      # [POSDIAG] 已打印过原始字段的持仓（id@exch，全程一次）
        self.code_unit = {}   # code -> 每张份数（OptUnit；A股 ETF 期权 10000）
        self.opened_round = 0         # 本轮（当前 bar）已开张数：同 bar 多标的不许突破上限
        self.opened_round_undl = {}   # 本轮各标的已开张数
        self.bt = _new_bt()


S = _St()


# ====================== 3. 数据层 ======================
def _bars(C, code, period, count):
    """返回 [{'t':str14,'o','h','l','c','v'}] 升序；失败返回 []

    回测必须限定 end_time：QMT 在 start/end 都为空时返回「到最新为止」的
    最近 count 根，回测里等于未来数据（2017 年 bar 会拿到 2026 年的价）。
    实盘（非回测）仍传空串，行为与改动前一致。
    """
    code = _norm_code(code)
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
    codes = [_norm_code(c) for c in (codes or [])]
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
        out[_norm_code(code)] = {
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
        code = _norm_code(str(code))
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
    """期权持仓 -> [{code,qty,avail,open,margin}]

    方向标记（2026-11-22 实测）：
      实盘义务仓（卖方）= m_eSideFlag '1'
      回测虚拟账号        = '0'，且 m_dRealUsedMargin 恒 0（保证金在 m_dMargin）
    两种都接受，其余方向跳过。保证金优先 m_dRealUsedMargin，回测回退 m_dMargin。
    """
    out = []
    lst = _safe('POS', _tdetail, C, 'position')
    if not lst:
        return out
    for p in lst:
        side = str(_gv(p, 'm_eSideFlag', ''))
        if side == '1':
            pass
        elif side == '0' and _BT['on']:
            pass
        else:
            continue
        m = _f(_gv(p, 'm_dRealUsedMargin', 0))
        if m <= 0:
            m = _f(_gv(p, 'm_dMargin', 0))
        if m <= 0:
            m = _f(_gv(p, 'm_dOpenCost', 0))
        out.append({
            'code': _norm_code(_gv(p, 'm_strInstrumentID', ''),
                               _gv(p, 'm_strExchangeID', '')),
            'qty': _i(_gv(p, 'm_nVolume', 0)),
            'avail': _i(_gv(p, 'm_nCanUseVolume', 0)),
            'open': _f(_gv(p, 'm_dOpenPrice', 0)),
            'margin': m,
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
    """三重防呆：模型研究无账号 / 模拟信号模式不成交 / LIVE_CONFIRM 代码锁
    回测态放行：走的是平台虚拟账号 + 虚拟资金，不需要实盘锁。"""
    if _BT['on']:
        return True
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
    """下单：限价 prType=11 + px。
    回测态 px 取 bar close，必落在 [low,high] 内，平台按 close 成交（不会触发改价 WARNING）。
    回测态中签名异常时回退短签名（两个探针分别验证过各自的可用性）。
    """
    fn = globals().get('passorder')
    if fn is None:
        _log('ORDER', 'passorder 不存在（模型研究/模拟模式？）', force=True)
        return None
    code = _norm_code(code)
    px = round(px, 4)
    r = _safe('ORDER', fn, op, 1101, S.acct, code, 11, px, int(qty),
              STRAT_TAG, _i(_g('PASSORDER_QUICK', PASSORDER_QUICK)), remark, C)
    if r is None and _BT['on']:
        r = _safe('ORDER', fn, op, 1101, S.acct, code, 11, px, int(qty), C)
        if r is not None:
            _log('ORDER', '[回测] 中签名失败，短签名成功 ref=%r' % (r,), force=True)
    return r


def sell_open(C, code, qty, px, remark):
    if not _can_trade():
        _log('DRY', '本应 卖开 %s x%d @%.4f | %s' % (code, qty, px, remark), force=True)
        return None
    ref = _passorder(52, code, qty, px, remark, C)
    if _BT['on']:
        # 回测态 passorder 返回值不可靠，用虚拟账号持仓复核（成交即时生效，v6 实测）
        ok = _bt_verify(C, code, True)
        if ok:
            _fee = _f(_g('FEE_SELL', FEE_SELL)) * max(1, _i(qty))
            S.bt['spr'] += _spread_cost(px, qty)
            S.bt['fee'] += _fee
            _bt_note_open(code, px, qty, _fee)
        _log('SELL', '卖开 %s x%d @%.4f [回测]%s ref=%r | %s' % (
            code, qty, px, '已成交' if ok else '未见持仓(未成交)', ref, remark), force=True)
        return 'bt-ok' if ok else None
    _log('SELL', '卖开 %s x%d @%.4f ref=%r | %s' % (code, qty, px, ref, remark), force=True)
    return ref


def buy_close(C, code, qty, px, remark):
    if not _can_trade():
        _log('DRY', '本应 买平 %s x%d @%.4f | %s' % (code, qty, px, remark), force=True)
        return None
    ref = _passorder(53, code, qty, px, remark, C)
    if _BT['on']:
        ok = _bt_verify(C, code, False)
        if ok:
            _fee = _f(_g('FEE_BUY', FEE_BUY)) * max(1, _i(qty))
            S.bt['spr'] += _spread_cost(px, qty)
            S.bt['fee'] += _fee
            _bt_note_close(code, px, qty, _fee)
        _log('COVER', '买平 %s x%d @%.4f [回测]%s ref=%r | %s' % (
            code, qty, px, '已平掉' if ok else '持仓仍在(未成交)', ref, remark), force=True)
        return 'bt-ok' if ok else None
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


# ====================== 5.5 回测（平台记账，仅 do_back_test）======================
# 2026-11-22 探针实测结论（build 20261122-0040）：
#   [OK] passorder(52 卖开 / 53 买平) 回测态真实下单，虚拟账号按股票式记账
#   [OK] 持仓/乘数(10000 份每张)/浮盈/盈亏 精确：(0.0345-0.0322)*10000 = +23 与账本逐位一致
#   [OK] T+0 当天开平放行；限价落在 bar[low,high] 内按限价，否则退回 close 并打系统 WARNING
#   [OK] 手续费按手：set_commission(1,[0,0,1.2,1.7,1.7,0]) 实测精确扣 1.2 / 1.7 元
#   [!]  保证金按 权利金×ShortMarginRatio 冻结（1 张冻 24 元；真实 MarginUnit=3809.6）→ 严重低估
#   [!]  POSITION 的 m_eSideFlag 回测态为 '0'，m_dRealUsedMargin 恒 0（保证金在 m_dMargin）
#   [!]  滑点参数实测无效、无历史盘口 → 撮合价 = bar close（价差影响在报告层单列）
#   [X]  无期权到期结算 → 必须自己 DTE 平仓（FORCE_CLOSE_DTE）
# 因此回测与实盘共用同一路径：_positions 读平台持仓、sell_open/buy_close 发 passorder。
# 回测态只有三处差异：虚拟时钟、close 报价、委托即时成交（不建在途委托）。


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
    """回测区间闸（代码内）：区间外静默跳过，不计算不下单。
    注意：QMT 回测面板的起止时间对本策略的 handlebar 回放有效（v3~v6 实测首根=20261113 09:30），
    这里的 BT_START/BT_END 只是双保险，防止面板设成宽区间时白跑。
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


# ---- 报告层成本修正：平台按 bar close 撮合 = 零价差；此处的口径来自真实盘口实测 ----
# 510050 / 588000（2026-12 档）实测：虚值 put 价差 1-3 tick（1 tick = 1 元/张），
# 深实值 put 40-95 tick，折成权利金约 2-2.6%，单边取 1.25%、并以 60 元/张封顶。
SPREAD_OTM_YUAN = 1.0      # 虚值单边：1 tick
SPREAD_ITM_PCT = 1.25      # 实值单边：权利金 × 1.25%
SPREAD_ITM_MIN_PX = 0.10   # 权利金高于此值才按实值档（虚值档权利金远低于它）
SPREAD_ITM_CAP = 60.0      # 单边封顶（元/张），防深实值按比例高估


def _spread_cost(px, qty):
    """单边价差成本（元）：卖开吃 bid、买回付 ask 相对 mid 的损耗。
    虚值 1 元/张；实值 px×1.25%（下限 1 元、上限 60 元/张）。
    """
    px = _f(px)
    per = SPREAD_OTM_YUAN
    if px > SPREAD_ITM_MIN_PX:
        per = px * 10000.0 * SPREAD_ITM_PCT / 100.0
        per = max(SPREAD_OTM_YUAN, min(per, SPREAD_ITM_CAP))
    return per * max(1, _i(qty))


# ---- 回测自建成交账本：平台余额含义务仓市值重估，不能当盈亏直接用 ----
def _bt_note_open(code, px, qty, fee):
    """记一笔卖开成交（用于自算已实现 / 按收盘重算浮盈亏）"""
    q = max(1, _i(qty))
    unit = _i(S.code_unit.get(code, 0)) or 10000
    S.bt['pos'][code] = {'code': code, 'open': _f(px), 'qty': q,
                         'fee': _f(fee), 'unit': unit,
                         'ymd': _BT['ymd'], 'hm': _BT['hm']}


def _bt_note_close(code, px, qty, fee):
    """记一笔买平成交：已实现 = (卖开价-买平价) x 份数 x 张数 - 双边手续费"""
    rec = S.bt['pos'].pop(code, None)
    if not rec:
        return
    n = min(max(1, _i(qty)), rec['qty'])
    pnl = (rec['open'] - _f(px)) * rec['unit'] * n
    pnl -= rec['fee'] * (float(n) / max(1, rec['qty'])) + _f(fee)
    S.bt['real'] += pnl
    if pnl >= 0:
        S.bt['win'] += 1
    else:
        S.bt['loss'] += 1


def _note_round_open(undl):
    """记一笔本轮已开；同 bar 后续标的的上限判定必须看得到它"""
    S.opened_round += 1
    S.opened_round_undl[undl] = _i(S.opened_round_undl.get(undl, 0)) + 1


def _bt_verify(C, code, want_open):
    """回测成交复核：直接看平台虚拟账号持仓是否按预期变化。
    want_open=True  期望出现该合约持仓（卖开成交）
    want_open=False 期望该合约持仓消失（买平成交）
    """
    code = _norm_code(code)
    try:
        for p in _positions(C):
            if p['code'] == code:
                return bool(p['qty'] > 0) if want_open else False
    except BaseException:
        pass
    return False if want_open else True


def _bt_quotes(C, codes):
    """回测报价：无历史盘口，last = bid = ask = 该 bar 收盘价。
    价差（真实卖吃 bid / 买付 ask）无法在撮合层建模——发低限价会被平台按 close 兜底成交，
    反而凭空造出成交。改由报告层 _spread_cost() 逐笔扣（见回测实测口径）。
    """
    out = {}
    for code in codes:
        px = _bt_last_px(C, code)
        if px <= 0:
            continue
        out[code] = {'last': px, 'bid': px, 'ask': px}
    return out


def _bt_posdiag(C, positions):
    """[POSDIAG] 回测持仓诊断：每交易日每合约一行（只读，不参与任何决策）

    一次跑通即可判定三条静默失效链：
      RAW id/exch      -> 持仓代码是否带交易所后缀（不带则与策略 code 字面不等，
                          _bt_verify 恒 False，卖开日志误报「未见持仓(未成交)」）
      bars=0 / last=0  -> 该合约回测态取不到 1m，decide_exit 里 bid<=0 -> 恒 hold
      dte=-1           -> get_option_detail_data 无 ExpireDate，DTE 强平失效
    日志量：每交易日每合约 <=1 行 + 每合约原始字段 1 行（全程）。
    """
    if not positions:
        return
    ymd = _BT['ymd']
    per = str(_g('SIGNAL_PERIOD', SIGNAL_PERIOD))
    tp = _f(_g('TP_PCT_PUT', TP_PCT_PUT))
    rows = []
    for p in positions:
        code = p['code']
        if S.bh_day.get(code) == ymd:
            continue
        S.bh_day[code] = ymd
        nb = len(_bars(C, code, per, 6))
        last = _bt_last_px(C, code)
        dte = _detail_dte(C, code)
        op = _f(p['open'])
        # last<=0 = 没有行情，回落率无意义（打 n/a，不能算成 100% 误导成本该止盈）
        drop = ('%.1f%%' % ((op - last) / op * 100.0)) if (op > 0 and last > 0) else 'n/a'
        rows.append('%s bars=%d last=%.4f drop=%s dte=%d open=%.4f qty=%d'
                    % (code, nb, last, drop, dte, op, p['qty']))
    if rows:
        _log('POSDIAG', 'ymd=%d 在仓=%d tp=%.0f%% | %s'
             % (ymd, len(positions), tp, ' || '.join(rows)), force=True)
    if S.pd_raw:
        return
    lst = _safe('POS', _tdetail, C, 'position') or []
    for r in lst:
        cid = str(_gv(r, 'm_strInstrumentID', ''))
        ex = str(_gv(r, 'm_strExchangeID', ''))
        k = cid + '@' + ex
        if k in S.pd_raw:
            continue
        S.pd_raw[k] = 1
        _log('POSDIAG', 'RAW id=%r exch=%r side=%r vol=%s canuse=%s open=%s margin=%s rmargin=%s'
             % (cid, ex, str(_gv(r, 'm_eSideFlag', '')), _gv(r, 'm_nVolume', 0),
                _gv(r, 'm_nCanUseVolume', 0), _gv(r, 'm_dOpenPrice', 0),
                _gv(r, 'm_dMargin', 0), _gv(r, 'm_dRealUsedMargin', 0)), force=True)


def _bt_unreal(C):
    """未平仓义务仓浮盈亏（按当根收盘重算，单一取价口径 = bar close）

    取不到行情时按开仓价计（浮盈亏 0，明细标 n/a），不拿别的价顶替。
    返回 (浮盈亏, [明细字符串])
    """
    tot = 0.0
    det = []
    for code, rec in (S.bt.get('pos') or {}).items():
        last = _bt_last_px(C, code) if C is not None else 0.0
        if last <= 0:
            last = rec['open']
            tag = 'n/a'
        else:
            tag = '%+.1f%%' % ((rec['open'] - last) / rec['open'] * 100.0)
        pnl = (rec['open'] - last) * rec['unit'] * rec['qty'] - rec['fee']
        tot += pnl
        det.append('%s x%d 浮%+.1f(%s,open=%.4f,last=%.4f)' % (
            code, rec['qty'], pnl, tag, rec['open'], last))
    return tot, det


def _bt_summary(C=None, force=False):
    """回测总账：自建成交账本（已实现 + 按当根收盘重算浮盈亏）。

    为什么不再直接拿平台余额减初始：QMT 回测把义务仓市值当正资产又加了一次
    （实测卖开 1 张权利金 122 元、余额 +242.8；权利金跌=浮盈时余额反而降），
    净额方向会跟真实盈亏相反。平台余额只保留作交叉核对（并标清口径）。
    """
    try:
        acc = _account(C) if C is not None else {'avail': 0.0, 'balance': 0.0, 'margin': 0.0}
        pos = _positions(C) if C is not None else []
    except BaseException:
        acc = {'avail': 0.0, 'balance': 0.0, 'margin': 0.0}
        pos = []
    real = _f(S.bt.get('real'))
    unreal, det = _bt_unreal(C)
    net = real + unreal - _f(S.bt['spr'])
    sig = (round(real, 2), round(unreal, 2), len(pos))
    if not force and sig == S.bt_sig and S.bt['bar'] % 400 != 0:
        return
    S.bt_sig = sig
    if S.bt.get('init_bal') is None and acc['balance'] > 0:
        # 首次汇总发生在任何成交之前（handlebar 末尾才建仓），即平台注入的初始资金
        S.bt['init_bal'] = acc['balance']
    held = ' '.join('%s×%d' % (p['code'], p['qty']) for p in pos[:8]) or '无'
    _log('BTSUM', 'bar=%d 开=%d 平=%d | 自算 已实现=%+.2f 浮盈亏=%+.2f 费=%.2f 差=%.2f '
                  '净=%+.2f | 在仓=%d张 余额=%.2f(含市值重估) | %s'
         % (S.bt['bar'], S.bt['opens'], S.bt['closed'], real, unreal,
            S.bt['fee'], S.bt['spr'], net, len(pos), acc['balance'], held), force=True)
    if force:
        init = S.bt.get('init_bal') or 0.0
        _log('BTSUM', '收尾 | 初始资金=%.2f 期末余额=%.2f（含义务仓市值重估，仅供核对注入金额）'
                      ' | 胜/负=%d/%d | 自算 已实现=%+.2f 浮盈亏=%+.2f 费=%.2f（已计入）'
                      ' 差=%.2f 净=%+.2f'
             % (init, acc['balance'], S.bt['win'], S.bt['loss'], real, unreal,
                S.bt['fee'], S.bt['spr'], net), force=True)
        if det:
            _log('BTSUM', '收尾未平仓明细 | %s' % ' || '.join(det), force=True)


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
    S.pd_raw = {}
    S.code_unit = {}
    S.opened_round = 0
    S.opened_round_undl = {}
    S.in_bt = 0
    S.bt_sig = None
    S.bt = _new_bt()        # 点一次回测会起两个实例（模拟+回测），账本必须每次 init 清空
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
    _md = _i(_g('MAX_ENTRIES_PER_DAY', MAX_ENTRIES_PER_DAY))
    _log('INIT', '开仓闸门：在仓上限=%d张 单标的上限=%d张 每日开仓上限=%s' % (
        _i(_g('MAX_TOTAL', MAX_TOTAL)), _i(_g('MAX_PER_UNDL', MAX_PER_UNDL)),
        '不限' if _md <= 0 else _md), force=True)
    if bool(_gv(C, 'do_back_test', False)):
        _log('INIT', '回测区间闸（代码内双保险）%s ~ %s：区间外静默跳过'
             % (BT_START or '不限', BT_END or '不限'), force=True)
        _log('INIT', '主图=%s.%s period=%r' % (
            getattr(C, 'stockcode', '?'), getattr(C, 'market', '?'),
            getattr(C, 'period', '?')), force=True)
    _log('INIT', 'global fn: get_trade_detail_data=%r passorder=%r cancel=%r' % (
        'get_trade_detail_data' in globals(), 'passorder' in globals(),
        'cancel' in globals()), force=True)
    if _BT['on']:
        # 平台记账：手续费按手（commissionType=1），实测卖开 1.2 / 买平 1.7 元精确扣除
        _fee_s = _f(_g('FEE_SELL', FEE_SELL))
        _fee_b = _f(_g('FEE_BUY', FEE_BUY))
        try:
            C.set_commission(1, [0, 0, _fee_s, _fee_b, _fee_b, 0])
            _log('INIT', '回测平台记账：set_commission(按手) ok 卖开%.2f/买平%.2f 元每张'
                 % (_fee_s, _fee_b), force=True)
        except BaseException as e:
            _log('INIT', 'set_commission 失败 %s：手续费不会扣，需报告层手工统计'
                 % (type(e).__name__,), force=True)
        _log('INIT', '回测撮合口径：成交价=bar close（滑点参数实测无效、无历史盘口）；'
             '保证金按平台比例（严重低估）；价差需报告层修正', force=True)
        _log('INIT', '回测记账：自建成交账本（已实现 + 当根收盘重算浮盈亏）；'
             '平台余额含义务仓市值重估、只作交叉核对，不当盈亏', force=True)

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
        _noise_flush()                # 昨日被折叠的重复过程日志，收尾汇总一行
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
    if _BT['on']:
        qs = _bt_quotes(C, [p['code'] for p in positions])   # 回测无盘口：last=bid=ask=close
    else:
        qs = _ticks(C, [p['code'] for p in positions])
    for p in positions:
        code = p['code']
        if code in S.pending:          # 已有在途委托，不重复下
            continue
        if S.dry_skip.get(code) and not _can_trade():
            continue
        if (not _BT['on']) and (_ts() - S.closed_ts.get(code, 0)) < _i(
                _g('CLOSE_COOLDOWN_S', CLOSE_COOLDOWN_S)):
            continue      # 冷却只用于防实盘持仓快照延迟；回测用墙钟会把后续止盈一起锁掉
        q = qs.get(code) or {}
        act, reason, px = decide_exit(p, q, cfg)
        if cfg['force_dte'] == 1:
            d = _detail_dte(C, code)
            if 0 <= d <= cfg['force_n']:
                act, reason, px = 'close', 'dte%d' % d, _f(q.get('ask')) or _f(q.get('bid'))
        if act != 'close':
            if not q:      # 取不到该合约行情 -> 止盈无法判定（只提示一次，明细见 [POSDIAG]）
                _log('HOLD', '%s 无行情 止盈无法判定 open=%.4f' % (code, p['open']),
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
        if _BT['on']:
            S.bt['closed'] += 1       # 回测：买平即时成交
            S.closed_ts[code] = _ts()
        else:
            S.pending[code] = {'kind': 'close', 'ref': ref, 'ts': _ts(),
                               'px': px, 'qty': qty, 'remark': reason}


def _detail_dte(C, code):
    d = _safe('DETAIL', C.get_option_detail_data, _norm_code(code))
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

    # 上限（必须把「本轮已开、但持仓快照还没反映」的张数算进来，否则同一根 bar
    #  多个标的同时买9 会各自看到旧的 total、一起突破 max_total：11-13 实测 4 笔到 12 张）
    total = sum(p['qty'] for p in positions) + S.opened_round
    per_undl = (sum(p['qty'] for p in positions if _undl_of(C, p['code']) == undl)
                + _i(S.opened_round_undl.get(undl, 0)))
    if per_undl >= cfg['max_undl'] or total >= cfg['max_total']:
        _log('GATE', '%s 达张数上限 undl=%d/%d total=%d/%d' % (
            undl, per_undl, cfg['max_undl'], total, cfg['max_total']), key='L' + undl)
        return
    if cfg['max_day'] > 0 and S.entries_today >= cfg['max_day']:
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
        # 回测：先按虚值门粗筛，少读一堆期权行情；报价一律取 bar 收盘价（无历史盘口）
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

    remark = '%s|%s|s%d|%.2f' % (STRAT_TAG, undl.split('.')[0], cur, spot)
    ref = sell_open(C, pick['code'], 1, pick['bid'], remark[:24])
    if not _can_trade():
        # DRY：按「已模拟建仓」记账，便于观察后续逻辑，不建在途委托
        S.entries_today += 1
        _note_round_open(undl)
        cy['bought'] = True
        cy['reset'] = False
    elif not _ref_ok(ref):
        _log('ENTRY', '%s 下单失败 ref=%r（本周期不再重试）' % (undl, ref), force=True)
        S.fail_streak += 1
        cy['bought'] = True
        cy['reset'] = False
    else:
        S.entries_today += 1
        _note_round_open(undl)
        cy['bought'] = True
        cy['reset'] = False
        if _BT['on']:
            S.bt['opens'] += 1        # 回测：委托即时成交，不建在途委托
        else:
            S.pending[pick['code']] = {'kind': 'open', 'ref': ref, 'ts': _ts(),
                                       'px': pick['bid'], 'qty': 1, 'undl': undl,
                                       'remark': remark}
    _log('ENTRY', '%s -> %s strike=%.4f bid=%.4f net=%.1f margin=%.0f yield=%.2f%%' % (
        undl, pick['code'], pick['strike'], pick['bid'], pick['net'], pick['margin'],
        pick['yield']), force=True)


def _undl_of(C, code):
    """由合约反查标的"""
    code = _norm_code(code)
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
            S.code_unit[code] = unit
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
    """秒级巡检：日切 → 委托回收 → 止盈/强平
    回测态直接返回：回测由 handlebar 逐根驱动，run_time 在回测里不可靠，
    若被触发会与 handlebar 重复平仓/重复下单。"""
    if S.blocked or (BT_ONLY and not bool(_gv(C, 'do_back_test', False))):
        return
    if bool(_gv(C, 'do_back_test', False)):
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
                _noise_flush()
                _bt_summary(C, force=True)
                return
        if not _bt_in_range():
            if S.in_bt:
                S.in_bt = 0
                _log('BTSUM', '回测区间结束（已走出 %s~%s）' % (BT_START, BT_END), force=True)
                _noise_flush()
                _bt_summary(C, force=True)
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

    if is_bt:
        positions = _positions(C)     # 平台虚拟账号持仓
        _bt_posdiag(C, positions)     # [POSDIAG] code/行情/DTE 缺口诊断（只读）
        _do_exits(C, positions)       # 止盈 / DTE 强平（与实盘同一函数、同一份规则）
        positions = _positions(C)     # 平完重读：刚平的张数不应再占开仓额度
    else:
        _poll_orders(C)               # 实盘才需要回收在途委托（回测委托即时成交）
        positions = _positions(C)
        _do_exits(C, positions)

    if S.fail_streak >= _i(_g('MAX_ORDER_FAIL', MAX_ORDER_FAIL)):
        return

    # 本轮已开清零：同一张 bar 上后面的标的必须能看见前面刚开的仓（见 _try_entry 上限）
    S.opened_round = 0
    S.opened_round_undl = {}

    for undl in _undl_list():
        try:
            _try_entry(C, undl, positions, _cfg())
        except BaseException as e:
            _log('ERR', '%s 开仓判定异常 %s: %s' % (undl, type(e).__name__, str(e)[:120]),
                 force=True)

    if is_bt:
        _bt_summary(C)
    elif _i(_g('LOG_VERBOSE', LOG_VERBOSE)) == 1:
        _log('SUM', '持仓=%d 张 今日开仓=%d 待确认=%d' % (
            sum(p['qty'] for p in positions), S.entries_today, len(S.pending)), key='SUM')
