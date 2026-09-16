"""期权 instId 反解回退（`_parse_inst_id`）单测。

背景：OKX `get_instruments` **只返回当前在售合约** —— 已到期合约查不到。
回测必然要枚举历史合约，故 `fetch_lifecycle` 在 `_find_inst` 失败时
回退到从 instId 字符串反解元数据。

instId 格式：``SOL-USD_UM-260912-77-P``（家族-到期日-strike-类型），
尾部固定 3 段 → **从右侧取段**（`_UM` 后缀会破坏左侧段位假设）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nanobot_quant import okx_options_data as od


def _ms(y, m, d, hh=8):
    return int(datetime(y, m, d, hh, tzinfo=timezone.utc).timestamp() * 1000)


# ── 正常解析 ──────────────────────────────────────────────────────────


def test_parse_inst_id_basic_put():
    inst = od._parse_inst_id("SOL-USD_UM-260912-77-P")
    assert inst["instId"] == "SOL-USD_UM-260912-77-P"
    assert inst["instFamily"] == "SOL-USD_UM"
    assert inst["uly"] == "SOL-USD"
    assert inst["stk"] == "77"
    assert inst["optType"] == "P"
    # 到期时刻补 08:00 UTC（OKX 期权每日到期）
    assert inst["expTime"] == str(_ms(2026, 9, 12))
    assert inst["_parsed"] is True


def test_parse_inst_id_call_and_decimal_strike():
    inst = od._parse_inst_id("SOL-USD_UM-260912-101.5-C")
    assert inst["stk"] == "101.5"
    assert inst["optType"] == "C"


def test_parse_inst_id_lowercase_normalised():
    inst = od._parse_inst_id("sol-usd_um-260912-77-p")
    assert inst["instId"] == "SOL-USD_UM-260912-77-P"
    assert inst["instFamily"] == "SOL-USD_UM"


def test_parse_inst_id_each_supported_family():
    for fam, base in (("BTC-USD_UM", "BTC"), ("ETH-USD_UM", "ETH"),
                      ("SOL-USD_UM", "SOL"), ("XAU-USD_UM", "XAU")):
        inst = od._parse_inst_id(f"{fam}-260912-100-P")
        assert inst["instFamily"] == fam
        assert inst["uly"] == f"{base}-USD"


# ── 非法输入 fail-closed ──────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "SOL-USD_UM-260912-77",           # 段数不足
    "SOL-USD_UM-260912-77-X",         # 期权类型非法
    "SOL-USD_UM-260912-ab-77-P",      # 到期日非 yymmdd
    "SOL-USD_UM-1789653069900-77-P",  # 到期日是毫秒时间戳（旧测试造的坏输入）
    "SOL-USD_UM-260912-x-77-P",       # strike 非数值
    "DOGE-USD_UM-260912-77-P",        # 家族不在白名单
])
def test_parse_inst_id_rejects_bad_input(bad):
    with pytest.raises(od.OkxSdkError):
        od._parse_inst_id(bad)


# ── fetch_lifecycle 回退路径 ──────────────────────────────────────────


def test_fetch_lifecycle_falls_back_for_expired_contract(monkeypatch):
    """instruments 查不到（已到期）→ 反解回退，mark 照常返回，不抛「不存在」。"""

    def _not_found(inst_id):
        raise od.OkxSdkError(f"合约不存在或已被移除: {inst_id}")

    monkeypatch.setattr(od, "_find_inst", _not_found)
    monkeypatch.setattr(od, "_mark_candle_pages",
                        lambda inst_id, bar: ({1757000000000: 0.42}, False))
    monkeypatch.setattr(od, "_ref_prices", lambda *a, **k: {})

    life = od.fetch_lifecycle("SOL-USD_UM-260912-77-P", "5m")
    assert life["strike"] == 77.0
    assert life["opt_type"] == "P"
    assert life["exp_ms"] == _ms(2026, 9, 12)
    assert life["inst_parsed"] is True
    assert life["rows"] == [{"ts": 1757000000000, "mark_px": 0.42, "ref_px": None}]
    # 合约规格缺失 → FAMILY_LOT 常量兜底（SOL = 0.1 币/张）
    assert life["lot_coin"] == 0.1


def test_fetch_lifecycle_uses_instrument_when_listed(monkeypatch):
    """在售合约走正常路径（不触发回退），lot_coin 取自官方 ctVal×ctMult。"""

    monkeypatch.setattr(od, "_find_inst", lambda inst_id: {
        "instId": inst_id, "instFamily": "SOL-USD_UM", "uly": "SOL-USD",
        "stk": "99", "optType": "P", "expTime": str(_ms(2026, 9, 12)),
        "listTime": str(_ms(2026, 9, 9)), "ctVal": "1", "ctMult": "0.1",
    })
    monkeypatch.setattr(od, "_mark_candle_pages",
                        lambda inst_id, bar: ({1757000000000: 1.23}, False))
    monkeypatch.setattr(od, "_ref_prices", lambda *a, **k: {})

    life = od.fetch_lifecycle("SOL-USD_UM-260912-99-P", "5m")
    assert life["inst_parsed"] is False       # 未走回退
    assert life["strike"] == 99.0
    assert life["lot_coin"] == 0.1
    assert life["list_ms"] == _ms(2026, 9, 9)


def test_fetch_lifecycle_still_rejects_unknown_family(monkeypatch):
    """回退不能放宽家族白名单（DOGE 仍拒绝）。"""
    monkeypatch.setattr(od, "_find_inst", lambda inst_id: (_ for _ in ()).throw(
        od.OkxSdkError("不存在")))
    with pytest.raises(od.OkxSdkError, match="未知标的家族"):
        od.fetch_lifecycle("DOGE-USD_UM-260912-77-P", "5m")


def test_fetch_lifecycle_raises_when_no_mark_data(monkeypatch):
    """回退后仍无 mark 数据 → 明确报错（不静默返回空）。"""
    monkeypatch.setattr(od, "_find_inst", lambda inst_id: (_ for _ in ()).throw(
        od.OkxSdkError("不存在")))
    monkeypatch.setattr(od, "_mark_candle_pages", lambda inst_id, bar: ({}, False))
    with pytest.raises(od.OkxSdkError, match="暂无 mark K 线数据"):
        od.fetch_lifecycle("SOL-USD_UM-260912-77-P", "5m")
