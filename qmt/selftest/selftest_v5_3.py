# -*- coding: utf-8 -*-
"""v5_3 自测：A 终局 bar 计数校正 + B 卖方收益率标签（不依赖 QMT）

并回归 v5_2 的日终/终局行为，确认这两处改动没碰坏它们。
运行：python3 qmt/selftest/selftest_v5_3.py
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
import ashare_put_v5_3 as M  # noqa: E402

ORIG_UNREAL = M._bt_unreal      # reset() 会把 _bt_unreal 打桩，B 段要用原实现

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


def reset(ymd=20261113, bar=1):
    lines[:] = []
    M._log = _log
    M._noise_flush = lambda tag='NOISE': None
    M._BT.update({'on': True, 'closed': 0, 'ymd': ymd, 'hm': 930, 'eod_done': 0})
    M.S.in_bt = 1
    M.S.bt_sig = None
    M.S.bt = M._new_bt()
    M.S.bt['bar'] = bar
    M._account = lambda C=None: {'avail': 1.0e6, 'balance': 1000797.90, 'margin': 0.0}
    M._positions = lambda C=None: []
    M._bt_unreal = lambda C=None: (0.0, [])


def n(tag='BTSUM'):
    return len([1 for t, _ in lines if t == tag])


# --- A：终局末根 bar 计数 ------------------------------------------------
reset(ymd=20261119, bar=1204)          # v5_2 在这条路径上打过 1204（应为 1205）
M._bt_terminal(None)
check('A1 终局只多算 1 根 bar', M.S.bt['bar'] == 1205, str(M.S.bt['bar']))
check('A2 终局主行 bar=1205', 'bar=1205' in lines[1][1], lines[1][1][:70])
check('A3 终局共 4 行（区间结束 + 收尾3）', n() == 4, 'got=%d' % n())
check('A4 终局置 _BT[closed]=1', M._BT['closed'] == 1)
check('A5 终局置 S.in_bt=0', M.S.in_bt == 0)

reset(bar=500)
M._bt_terminal(None, '走出区间测试', closed=False)
check('A6 走出区间分支不计 closed', M._BT['closed'] == 0)
check('A7 走出区间也补 1 根 bar', M.S.bt['bar'] == 501, str(M.S.bt['bar']))
check('A8 走出区间用自定义 msg', '走出区间测试' in lines[0][1])

# --- B：卖方收益率标签 --------------------------------------------------
src = _src('ashare_put_v5_3.py')
check('B1 POSDIAG 用「卖方%=」', '卖方%%=%s' in src)
check('B2 格式化串里不再有 drop= 标签', 'drop=%s' not in src)

reset()
M._bt_unreal = ORIG_UNREAL          # 恢复原实现（reset 打桩会把持仓吃掉）
M.S.bt['pos'] = {'10012598.SHO': {'open': 0.0122, 'unit': 10000, 'qty': 1, 'fee': 2.2}}
M._bt_last_px = lambda C, code: 0.0237
tot, det = M._bt_unreal(object())
check('B3 浮盈亏 = (0.0122-0.0237)*10000 - 2.2 = -117.2', abs(tot + 117.2) < 1e-6, str(tot))
check('B4 明细带符号且标明卖方', '卖方-94.3%' in det[0], det[0])

reset()
M._bt_unreal = ORIG_UNREAL
M.S.bt['pos'] = {'10012646.SHO': {'open': 0.0050, 'unit': 10000, 'qty': 1, 'fee': 2.2}}
M._bt_last_px = lambda C, code: 0.0034
tot, det = M._bt_unreal(object())
check('B5 盈利侧显示卖方+32.0%', '卖方+32.0%' in det[0], det[0])
check('B6 盈利侧浮盈亏 = +13.8', abs(tot - 13.8) < 1e-6, str(tot))
reset()
M._bt_unreal = ORIG_UNREAL
M.S.bt['pos'] = {'X': {'open': 0.01, 'unit': 10000, 'qty': 1, 'fee': 0.0}}
M._bt_last_px = lambda C, code: 0.0
tot, det = M._bt_unreal(object())
check('B7 无行情回落 n/a', '卖方n/a' in det[0], det[0])

# --- 回归：日终一行 / 终局三行（v5_2 行为不能被破坏） -------------------
reset()
M._bt_unreal = ORIG_UNREAL
M._bt_summary(eod=True)
check('R1 日终端仍只 1 行且带 [日终]', n() == 1 and lines[0][1].startswith('[日终]'), lines[0][1][:40])
reset()
M._bt_summary(force=True)
check('R2 终局三段式仍是 3 行', n() == 3, 'got=%d' % n())
check('R3 收尾行含「收尾 |」', any('收尾 |' in m for _, m in lines))
check('R4 口径行含「口径 |」', any('口径 |' in m for _, m in lines))

# --- 常量与版本 ---------------------------------------------------------
check('C1 BUILD 已更新', M.BUILD == '20261122-1545', M.BUILD)
check('C2 VERSION_LABEL = v5_3', M.VERSION_LABEL == 'v5_3', M.VERSION_LABEL)
check('C3 BT_END 仍为 20261119', M.BT_END == '20261119', M.BT_END)
check('C4 日终时刻仍为 1500', M.BT_EOD_HM == 1500, str(M.BT_EOD_HM))

print('')
if errors:
    print('FAILED: %s' % ', '.join(errors))
    sys.exit(1)
print('ALL PASSED')
