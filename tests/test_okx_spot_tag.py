"""OKX 现货单 `tag` 参数合法性（2026-10-29 首笔真实现货 BUY 暴露）。

OKX 的 ``tag``（订单标签）只接受 **1–16 位字母数字**：

- ``""``（空串）→ ``51000 Parameter tag error``
- ``"td-live"``（含连字符）→ 同样 51000

修复前 ``OkxCexBroker`` 传的是 ``"td-live"`` ⇒ BNB 三个 slot 的 BUY 全部失败
（``TD BATCH BUY FAIL ... 51000 Parameter tag error``），且随后日志把它误报成
「无可用资金 slot」。期权线一直正常是因为它的标签是 ``nbputo1``/``nbputc1``
这类纯字母数字。
"""
from __future__ import annotations

import re

import pytest

from nanobot_quant import okx_spot
from nanobot_quant.brokers import okx_cex_broker as broker_mod


class _FakeTrade:
    def __init__(self, captured, resp):
        self._c, self._r = captured, resp

    def set_order(self, **kw):
        self._c["set_order"] = kw
        return self._r

    def send_request(self, *a, **kw):
        self._c["send_request"] = kw
        return self._r


def _ok(rows):
    return {"code": "0", "data": rows}


def test_empty_tag_is_omitted_not_sent_as_empty_string(monkeypatch):
    """不传 tag 才是「没标签」的正确表达；空串会被 OKX 判 51000。"""
    import nanobot_quant.okx_sdk as sdk

    captured = {}
    monkeypatch.setattr(sdk, "trade_for",
                        lambda c: _FakeTrade(captured, _ok([{"sCode": "0", "ordId": "A1"}])))
    okx_spot.market_order({"api_key": "k"}, inst_id="SOL-USDT", side="buy", sz="0.5")
    assert "tag" not in captured["set_order"]

    okx_spot.market_order({"api_key": "k"}, inst_id="SOL-USDT", side="buy", sz="0.5", tag="t1")
    assert captured["set_order"]["tag"] == "t1"


def test_broker_tag_is_okx_legal():
    """broker 的标签必须是纯字母数字、1–16 位（含 `-`/`_` 会 51000）。"""
    tag = broker_mod.ORDER_TAG
    assert re.fullmatch(r"[A-Za-z0-9]{1,16}", tag), tag


def test_broker_order_uses_legal_tag(monkeypatch):
    """实际下单路径带的必须是合法标签（回归 51000 Parameter tag error）。"""
    import nanobot_quant.okx_sdk as sdk

    captured = {}
    monkeypatch.setattr(sdk, "trade_for",
                        lambda c: _FakeTrade(captured, _ok([{"sCode": "0", "ordId": "A9"}])))
    req = okx_spot.strict_order(
        {"api_key": "k"}, inst_id="BNB-USDC", side="buy", sz="0.005",
        tgt_ccy="base_ccy", tag=broker_mod.ORDER_TAG,
    )
    assert req["ord_id"] == "A9"
    sent = captured["set_order"]["tag"]
    assert re.fullmatch(r"[A-Za-z0-9]{1,16}", sent), sent
