"""OKX CEX P3 批：shared_pool 主 broker 余额聚合 + dummy 时间口径。

现场两个 bug（2026-10-29 okx_cex 通道实测）：
1) ``DummyDataSource.get_datetime()`` 返回 naive，而 lumibot
   ``market_close_time()`` 是 aware → 每轮 TypeError → on_bot_crash 打死循环；
2) shared_pool 主 broker 不绑子账号 → ``account_balance(None)`` 落到默认账号
   （= 期权占用账号，key 无效）→ 50119 → 余额 0 → portfolio BLOCK。
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


# ── 1) dummy 时间口径 ─────────────────────────────────────────────────────


def test_dummy_data_source_returns_aware_utc():
    from nanobot_quant.brokers._dummy_data_source import DummyDataSource

    dt = DummyDataSource().get_datetime()
    assert dt.tzinfo is not None, "必须 aware，否则 lumibot 时间比较 TypeError"
    assert dt.utcoffset().total_seconds() == 0


def test_dummy_datetime_comparable_with_aware_market_close():
    """复现 lumibot _handle_lifecycle_methods 的比较，naive 会直接抛 TypeError。"""
    from nanobot_quant.brokers._dummy_data_source import DummyDataSource

    current = DummyDataSource().get_datetime()
    market_close = datetime.now(timezone.utc)  # 与 utc_to_local 同性质：aware
    assert (current >= market_close + (current - current)) is True or True
    # 关键断言：比较不抛异常
    _ = current >= market_close


# ── 2) shared_pool 余额聚合 ───────────────────────────────────────────────


def _mk_broker(monkeypatch, sub_account=""):
    from nanobot_quant.brokers import okx_cex_broker as mod

    b = mod.OkxCexBroker.__new__(mod.OkxCexBroker)
    b._sub_account = sub_account
    b._quote = "USDT"
    b._tracked = {}
    return b


def test_shared_pool_balances_aggregate_spot_accounts(monkeypatch):
    from nanobot_quant.brokers import okx_cex_broker as mod

    calls = []

    def fake_balance(account=""):
        calls.append(account)
        return {
            "details": [
                {"ccy": "USDT", "avail_bal": "10", "frozen_bal": "1", "eq_usd": "11"},
                *(
                    [{"ccy": "SOL", "avail_bal": "0.5", "frozen_bal": "0", "eq_usd": "80"}]
                    if account == "bot2"
                    else []
                ),
            ]
        }

    monkeypatch.setattr(mod, "account_balance", fake_balance)
    b = _mk_broker(monkeypatch)
    monkeypatch.setattr(b, "_spot_names", lambda: ["bot2", "bot3"])

    bal = b._balances()
    assert calls == ["bot2", "bot3"], "必须逐账号查询，不得落到默认(期权)账号"
    assert bal["USDT"]["available"] == pytest.approx(20.0)
    assert bal["USDT"]["locked"] == pytest.approx(2.0)
    assert bal["USDT"]["eq_usd"] == pytest.approx(22.0)
    assert bal["SOL"]["eq_usd"] == pytest.approx(80.0)


def test_shared_pool_balances_fail_closed_when_all_accounts_fail(monkeypatch):
    from nanobot_quant.brokers import okx_cex_broker as mod

    def boom(account=""):
        raise RuntimeError("50119 API key doesn't exist")

    monkeypatch.setattr(mod, "account_balance", boom)
    b = _mk_broker(monkeypatch)
    monkeypatch.setattr(b, "_spot_names", lambda: ["bot2", "bot3"])

    assert b._balances() == {}, "全账号失败必须返回 {} 让调用方 fail-closed"


def test_slot_broker_still_queries_its_own_account(monkeypatch):
    from nanobot_quant.brokers import okx_cex_broker as mod

    calls = []

    def fake_balance(account=""):
        calls.append(account)
        return {"details": [{"ccy": "USDT", "avail_bal": "5", "frozen_bal": "0", "eq_usd": "5"}]}

    monkeypatch.setattr(mod, "account_balance", fake_balance)
    b = _mk_broker(monkeypatch, sub_account="bot4")
    monkeypatch.setattr(b, "_spot_names", lambda: ["bot2", "bot3"])

    assert b._balances()["USDT"]["available"] == pytest.approx(5.0)
    assert calls == ["bot4"], "绑了子账号的 slot broker 不得走聚合"


# ── 3) 现货池账号名排除期权账号 ──────────────────────────────────────────


def test_spot_account_names_excludes_option_account(monkeypatch):
    from nanobot_quant import okx_cex_credentials as creds

    monkeypatch.setattr(
        creds,
        "list_sub_accounts",
        lambda c=None: [
            {"name": "DreamShepherdbot1", "configured": True},
            {"name": "DreamShepherdbot2", "configured": True},
            {"name": "DreamShepherdbot3", "configured": False},
        ],
    )
    monkeypatch.setattr(creds, "option_account_name", lambda: "DreamShepherdbot1")
    assert creds.spot_account_names() == ["DreamShepherdbot2"]
