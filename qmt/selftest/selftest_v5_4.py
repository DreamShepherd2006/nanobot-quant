# -*- coding: utf-8 -*-
"""v5_4 自测：C 同浪加仓门（+ v5_3 的 A/B 回归，不依赖 QMT）

运行：python3 qmt/selftest/selftest_v5_4.py
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
import ashare_put_v5_4 as M  # noqa: E402

ORIG_UNREAL = M._bt_unreal
SRC = _src('ashare_put_v5_4.py')

lines = []
errors = []


def _log(tag, msg, key=None, force=False):
    lines.append((tag, msg, key))


def check(name, cond, detail=''):
    if cond:
        print('[OK] %s' % name)
    else:
        errors.append(name)
        print('[FAIL] %s %s' % (name, detail))


def reset(bar=1):
    lines[:] = []
    M._log = _log
    M._noise_flush = lambda tag='NOISE': None
    M._BT.update({'on': True, 'closed': 0, 'ymd': 20261113, 'hm': 930, 'eod_done': 0})
    M.S.in_bt = 1
    M.S.bt = M._new_bt()
    M.S.bt['bar'] = bar
    M._account = lambda C=None: {'avail': 1.0e6, 'balance': 1000797.90, 'margin': 0.0}
    M._positions = lambda C=None: []
    M._bt_unreal = lambda C=None: (0.0, [])


# ============ C：_wave_blocked 语义 ============
wm1 = {'wave_max': 1}
check('C1 空仓不拦', M._wave_blocked(wm1, 0) == (False, ''))
b, why = M._wave_blocked(wm1, 1)
check('C2 已有 1 张 → 拦', b is True and '本浪已开 1 张' in why, why)
b, why = M._wave_blocked(wm1, 2)
check('C3 已有 2 张 → 拦且报真实张数', b is True and '本浪已开 2 张' in why, why)
check('C4 wave_max=0 关闭该门', M._wave_blocked({'wave_max': 0}, 5) == (False, ''))
check('C5 wave_max=2 时 1 张放行', M._wave_blocked({'wave_max': 2}, 1) == (False, ''))
check('C6 wave_max=2 时 2 张拦', M._wave_blocked({'wave_max': 2}, 2)[0] is True)
check('C7 cfg 缺 key 视为关闭', M._wave_blocked({}, 3) == (False, ''))
check('C8 cfg=None 不崩且放行', M._wave_blocked(None, 3) == (False, ''))
check('C9 per_undl 传字符串也能判', M._wave_blocked(wm1, '1')[0] is True)

# ============ 接线：门在 max_undl/max_total 之前 ============
i_wave = SRC.find('_wave_blocked(cfg, per_undl)')
i_cap = SRC.find("if per_undl >= cfg['max_undl'] or total >= cfg['max_total']:")
check('W1 门已接线', i_wave > 0 and i_cap > 0, 'i_wave=%d i_cap=%d' % (i_wave, i_cap))
check('W2 先判浪门再判张数上限', 0 < i_wave < i_cap, 'i_wave=%d i_cap=%d' % (i_wave, i_cap))
check('W3 浪门拦截日志独立', "'本浪已开 %d 张" in SRC or '本浪已开 %d 张（每浪/标的=%d' in SRC)
check('W4 有降噪 key', "key='W' + undl" in SRC)
check('W5 INIT 打生效值', '每浪/标的=' in SRC)
check('W6 默认常量 = 1', M.MAX_PER_WAVE_PER_UNDL == 1, str(M.MAX_PER_WAVE_PER_UNDL))

cfg = M._cfg()
check('W7 _cfg() 含 wave_max=1', cfg.get('wave_max') == 1, str(cfg.get('wave_max')))
check('W8 _cfg() 其余闸门未变',
      (cfg.get('max_undl'), cfg.get('max_total'), cfg.get('max_day')) == (3, 6, 0),
      str((cfg.get('max_undl'), cfg.get('max_total'), cfg.get('max_day'))))

# ============ 11-13 实测信号序列重放（只算闸门，不碰下单） ============
# 日志里 11-13 的低9 首穿顺序（成交时间近似）：
seq = ['510500', '588000', '588000', '510300', '510500', '510300',
       '510050', '510500', '510300', '510500', '588000', '510050',
       '510500', '588000', '510300', '510050', '588000', '510500', '510050']


def replay(wave_max):
    """用 v5_4 的三道闸门重放：浪门 → max_undl/max_total。返回 (开仓列表, 拦截统计)"""
    pos = {}
    opened, blocked = [], {}
    for undl in seq:
        per = pos.get(undl, 0)
        total = sum(pos.values())
        wb, _ = M._wave_blocked({'wave_max': wave_max}, per)
        if wb:
            blocked['wave'] = blocked.get('wave', 0) + 1
            continue
        if per >= 3 or total >= 6:
            blocked['cap'] = blocked.get('cap', 0) + 1
            continue
        pos[undl] = per + 1
        opened.append(undl)
    return opened, blocked


op_v4, bl_v4 = replay(1)
check('S1 一浪一张：11-13 每标的至多 1 张',
      all(op_v4.count(u) <= 1 for u in set(op_v4)), str(op_v4))
check('S2 一浪一张：首日只开 4 张（4 个标的各 1）', len(op_v4) == 4, str(op_v4))
check('S3 510050 终于有机会（旧版被 total=6/6 挡死）', '510050' in op_v4, str(op_v4))
check('S4 浪门确实拦下重复信号', bl_v4.get('wave', 0) >= 10, str(bl_v4))
op_off, bl_off = replay(0)
check('S5 关浪门回到旧行为（首日 6 张、510050 进不去）',
      len(op_off) == 6 and '510050' not in op_off, str((len(op_off), op_off)))
check('S6 关浪门时拦截全来自张数上限', bl_off.get('wave', 0) == 0, str(bl_off))

# ============ A/B 回归（v5_3 已验的不能被改坏） ============
reset(bar=1204)
M._bt_terminal(None)
check('R1 终局 bar=1205', M.S.bt['bar'] == 1205 and 'bar=1205' in lines[1][1], lines[1][1][:60])
reset()
M._bt_unreal = ORIG_UNREAL
M.S.bt['pos'] = {'10012598.SHO': {'open': 0.0122, 'unit': 10000, 'qty': 1, 'fee': 2.2}}
M._bt_last_px = lambda C, code: 0.0237
tot, det = M._bt_unreal(object())
check('R2 卖方% 标签与数值', '卖方-94.3%' in det[0] and abs(tot + 117.2) < 1e-6, det[0])
reset()
M._bt_unreal = ORIG_UNREAL
M._bt_summary(eod=True)
check('R3 日终仍 1 行', len([1 for t, _, _ in lines if t == 'BTSUM']) == 1)
check('R4 BUILD / VERSION', M.BUILD == '20261122-1610' and M.VERSION_LABEL == 'v5_4')

print('')
if errors:
    print('FAILED: %s' % ', '.join(errors))
    sys.exit(1)
print('ALL PASSED')
print('')
print('--- 11-13 闸门重放（一浪一张，max_undl=3 / max_total=6）---')
print('开仓 %d 张: %s' % (len(op_v4), ' '.join(op_v4)))
print('拦截 %s' % bl_v4)
print('--- 同序列关浪门（旧行为）---')
print('开仓 %d 张: %s' % (len(op_off), ' '.join(op_off)))
print('拦截 %s' % bl_off)
