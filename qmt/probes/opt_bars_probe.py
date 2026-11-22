#coding:gbk
# =====================================================================
# QMT 期权 1m 取数探针（只读，不下单，不写文件）
# 用法：QMT -> 模型研究 -> 回测 20261113~20261120 -> 主图 510050.SH / 1m
# 输出全部以 [BARS2] 开头，约 9 行，跑完贴回即可
# 目的：一次定性「期权 1m 取不到」到底是 start_time 为空、参数个数、
#       字段列表，还是合约代码/key 格式
# =====================================================================
import time

BUILD = "20261122-0120"
FALLBACK = "10012448.SHO"
BT_START_YMD = 20261113

_done = []


def _p(msg):
    try:
        print("[BARS2] " + str(msg))
    except BaseException:
        pass


def _clock(C):
    try:
        tt = C.get_bar_timetag(C.barpos)
        t = time.localtime(tt / 1000.0)
        return (t.tm_year * 10000 + t.tm_mon * 100 + t.tm_mday,
                t.tm_hour * 100 + t.tm_min)
    except BaseException:
        return 0, 0


def _keys(d):
    if not isinstance(d, dict):
        return "NOT_DICT:" + type(d).__name__
    ks = list(d.keys())
    return "keys=%s n=%d" % (ks[:3], len(ks))


def _val(d):
    if not isinstance(d, dict) or not d:
        return "no-val"
    v = list(d.values())[0]
    try:
        cols = ",".join([str(c) for c in list(v.columns)[:6]])
    except BaseException:
        cols = "?"
    return "shape=%s cols=%s" % (getattr(v, "shape", None), cols)


def _call(tag, C, *a):
    try:
        d = C.get_market_data_ex(*a)
        _p("%s %s | %s" % (tag, _keys(d), _val(d)))
    except BaseException as e:
        _p("%s EXC %s: %s" % (tag, type(e).__name__, str(e)[:80]))


def _pick(C):
    """优先取平台持仓里的合约（最有价值），没有就用 fallback"""
    try:
        f = globals().get("get_trade_detail_data")
        if f is None:
            f = getattr(C, "get_trade_detail_data")
        lst = f("", "STOCK_OPTION", "POSITION")
    except BaseException:
        return FALLBACK, "nocall"
    for r in (lst or []):
        try:
            vol = int(getattr(r, "m_nVolume", 0) or 0)
            if vol <= 0:
                continue
            code = str(getattr(r, "m_strInstrumentID", "") or "")
            ex = str(getattr(r, "m_strExchangeID", "") or "")
            if not code:
                continue
            if "." not in code and ex:
                code = code + "." + ex
            return code, "pos"
        except BaseException:
            pass
    return FALLBACK, "fb"


def _probe(C):
    if _done:
        return
    ymd, hm = _clock(C)
    if ymd < BT_START_YMD:
        return
    code, src = _pick(C)
    if src != "pos" and ymd < BT_START_YMD + 1:
        return            # 首日还没持仓就先等，避免测到 fallback 误导
    _done.append(1)
    try:
        bt = bool(getattr(C, "do_back_test", False))
    except BaseException:
        bt = "?"
    cur14 = "%08d%04d00" % (ymd, hm)
    _p("START build=%s bt=%r clock=%s" % (BUILD, bt, cur14))
    _p("code=%s src=%s" % (code, src))

    per = "1m"
    F = ["open", "high", "low", "close", "volume"]
    bare = code.split(".")[0]

    # A 导出脚本原样：空 fields + 显式区间 + count=-1 + fill=False（8 个位置参数）
    _call("A_exp", C, [], [code], per, "20261113", "20261120", -1, "none", False)

    # A2 同一调用但只传纯数字代码（测后缀是不是必须）
    if bare != code:
        _call("A2_bare", C, [], [bare], per, "20261113", "20261120", -1, "none", False)
    else:
        _p("A2_bare skip code_has_no_suffix")

    # B 策略现写法：空 start + 有限 count + fill=True + 第9参 subscribe（9 个）
    _call("B_cur", C, F, [code], per, "", cur14, 6, "none", True, True)

    # C 显式 start + 有限 count + fill=False（8 个）
    _call("C_start6", C, F, [code], per, "20261113", cur14, 6, "none", False)

    # D 显式 start + count=-1 + fill=True（8 个）
    _call("D_full", C, F, [code], per, "20261113", cur14, -1, "none", True)

    # E 空 fields + 空 start + 有限 count（单独看空 start 是不是根因）
    _call("E_nostart", C, [], [code], per, "", cur14, 6, "none", True, True)

    # F 原生 xtdata 同参数（对照内置版返回结构是否不同）
    try:
        from xtquant import xtdata
        d = xtdata.get_market_data_ex([], [code], period=per,
                                      start_time="20261113",
                                      end_time="20261120",
                                      count=-1, dividend_type="none",
                                      fill_data=False)
        _p("F_xtdata %s | %s" % (_keys(d), _val(d)))
    except BaseException as e:
        _p("F_xtdata EXC %s: %s" % (type(e).__name__, str(e)[:80]))

    _p("END")


def init(C):
    pass


def after_init(C):
    pass


def handlebar(C):
    _probe(C)
