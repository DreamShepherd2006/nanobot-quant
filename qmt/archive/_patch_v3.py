# -*- coding: utf-8 -*-
"""v2 -> v3：只加 [POSDIAG] 回测持仓诊断 + 日志降噪，不改任何决策逻辑。

背景（2026-11-22 v2 build 20261122-0100 回测）：
  13 开 / 7 平 / 6 压在仓，日志里出现
    [SELL] ... [回测]未见持仓(未成交) ref=None
    [BT.HOLD] 1001xxxx open=0.0122 bid=0.0000
  怀疑三条静默失效链（尚缺证据）：
    ① _positions() 的 m_strInstrumentID 与策略 code 字面不等（后缀 .SHO 缺失）
       -> _bt_verify 恒 False -> 卖开被判失败
    ② 该合约回测态取不到 1m -> _bt_last_px=0 -> decide_exit 里 bid<=0 直接 hold
       -> 止盈永不触发
    ③ get_option_detail_data 取不到 ExpireDate -> dte=-1 -> DTE 强平失效
  本版只加只读诊断把这些缺口显式打出来，跑一轮再决定怎么改。

另：v2 的 [BT.HOLD] 在「有行情但未达止盈线」时 bid 每 bar 变化 -> key 去重失效
    -> 每 bar 每仓一行（960 bar x 6 仓 ≈ 5700 行）。v3 改为仅在「无行情」时提示一次。
"""
import io

SRC = '/data/instances/neo/workspace/qmt/ashare_put_v2.py'
OUT_UTF8 = '/data/instances/neo/workspace/qmt/ashare_put_v3.py'
OUT_GBK = '/data/instances/neo/workspace/qmt/deliver/ashare_put_v3.py'

s = io.open(SRC, encoding='utf-8').read()
n = 0


def rep(old, new, tag):
    global s, n
    assert old in s, '[FAIL] 未找到片段: %s' % tag
    assert s.count(old) == 1, '[FAIL] %s 出现 %d 次（应为 1）' % (tag, s.count(old))
    before = len(s)
    s = s.replace(old, new)
    n += 1
    print('[OK] %-26s %d -> %d 字符' % (tag, before, len(s)))


# ---------- R1: BUILD ----------
rep("BUILD = '20261122-0100'", "BUILD = '20261122-0030'", 'BUILD')

# ---------- R2: 新增状态字段 ----------
rep(
    "        self.bt_sig = None    # 回测汇总去重签名",
    "        self.bt_sig = None    # 回测汇总去重签名\n"
    "        self.pd_raw = {}      # [POSDIAG] 已打印过原始字段的持仓（id@exch，全程一次）",
    'S.pd_raw')

rep(
    "    S.bh_near = {}",
    "    S.bh_near = {}\n    S.pd_raw = {}",
    'init 重置 pd_raw')

# ---------- R3: 新增 _bt_posdiag（插在 _bt_summary 之前）----------
POSDIAG = '''def _bt_posdiag(C, positions):
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


'''
rep("def _bt_summary(C=None, force=False):", POSDIAG + "def _bt_summary(C=None, force=False):",
    '_bt_posdiag')

# ---------- R4: handlebar 调用诊断 ----------
rep(
    "        positions = _positions(C)     # 平台虚拟账号持仓\n"
    "        _do_exits(C, positions)       # 止盈 / DTE 强平（与实盘同一函数、同一份规则）",
    "        positions = _positions(C)     # 平台虚拟账号持仓\n"
    "        _bt_posdiag(C, positions)     # [POSDIAG] code/行情/DTE 缺口诊断（只读）\n"
    "        _do_exits(C, positions)       # 止盈 / DTE 强平（与实盘同一函数、同一份规则）",
    'handlebar 调 POSDIAG')

# ---------- R5: HOLD 降噪（仅无行情时提示一次）----------
rep(
    "        if act != 'close':\n"
    "            _log('HOLD', '%s open=%.4f bid=%.4f' % (code, p['open'], _f(q.get('bid'))),\n"
    "                 key='H' + code)\n"
    "            continue",
    "        if act != 'close':\n"
    "            if not q:      # 取不到该合约行情 -> 止盈无法判定（只提示一次，明细见 [POSDIAG]）\n"
    "                _log('HOLD', '%s 无行情 止盈无法判定 open=%.4f' % (code, p['open']),\n"
    "                     key='H' + code)\n"
    "            continue",
    'HOLD 降噪')

# ---------- 输出 ----------
# UTF-8 阅读版首行必须声明 utf-8，否则编辑器/解释器按 gbk 解码中文即报错
utf8 = s.replace('#coding:gbk\n# -*- coding: gbk -*-', '# -*- coding: utf-8 -*-', 1)
assert utf8.startswith('# -*- coding: utf-8 -*-'), 'UTF-8 版首行声明替换失败'
io.open(OUT_UTF8, 'w', encoding='utf-8', newline='\n').write(utf8)
body = s.replace('\r\n', '\n').replace('\n', '\r\n')
data = body.encode('gbk')          # 含 GBK 不可编码字符会在这里抛错
io.open(OUT_GBK, 'wb').write(data)

# 自检
chk = io.open(OUT_GBK, 'rb').read().decode('gbk')
assert 'POSDIAG' in chk
assert 'pd_raw' in chk
assert "BUILD = '20261122-0030'" in chk
assert chk.count('\r\n') == chk.count('\n'), 'CRLF 不一致'
assert chk.startswith('#coding:gbk'), 'GBK 版首行必须声明 gbk'
compile(utf8, OUT_UTF8, 'exec')
print('[OK] 改动 %d 处 | UTF-8 %d 字节 | GBK %d 字节 | CRLF %d 行'
      % (n, len(utf8.encode('utf-8')), len(data), chk.count('\r\n')))
print('[OK] GBK 自检通过（可编码 + 回读一致 + 语法编译通过）')
