# -*- coding: utf-8 -*-
"""v5_1 自测：只验证「日志降噪」这一件事（不依赖 QMT）

验证点：
  1) 浮盈亏 / 已实现盈亏漂移不再触发 BTSUM（旧行为：每 bar 一行）
  2) 成交（开仓数 / 平仓数变化）必打
  3) 交易日变化必打
  4) 每 BT_SUM_EVERY_N_BARS(400) 根心跳打一行
  5) force=True 一定打
运行：python3 qmt/selftest/selftest_v5_1.py
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
import ashare_put_v5_1 as M  # noqa: E402

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
    M._BT['hm'] = 1000
    M.S.bt_sig = None
    M.S.bt = M._new_bt()
    # 屏蔽平台接口（真实环境读不到就当空）
    M._account = lambda C=None: {'avail': 1.0e6, 'balance': 1.0e6, 'margin': 0.0}
    M._positions = lambda C=None: []
    M._bt_unreal = lambda C=None: (0.0, [])


def n_sum():
    # 只数「主行」（以 日= 开头）；force 时另有收尾/口径两行，单独验
    return len([t for t, m in lines if t == 'BTSUM' and m.startswith('日=')])


# --- 场景 1：1205 根 bar，浮盈亏每根都漂移，无成交 -------------------------
reset()
for bar in range(1, 1206):
    M.S.bt['bar'] = bar
    M.S.bt['real'] = bar * 0.37          # 已实现也在漂（模拟脏数据）
    M._bt_unreal = lambda C=None, b=bar: (b * 0.11, [])
    M._bt_summary()
c1 = n_sum()
# 400/800/1200 三根心跳（bar=1 时 sig=None -> 首条必打，不在此场景内统计口径）
# 首条（sig None->值）+ bar 400/800/1200 三根心跳
check('无成交/无换日：仅首条+3 根心跳', c1 == 4, 'got=%d' % c1)
check('降噪后 < 旧行为 1%', c1 < 1205 * 0.01, 'got=%d old=1205' % c1)

# --- 场景 2：首条必打（账本初值/初始资金基线）-----------------------------
reset()
M.S.bt['bar'] = 1
M._bt_summary()
check('首条必打（sig 由 None 变化）', n_sum() == 1, 'got=%d' % n_sum())

# --- 场景 3：成交触发 -----------------------------------------------------
reset()
M.S.bt['bar'] = 10
M._bt_summary()                      # 首条
n0 = n_sum()
M.S.bt['opens'] = 1
M.S.bt['bar'] = 11
M._bt_summary()                      # 开仓 -> 必打
check('开仓数变化必打', n_sum() == n0 + 1, 'got=%d want=%d' % (n_sum(), n0 + 1))
n1 = n_sum()
M.S.bt['closed'] = 1
M.S.bt['bar'] = 12
M._bt_summary()                      # 平仓 -> 必打
check('平仓数变化必打', n_sum() == n1 + 1, 'got=%d want=%d' % (n_sum(), n1 + 1))
n2 = n_sum()
M.S.bt['bar'] = 13
M._bt_summary()                      # 什么都没变 -> 不打
check('无变化不打', n_sum() == n2, 'got=%d want=%d' % (n_sum(), n2))

# --- 场景 4：换日触发 -----------------------------------------------------
M._BT['ymd'] = 20261114
M.S.bt['bar'] = 14
M._bt_summary()
check('交易日变化必打', n_sum() == n2 + 1, 'got=%d want=%d' % (n_sum(), n2 + 1))

# --- 场景 5：心跳参数可配 + 非法值兜底 ------------------------------------
reset()
M._g = lambda name, default=None: 50 if name == 'BT_SUM_EVERY_N_BARS' else default
for bar in range(1, 121):
    M.S.bt['bar'] = bar
    M._bt_summary()
check('心跳=50 → 50/100 两根 + 首条', n_sum() == 3, 'got=%d' % n_sum())
reset()
M._g = lambda name, default=None: -5 if name == 'BT_SUM_EVERY_N_BARS' else default
for bar in range(1, 1206):
    M.S.bt['bar'] = bar
    M._bt_summary()
check('负数按 400 兜底', n_sum() == 4, 'got=%d' % n_sum())

# --- 场景 6：force --------------------------------------------------------
reset()
M.S.bt['bar'] = 7
M._bt_summary()
n3 = n_sum()
M.S.bt['bar'] = 8
M._bt_summary(force=True)
check('force 必打主行', n_sum() == n3 + 1, 'got=%d want=%d' % (n_sum(), n3 + 1))
check('force 附带收尾+口径两行', len([t for t, m in lines if t == 'BTSUM' and not m.startswith('日=')]) == 2,
      'got=%d' % len([t for t, m in lines if t == 'BTSUM' and not m.startswith('日=')]))

# --- 场景 7：行内容含虚拟日期与交易日（不再只有 bar 号）------------------
reset()
M.S.bt['bar'] = 1
M._bt_summary()
last = lines[-1][1]
check('行内含 日=20261113', '日=20261113' in last, last[:90])
check('行内含 开=/平=（事件字段仍在）', ('开=' in last and '平=' in last), last[:90])

print('')
if errors:
    print('FAILED: %s' % ', '.join(errors))
    sys.exit(1)
print('ALL PASSED')
