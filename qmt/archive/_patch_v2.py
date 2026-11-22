# -*- coding: utf-8 -*-
"""把 ashare_put_v1.py 的回测路径从「内存模拟撮合」改成「QMT 平台记账」。

依据 2026-11-21/22 六个探针实测结论（build 20261121-2355 ~ 20261122-0040）：
  passorder 在回测态可真实下单；虚拟账号按股票式记账；乘数 10000；盈亏精确；
  T+0 放行；限价须落在 bar[low,high] 内；手续费按手 set_commission 生效；
  保证金严重低估；无到期结算；滑点参数无效；无历史盘口。
"""
import io
import os

SRC = '/data/instances/neo/workspace/qmt/ashare_put_v1.py'
OUT_UTF8 = '/data/instances/neo/workspace/qmt/ashare_put_v2.py'
OUT_GBK = '/data/instances/neo/workspace/qmt/deliver/ashare_put_v2.py'

s = io.open(SRC, encoding='utf-8').read()
n_changes = 0


def rep(old, new, tag):
    global s, n_changes
    assert old in s, '[FAIL] 未找到片段: %s' % tag
    cnt = s.count(old)
    assert cnt == 1, '[FAIL] %s 出现 %d 次（应为 1）' % (tag, cnt)
    before = len(s)
    s = s.replace(old, new)
    n_changes += 1
    print('[OK] %-28s %d -> %d 字符' % (tag, before, len(s)))


# ---------- R1: BUILD ----------
rep("BUILD = '20261121-2205'", "BUILD = '20261122-0100'", 'BUILD')

# ---------- R2: BT_HALF_SPREAD_PCT 注释 ----------
rep(
    "BT_HALF_SPREAD_PCT = 1.0       # 回测模拟半价差(%)：卖开吃 bid=close×(1-x)，买平付 ask=close×(1+x)",
    "BT_HALF_SPREAD_PCT = 1.0       # 已废弃：2026-11-22 起回测走平台记账、撮合价=bar close；仅留兼容",
    'BT_HALF_SPREAD_PCT',
)

# ---------- R3: _can_trade 回测放行 ----------
rep(
    '''def _can_trade():
    """三重防呆：模型研究无账号 / 模拟信号模式不成交 / LIVE_CONFIRM 代码锁"""
    return _i(_g('LIVE_CONFIRM', LIVE_CONFIRM)) == 1''',
    '''def _can_trade():
    """三重防呆：模型研究无账号 / 模拟信号模式不成交 / LIVE_CONFIRM 代码锁
    回测态放行：走的是平台虚拟账号 + 虚拟资金，不需要实盘锁。"""
    if _BT['on']:
        return True
    return _i(_g('LIVE_CONFIRM', LIVE_CONFIRM)) == 1''',
    '_can_trade',
)

# ---------- R4: _positions 兼容回测态字段 ----------
rep(
    '''def _positions(C):
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
    return out''',
    '''def _positions(C):
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
            'code': str(_gv(p, 'm_strInstrumentID', '')),
            'qty': _i(_gv(p, 'm_nVolume', 0)),
            'avail': _i(_gv(p, 'm_nCanUseVolume', 0)),
            'open': _f(_gv(p, 'm_dOpenPrice', 0)),
            'margin': m,
        })
    return out''',
    '_positions',
)

# ---------- R5: 5.5 节整体替换 ----------
i1 = s.index('# ====================== 5.5 回测模拟撮合')
i2 = s.index('# ====================== 6. 编排')
old_sec = s[i1:i2]
assert len(old_sec) > 5000, '5.5 节长度异常: %d' % len(old_sec)

new_sec = '''# ====================== 5.5 回测（平台记账，仅 do_back_test）======================
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


def _bt_quotes(C, codes):
    """回测报价：无历史盘口，last = bid = ask = 该 bar 收盘价。
    价差（真实卖吃 bid / 买付 ask，实测近月 5-10% 虚值约 2.01%）无法在撮合层建模，
    由报告层按成交额×1% 单独修正。
    """
    out = {}
    for code in codes:
        px = _bt_last_px(C, code)
        if px <= 0:
            continue
        out[code] = {'last': px, 'bid': px, 'ask': px}
    return out


def _bt_summary(C=None, force=False):
    """回测总账：直接读平台虚拟账号（余额/可用/在仓），不再自建内存账本"""
    try:
        acc = _account(C) if C is not None else {'avail': 0.0, 'balance': 0.0, 'margin': 0.0}
        pos = _positions(C) if C is not None else []
    except BaseException:
        acc = {'avail': 0.0, 'balance': 0.0, 'margin': 0.0}
        pos = []
    sig = (round(acc['balance'], 2), len(pos))
    if not force and sig == S.bt_sig and S.bt['bar'] % 400 != 0:
        return
    S.bt_sig = sig
    held = ' '.join('%s×%d' % (p['code'], p['qty']) for p in pos[:8]) or '无'
    _log('BTSUM', 'bar=%d 开=%d 平=%d | 余额=%.2f 可用=%.2f | 在仓=%d张 %s'
         % (S.bt['bar'], S.bt['opens'], S.bt['closed'], acc['balance'], acc['avail'],
            len(pos), held), force=True)


'''
s = s[:i1] + new_sec + s[i2:]
n_changes += 1
print('[OK] %-28s %d -> %d 字符' % ('5.5 节重写', i1 + len(old_sec) + len(s[i2:]), len(s)))

# ---------- R6: init 里的回测提示 + set_commission ----------
rep(
    '''    if _BT['on']:
        _log('INIT', '回测模拟撮合开启：期权 bar 收盘价 ± %.2f%% 半价差模拟盘口，内存记账'
             '（不读持仓/不发单，总账看 [BTSUM]）' % (
                 _f(_g('BT_HALF_SPREAD_PCT', BT_HALF_SPREAD_PCT))), force=True)''',
    '''    if _BT['on']:
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
             '保证金按平台比例（严重低估）；价差需报告层修正', force=True)''',
    'init-回测提示',
)

# ---------- R7: 回测区间提示文案 ----------
rep(
    """        _log('INIT', '回测区间（代码写死，面板起止对本策略无效）%s ~ %s'
             % (BT_START or '不限', BT_END or '不限'), force=True)""",
    """        _log('INIT', '回测区间闸（代码内双保险）%s ~ %s：区间外静默跳过'
             % (BT_START or '不限', BT_END or '不限'), force=True)""",
    'init-区间文案',
)

# ---------- R8: handlebar 出场/持仓段 ----------
rep(
    '''    _poll_orders(C)
    if is_bt:
        _bt_exits(C, _cfg())          # 内存持仓：到期结算 / 强平 / 止盈
        positions = _bt_positions()
    else:
        positions = _positions(C)
        _do_exits(C, positions)''',
    '''    if is_bt:
        positions = _positions(C)     # 平台虚拟账号持仓
        _do_exits(C, positions)       # 止盈 / DTE 强平（与实盘同一函数、同一份规则）
    else:
        _poll_orders(C)               # 实盘才需要回收在途委托（回测委托即时成交）
        positions = _positions(C)
        _do_exits(C, positions)''',
    'handlebar-出场段',
)

# ---------- R9: handlebar 末尾 summary 调用 ----------
rep(
    '''    if is_bt:
        _bt_summary()
    elif _i(_g('LOG_VERBOSE', LOG_VERBOSE)) == 1:''',
    '''    if is_bt:
        _bt_summary(C)
    elif _i(_g('LOG_VERBOSE', LOG_VERBOSE)) == 1:''',
    'handlebar-summary',
)

# ---------- R10: 区间结束时 summary 调用（两处） ----------
s_before = s
s = s.replace('_bt_summary(force=True)', '_bt_summary(C, force=True)')
assert s != s_before, '[FAIL] 区间结束 summary 未替换'
print('[OK] %-28s 替换 %d 处' % ('区间结束 summary', s_before.count('_bt_summary(force=True)')))
n_changes += 1

# ---------- R11: _try_entry 删除回测内存建仓 ----------
rep(
    '''    if _BT['on']:
        # 回测：内存模拟建仓，不发单、不建在途委托
        _bt_open(pick, undl, cfg)
        S.entries_today += 1
        cy['bought'] = True
        cy['reset'] = False
        _log('ENTRY', '[模拟] %s -> %s strike=%.4f bid=%.4f 权利金=%.1f margin=%.0f yield=%.2f%%' % (
            undl, pick['code'], pick['strike'], pick['bid'],
            pick['bid'] * pick['unit'] - cfg['fee_sell'], pick['margin'], pick['yield']), force=True)
        return

''',
    '',
    '_try_entry-删内存建仓',
)

# ---------- R12: _try_entry 下单成功分支：回测不建在途委托 ----------
rep(
    '''    else:
        S.entries_today += 1
        cy['bought'] = True
        cy['reset'] = False
        S.pending[pick['code']] = {'kind': 'open', 'ref': ref, 'ts': _ts(),
                                   'px': pick['bid'], 'qty': 1, 'undl': undl,
                                   'remark': remark}''',
    '''    else:
        S.entries_today += 1
        cy['bought'] = True
        cy['reset'] = False
        if _BT['on']:
            S.bt['opens'] += 1        # 回测：委托即时成交，不建在途委托
        else:
            S.pending[pick['code']] = {'kind': 'open', 'ref': ref, 'ts': _ts(),
                                       'px': pick['bid'], 'qty': 1, 'undl': undl,
                                       'remark': remark}''',
    '_try_entry-成交分支',
)

# ---------- R13: _do_exits 报价来源 ----------
rep(
    """    qs = _ticks(C, [p['code'] for p in positions])""",
    """    if _BT['on']:
        qs = _bt_quotes(C, [p['code'] for p in positions])   # 回测无盘口：last=bid=ask=close
    else:
        qs = _ticks(C, [p['code'] for p in positions])""",
    '_do_exits-报价',
)

# ---------- R14: _do_exits 平仓成功分支：回测不建在途委托 ----------
rep(
    '''        S.pending[code] = {'kind': 'close', 'ref': ref, 'ts': _ts(),
                           'px': px, 'qty': qty, 'remark': reason}''',
    '''        if _BT['on']:
            S.bt['closed'] += 1       # 回测：买平即时成交
            S.closed_ts[code] = _ts()
        else:
            S.pending[code] = {'kind': 'close', 'ref': ref, 'ts': _ts(),
                               'px': px, 'qty': qty, 'remark': reason}''',
    '_do_exits-成交分支',
)

# ---------- 校验：不应残留被删函数的引用 ----------
for gone in ('_bt_open(', '_bt_close(', '_bt_settle(', '_bt_finish(',
             '_bt_positions(', '_bt_exits('):
    assert gone not in s, '[FAIL] 仍残留已删除函数的调用: %s' % gone
print('[OK] 无残留调用（_bt_open/_bt_close/_bt_settle/_bt_finish/_bt_positions/_bt_exits）')

# ---------- 写 UTF-8 源 ----------
io.open(OUT_UTF8, 'w', encoding='utf-8', newline='\n').write(s)
print('[OK] UTF-8 源 -> %s (%d 行)' % (OUT_UTF8, s.count('\n') + 1))

# ---------- 写 GBK+CRLF 交付件 ----------
gbk = s.replace('\r\n', '\n').replace('\n', '\r\n')
try:
    data = gbk.encode('gbk')
except UnicodeEncodeError as e:
    print('[WARN] GBK 编码失败: %r' % (e,))
    raise
os.makedirs(os.path.dirname(OUT_GBK), exist_ok=True)
io.open(OUT_GBK, 'wb').write(data)
print('[OK] GBK+CRLF 交付件 -> %s (%d 字节)' % (OUT_GBK, len(data)))

# ---------- 语法自检（用 UTF-8 源） ----------
import py_compile
try:
    py_compile.compile(OUT_UTF8, doraise=True)
    print('[OK] py_compile 通过')
except Exception as e:
    print('[FAIL] 语法错误: %r' % (e,))
    raise

print('\n共 %d 处改动' % n_changes)
