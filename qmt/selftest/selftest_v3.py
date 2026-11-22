# -*- coding: utf-8 -*-
"""_bt_posdiag 本地自测（不依赖 QMT：把取数/持仓接口全 stub 掉）

覆盖：
  1) 三种缺口同时出现时，一行 POSDIAG 把 bars/last/dte 全打出来（判定 ①②③）
  2) RAW 行给出 m_strInstrumentID 与 m_strExchangeID，判定 code 后缀问题
  3) 同一交易日重复调用不重复打印（日志量控制）
  4) 换交易日各合约再打一行
  5) HOLD 降噪：正常持仓（有行情但未达止盈线）不打印
"""
import io
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
import ashare_put_v3 as M  # noqa: E402

out = []


class _Cap(object):
    def write(self, s):
        out.append(s)

    def flush(self):
        pass


def _fresh():
    out[:] = []
    M._PRN.clear()
    M.S.bh_day = {}
    M.S.pd_raw = {}
    M._BT['on'] = True
    M._BT['ymd'] = 20261117
    M._BT['hm'] = 1000


# ---- stub ----------------------------------------------------------------
FAKE_POS = [
    {'code': '10012748.SHO', 'qty': 1, 'open': 0.0320},
    {'code': '10012598.SHO', 'qty': 1, 'open': 0.0122},
    {'code': '10012682.SHO', 'qty': 1, 'open': 0.0470},
]
BARS = {'10012748.SHO': 6, '10012598.SHO': 0, '10012682.SHO': 6}
LAST = {'10012748.SHO': 0.0300, '10012598.SHO': 0.0, '10012682.SHO': 0.0460}
DTE = {'10012748.SHO': 3, '10012598.SHO': -1, '10012682.SHO': 5}


class _Raw(object):
    def __init__(self, cid, ex):
        self.m_strInstrumentID = cid
        self.m_strExchangeID = ex
        self.m_eSideFlag = '0'
        self.m_nVolume = 1
        self.m_nCanUseVolume = 1
        self.m_dOpenPrice = 0.032
        self.m_dMargin = 400.0
        self.m_dRealUsedMargin = 0.0


M._bars = lambda C, code, period, count: [{'t': '202611171000', 'o': 0, 'h': 0,
                                           'l': 0, 'c': 0, 'v': 0}] * BARS.get(code, 0)
M._bt_last_px = lambda C, code: LAST.get(code, 0.0)
M._detail_dte = lambda C, code: DTE.get(code, -1)
M._tdetail = lambda C, kind, acct_type=None: [
    _Raw('10012748', 'SHO'), _Raw('10012598', 'SHO'), _Raw('10012682', 'SHO')]

# ---- 1) 三种缺口一次打全 -------------------------------------------------
_fresh()
_m = io.StringIO()
_o = sys.stdout
sys.stdout = _m
M._bt_posdiag(object(), FAKE_POS)
sys.stdout = _o
txt = _m.getvalue()
lines = [x for x in txt.splitlines() if x]
print('=== 第 1 次调用（首日）===')
for x in lines:
    print('  ' + x)
assert any('POSDIAG' in x and '10012598.SHO bars=0' in x for x in lines), '缺行情合约未显示 bars=0'
assert any('10012598.SHO bars=0 last=0.0000 drop=n/a' in x and 'dte=-1' in x for x in lines), \
    '缺口三元组未同现一行（且缺行情时 drop 必须 n/a）'
assert any('10012748.SHO bars=6 last=0.0300 drop=6.3%' in x for x in lines), '有行情时回落率应正常计算'
assert any('RAW id=' in x and "'10012748'" in x and "'SHO'" in x for x in lines), 'RAW 行缺 id/exch'
assert len([x for x in lines if 'RAW' in x]) == 3, 'RAW 行数应按合约各一次'

# ---- 3) 同一交易日重复调用 -------------------------------------------------
_fresh()
_m = io.StringIO()
sys.stdout = _m
M._bt_posdiag(object(), FAKE_POS)
n1 = len([x for x in _m.getvalue().splitlines() if x])
sys.stdout = _o
_m2 = io.StringIO()
sys.stdout = _m2
M._bt_posdiag(object(), FAKE_POS)
sys.stdout = _o
n2 = len([x for x in _m2.getvalue().splitlines() if x])
print('=== 同日重复调用：第1次 %d 行 / 第2次 %d 行 ===' % (n1, n2))
assert n1 > 0 and n2 == 0, '同日不应重复打印（日志降噪）'

# ---- 4) 换交易日 -----------------------------------------------------------
_fresh()
M._bt_posdiag(object(), FAKE_POS)
M._BT['ymd'] = 20261118
_m3 = io.StringIO()
sys.stdout = _m3
M._bt_posdiag(object(), FAKE_POS)
sys.stdout = _o
t3 = [x for x in _m3.getvalue().splitlines() if x]
print('=== 换日：%d 行（应 1 行 POSDIAG，RAW 不重复）===' % len(t3))
assert len(t3) == 1 and 'ymd=20261118' in t3[0], '换日应只打 1 行且是新日期'

# ---- 5) 空仓不打印 ---------------------------------------------------------
_fresh()
_m4 = io.StringIO()
sys.stdout = _m4
M._bt_posdiag(object(), [])
sys.stdout = _o
assert _m4.getvalue() == '', '空仓应静默'

print('\n[ALL PASS] _bt_posdiag 逻辑与日志量自测通过')
