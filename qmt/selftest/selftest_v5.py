# -*- coding: utf-8 -*-
"""v5 本地自测（不依赖 QMT：取数/下单/账户接口全 stub）

覆盖 v5 四条改动 + v4 保留项：
  1) 同 bar 多标的：上限必须算入本轮已开（v4 已修，防回归）
  2) 每日开仓上限 MAX_ENTRIES_PER_DAY：0 = 不限制
  3) 资金门 fail-closed：可用资金读成 0 时不再静默放行（v5）
  4) 盈亏单一口径：价差并入逐笔成本，净 = 已实现 + 浮盈亏（v5）
  5) DTE 强平：无行情时按内在价值 max(0, strike - 现价) 记账（v5）
运行：python3 qmt/selftest/selftest_v5.py
"""
import sys
import io

import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # qmt/
for _p in (ROOT, os.path.join(ROOT, 'archive')):
    sys.path.insert(0, _p)


def _src(name):
    """取源码文本：当前版本在 qmt/、历史版本在 qmt/archive/"""
    for _base in (ROOT, os.path.join(ROOT, 'archive')):
        _p = os.path.join(_base, name)
        if os.path.exists(_p):
            return io.open(_p, encoding='utf-8').read()
    raise SystemExit('source not found: %s' % name)
import ashare_put_v5 as M  # noqa: E402

out = []
errors = []
closed_calls = []


def _log(tag, msg, key=None, force=False):
    out.append('%s %s' % (tag, msg))


def check(name, cond, detail=''):
    if cond:
        print('[OK] %s' % name)
    else:
        errors.append(name)
        print('[FAIL] %s %s' % (name, detail))


def reset(md=0, total=2):
    out[:] = []
    closed_calls[:] = []
    M._log = _log
    M.MAX_ENTRIES_PER_DAY = md
    M.MAX_TOTAL = total
    M.MAX_PER_UNDL = 5
    M._BT['on'] = True
    M._BT['ymd'] = 20261113
    M._BT['hm'] = 1000
    M._BT['started'] = 1
    M.S.cycle = {}
    M.S.done_bar = {}
    M.S.skip_note = {}
    M.S.sig_prev = {}
    M.S.pending = {}
    M.S.dry_skip = {}
    M.S.closed_ts = {}
    M.S.entries_today = 0
    M.S.opened_round = 0
    M.S.opened_round_undl = {}
    M.S.fail_streak = 0
    M.S.bt = M._new_bt()


def stub_all():
    M._bars = lambda C, undl, period, n: [{'t': 100 + i, 'c': 10.0 + i * 0.01}
                                          for i in range(20)]
    M.setup_counts = lambda closes, cmp_len=4: [0] * (len(closes) - 1) + [9]
    M.is_first_cross = lambda counts, th: True
    M._min_dist = lambda undl: 0.0
    M._build_cands = lambda C, undl, cfg: [{'code': 'C-' + undl, 'strike': 1.0,
                                            'margin': 100.0, 'bid': 0.01,
                                            'net': 1.0, 'yield': 1.0}]
    M._spot = lambda C, undl, bars: 1.0
    M._bt_quotes = lambda C, codes: dict((c, {'last': 0.01, 'bid': 0.01, 'ask': 0.01,
                                              'mid': 0.01}) for c in codes)
    M.pick_put = lambda cands, spot, md, qs, fee: cands[0]
    M._account = lambda C: {'avail': 1000000.0, 'balance': 1000000.0, 'margin': 0.0}
    M._can_trade = lambda: True
    M.sell_open = lambda C, code, qty, px, remark: 'bt-ok'
    M._undl_of = lambda C, code: ''


def cfg(md=0, total=2, budget=100.0):
    return {'max_undl': 5, 'max_total': total, 'max_day': md,
            'min_dte': 0, 'fee_sell': 1.2, 'budget_pct': budget,
            'force_dte': 1, 'force_n': 3, 'tp_pct': 50.0}


def run_entry(undls, c):
    """同一根 bar 依次评估多个标的（positions 快照保持空 = 平台还没回持仓）"""
    positions = []
    for u in undls:
        M._try_entry(None, u, positions, c)


def main():
    stub_all()

    # 1) 同 bar 三个标的同时买9，上限 2 张 → 只开 2 笔
    reset(total=2)
    run_entry(['510050.SH', '510300.SH', '510500.SH'], cfg(total=2))
    check('同 bar 上限：只开 2 笔', M.S.entries_today == 2,
          'entries=%d' % M.S.entries_today)
    check('同 bar 上限：第 3 笔被拦', M.S.opened_round == 2,
          'opened_round=%d' % M.S.opened_round)

    # 2) 上限 6 张、6 个标的同时买9 → 只开 6 笔
    reset(total=6)
    run_entry(['510050.SH', '510300.SH', '510500.SH', '588000.SH',
               '588080.SH', '159919.SZ'], cfg(total=6))
    check('六标的同 bar：开仓不超过上限', M.S.opened_round == 6,
          'opened=%d entries=%d' % (M.S.opened_round, M.S.entries_today))

    # 3) 每日上限 0 = 不限制
    reset(md=0)
    M.S.entries_today = 99
    run_entry(['510050.SH'], cfg(md=0))
    check('日限=0 不拦（entries 99 仍开）', M.S.entries_today == 100,
          'entries=%d' % M.S.entries_today)

    # 4) 每日上限 >0 生效
    reset(md=1)
    M.S.entries_today = 1
    run_entry(['510050.SH'], cfg(md=1))
    check('日限=1 已达时拦截', M.S.entries_today == 1,
          'entries=%d' % M.S.entries_today)

    # 5) v5 资金门 fail-closed：可用资金读成 0 → 拒单（旧实现会放行）
    reset(total=5)
    M._account = lambda C: {'avail': 0.0, 'balance': 1000000.0, 'margin': 0.0}
    run_entry(['510050.SH'], cfg(total=5))
    check('资金门 avail=0 拒单', M.S.entries_today == 0,
          'entries=%d' % M.S.entries_today)
    check('资金门拒单日志含 fail-closed',
          any('fail-closed' in l for l in out), str(out[-2:]))

    # 6) v5 资金门：可用资金充足 → 放行
    reset(total=5)
    M._account = lambda C: {'avail': 1000000.0, 'balance': 1000000.0, 'margin': 0.0}
    run_entry(['510050.SH'], cfg(total=5))
    check('资金门充足放行', M.S.entries_today == 1,
          'entries=%d' % M.S.entries_today)

    # 7) v5 账本：已实现含双边价差
    reset()
    M._bt_note_open('1001.SHO', 0.1220, 1, 1.2)
    M._bt_note_close('1001.SHO', 0.1000, 1, 1.7)
    spr_open = M._spread_cost(0.1220, 1)
    spr_close = M._spread_cost(0.1000, 1)
    want = (0.1220 - 0.1000) * 10000 * 1 - 1.2 - 1.7 - spr_open - spr_close
    check('已实现盈亏含双边价差', abs(M.S.bt['real'] - want) < 1e-6,
          'real=%.4f want=%.4f' % (M.S.bt['real'], want))
    check('平仓后移出未平仓账本', '1001.SHO' not in M.S.bt['pos'],
          str(M.S.bt['pos'].keys()))

    # 8) 浮盈亏含开仓端价差
    reset()
    M._bt_note_open('1002.SHO', 0.0500, 2, 2.4)
    M._bt_last_px = lambda C, code: 0.0450 if code == '1002.SHO' else 0.0
    unreal, det = M._bt_unreal(object())
    want_u = (0.0500 - 0.0450) * 10000 * 2 - 2.4 - M._spread_cost(0.0500, 2)
    check('浮盈亏含开仓价差', abs(unreal - want_u) < 1e-6,
          'unreal=%.4f want=%.4f' % (unreal, want_u))

    # 9) BTSUM：净 = 已实现 + 浮盈亏（不再事后减一次价差）
    reset()
    M._bt_note_open('1001.SHO', 0.1220, 1, 1.2)
    M._bt_note_close('1001.SHO', 0.1000, 1, 1.7)
    M.S.bt['spr'] = 2.0
    M.S.bt['bar'] = 7
    M._bt_last_px = lambda C, code: 0.0
    M._bt_summary(None, force=True)
    line = [l for l in out if '收尾' in l]
    check('BTSUM 收尾输出存在', bool(line), str(out[-2:]))
    if line:
        net = M.S.bt['real'] + 0.0
        check('BTSUM 净额 = 已实现 + 浮盈亏', ('净=%+.2f' % net) in line[0],
              line[0][:220])
    check('BTSUM 标注余额不可当盈亏',
          any('不作盈亏依据' in l for l in out), str(out[-2:]))

    # 10) _settle_px：回测按内在价值，非回测返回 0
    class _C(object):
        def get_option_detail_data(self, code):
            return {'ExpireDate': '20261125', 'OptExercisePrice': 5.0}

    reset()
    M._undl_of = lambda C, code: '510050.SH'
    M._bars = lambda C, undl, period, n: [{'t': 1, 'c': 4.6}]
    check('_settle_px 内在价值 = strike - 现价',
          abs(M._settle_px(_C(), '1001.SH') - 0.4) < 1e-9,
          'got=%r' % M._settle_px(_C(), '1001.SH'))
    M._bars = lambda C, undl, period, n: [{'t': 1, 'c': 5.6}]
    check('_settle_px 虚值时归零', M._settle_px(_C(), '1001.SH') == 0.0,
          'got=%r' % M._settle_px(_C(), '1001.SH'))
    M._BT['on'] = False
    check('_settle_px 实盘返回 0（实物行权，不代记账）',
          M._settle_px(_C(), '1001.SH') == 0.0)
    M._BT['on'] = True

    # 11) DTE 强平整合：合约无行情 → 按内在价值买平，reason 标 _settle
    reset()
    M._undl_of = lambda C, code: '510050.SH'
    M._bars = lambda C, undl, period, n: [{'t': 1, 'c': 4.6}]
    M._detail_dte = lambda C, code: 2
    M._bt_quotes = lambda C, codes: dict((c, {'last': 0.0, 'bid': 0.0, 'ask': 0.0,
                                              'mid': 0.0}) for c in codes)
    M.decide_exit = lambda p, q, c: ('hold', '', 0.0)
    M.buy_close = lambda C, code, qty, px, remark: (
        closed_calls.append((code, qty, px, remark)) or 'ok')

    class _C2(object):
        def get_option_detail_data(self, code):
            return {'ExpireDate': '20261125', 'OptExercisePrice': 5.0}

    M._do_exits(_C2(), [{'code': '1001.SH', 'open': 0.05, 'qty': 1, 'avail': 1}])
    check('DTE 强平：无行情时仍下单', len(closed_calls) == 1, str(closed_calls))
    if closed_calls:
        code, qty, px, remark = closed_calls[0]
        check('DTE 强平：按内在价值成交', abs(px - 0.4) < 1e-9, 'px=%r' % px)
        check('DTE 强平：reason 标记结算', 'dte2_settle' in remark, remark)

    print('')
    if errors:
        print('FAILED: %s' % errors)
        return 1
    print('ALL PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
