# -*- coding: utf-8 -*-
"""v5_2 自测：日终摘要 + 终局兜底 + 版本号（不依赖 QMT）

验证点：
  1) 每个交易日只在 15:00 打一行 [日终]（同一天多根 bar 不重复）
  2) [日终] 行前缀正确，且不附带「收尾/口径」两行（那两条留给终局）
  3) 终局（force=True）仍然是主行 + 收尾 + 口径三行
  4) 终局触发条件：is_last_bar() 或 (ymd>=BT_END 且 hm>=15:00)
  5) 常量：BT_END=20261119、BT_EOD_HM=1500；INIT 用 VERSION_LABEL 而非硬编码 'v1'
运行：python3 qmt/selftest/selftest_v5_2.py
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
import ashare_put_v5_2 as M  # noqa: E402

lines = []
errors = []


def _log(tag, msg, key=None, force=False):
    lines.append((tag, msg))


def check(name, cond, detail=''):
    if cond:
        print('[OK] %s' % name)
    else:
        errors.append(name)
        print('[FAIL] %s %s' % (name, detail))


def reset(ymd=20261113):
    lines[:] = []
    M._log = _log
    M._BT['on'] = True
    M._BT['ymd'] = ymd
    M._BT['hm'] = 930
    M._BT['eod_done'] = 0
    M.S.bt_sig = None
    M.S.bt = M._new_bt()
    M._account = lambda C=None: {'avail': 1.0e6, 'balance': 1.0e6, 'margin': 0.0}
    M._positions = lambda C=None: []
    M._bt_unreal = lambda C=None: (0.0, [])


def n_main():
    return len([1 for t, m in lines if t == 'BTSUM' and '日=' in m])


def n_eod():
    return len([1 for t, m in lines if t == 'BTSUM' and m.startswith('[日终]')])


def day_loop(ymd, hms):
    """复刻 handlebar 末尾的日终判定，逐 bar 跑一遍"""
    for hm in hms:
        M._BT['ymd'] = ymd
        M._BT['hm'] = hm
        M.S.bt['bar'] += 1
        eod = (M._BT['hm'] >= M.BT_EOD_HM and M._BT.get('eod_done') != M._BT['ymd'])
        if eod:
            M._BT['eod_done'] = M._BT['ymd']
        M._bt_summary(eod=eod)


# --- 场景 1：一个交易日 240 根 bar（09:30..15:00）→ 只 1 行 [日终] ---------
def hms_of_day():
    # A股 1m：09:30..11:29 + 13:00..14:59 + 15:00 = 241 根
    out = [(m // 60) * 100 + m % 60 for m in range(9 * 60 + 30, 11 * 60 + 30)]
    out += [(m // 60) * 100 + m % 60 for m in range(13 * 60, 15 * 60)]
    out.append(1500)
    return out


reset()
day_loop(20261113, hms_of_day())
check('单日 241 根 bar 只打 1 行 [日终]', n_eod() == 1, 'eod=%d main=%d' % (n_eod(), n_main()))
check('单日主行 = 首条 + 日终（无心跳/无重复）', n_main() == 2, 'main=%d' % n_main())
check('[日终] 行不附带收尾/口径', len(lines) == n_main(), 'all=%d main=%d' % (len(lines), n_main()))
check('[日终] 前缀在行首', lines[-1][1].startswith('[日终] '), lines[-1][1][:40])

# --- 场景 2：连续 5 个交易日 → 5 行日终 -----------------------------------
reset()
for d in (20261113, 20261116, 20261117, 20261118, 20261119):
    M._BT['eod_done'] = 0 if d != 20261113 else M._BT['eod_done']
    M._BT['ymd'] = d
    day_loop(d, [1458, 1459, 1500])
check('5 个交易日 = 5 行 [日终]', n_eod() == 5, 'got=%d' % n_eod())

# --- 场景 3：终局仍是三行 -------------------------------------------------
reset()
M.S.bt['bar'] = 1
M._bt_summary(force=True)
check('终局 = 主行+收尾+口径 3 行', len(lines) == 3, 'got=%d' % len(lines))
check('终局主行不带 [日终]', not lines[0][1].startswith('[日终]'), lines[0][1][:30])
check('终局含「收尾 |」行', any('收尾 |' in m for _, m in lines))
check('终局含「口径 |」行', any('口径 |' in m for _, m in lines))

# --- 场景 4：终局触发条件 ------------------------------------------------
def terminal(ymd, hm, last=False):
    return last or (str(ymd) >= M.BT_END and hm >= M.BT_EOD_HM)


check('最后一天 15:00 触发终局', terminal(20261119, 1500))
check('最后一天 14:59 不触发', not terminal(20261119, 1459))
check('最后一天 15:00 + is_last_bar 触发', terminal(20261119, 1500, last=True))
check('倒数第二天 15:00 不触发', not terminal(20261118, 1500))

# --- 场景 5：常量与版本号 -------------------------------------------------
check('BT_END 与本地 1m 数据一致 = 20261119', M.BT_END == '20261119', M.BT_END)
check('BT_EOD_HM = 1500', M.BT_EOD_HM == 1500, str(M.BT_EOD_HM))
check('BT_START = 20261113', M.BT_START == '20261113', M.BT_START)
src = _src('ashare_put_v5_2.py')
check("INIT 不再硬编码 'v1 start'", "'v1 start | build=%s" not in src)
check('INIT 用 VERSION_LABEL', '"%s start | build=%s' in src or "'%s start | build=%s" in src)
check('VERSION_LABEL = v5_2', M.VERSION_LABEL == 'v5_2', M.VERSION_LABEL)

print('')
if errors:
    print('FAILED: %s' % ', '.join(errors))
    sys.exit(1)
print('ALL PASSED')
