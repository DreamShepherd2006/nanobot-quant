"""OKX CEX 现货通道 P1 基础测试（2026-10-28，docs/quant-system.md §33.52）。

P1 定位：可见的基础设施，**不接执行**（broker 与通道注册在 P2）——
故本文件只验证「新增能力先落地、存量行为不变」。

覆盖：
- ``BrokerSpec.slot_model`` 默认 ``per_account``、非法值 fail-closed
- ``open_lot`` 记录 ``ord_id`` / ``fill_px``（仅在传入时写入，
  Gate/DEX 台账字节级不变）
- ``per_account`` 批次容量 fail-closed（§33.52 拍板 b）
- ``td_batches`` 上限 50 → 200
- ``okx_cex`` 数据源 ``kind`` → ``executable``
"""
from __future__ import annotations

import json

import pytest

from nanobot_quant import batches as batches_mod
from nanobot_quant import exec_params as ep
from nanobot_quant.brokers import registry as reg
from nanobot_quant.data_sources import get_data_source


# ── BrokerSpec.slot_model ────────────────────────────────────────────────

def test_slot_model_defaults_to_per_account():
    spec = reg.BrokerSpec(name="x", display="X", family="cex")
    assert spec.slot_model == "per_account"


def test_slot_model_rejects_unknown_value():
    with pytest.raises(ValueError, match="slot_model"):
        reg.BrokerSpec(name="x", display="X", family="cex", slot_model="poolish")


def test_existing_channels_stay_per_account():
    """Gate / OKX DEX 的存量语义必须是 per_account（P1 零行为变化）。"""
    for name in ("gate", "okx_dex"):
        assert reg.get_broker(name).slot_model == "per_account"


# ── open_lot: ord_id / fill_px ───────────────────────────────────────────

def test_open_lot_without_ord_id_keeps_legacy_shape(tmp_path):
    bm = batches_mod.BatchManager(
        symbol="SOL", account_ids=["gate_bot1"], path=tmp_path / "b.json"
    )
    lot = bm.open_lot(1.0, 100.0)
    # 未传 ord_id/fill_px → 不写字段（Gate/DEX 台账字节级不变）
    assert set(lot["lot"]) == {"qty", "entry_price", "entry_time"}


def test_open_lot_records_ord_id_and_fill_px(tmp_path):
    bm = batches_mod.BatchManager(
        symbol="SOL", account_ids=["okx_main"], path=tmp_path / "b.json"
    )
    lot = bm.open_lot(0.05, 118.13, ord_id="3123456789", fill_px=118.2)
    assert lot["lot"]["ord_id"] == "3123456789"
    assert lot["lot"]["fill_px"] == 118.2
    bm.save()
    saved = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    assert saved["slots"][0]["lot"]["ord_id"] == "3123456789"


# ── per_account 批次容量校验（§33.52 拍板 b）─────────────────────────────

def _patch_slot_map(monkeypatch, mapping):
    import nanobot_quant.gate_credentials as gc

    monkeypatch.setattr(gc, "load_slot_map", lambda: dict(mapping))


def test_ensure_batches_rejects_more_than_sub_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        batches_mod, "batches_path", lambda *a, **kw: tmp_path / "b.json"
    )
    _patch_slot_map(monkeypatch, {"1": "gate_bot1", "2": "gate_bot2"})
    bm, msg = batches_mod.ensure_batches(5, symbol="SOL", channel="gate")
    assert bm is None
    assert "超过实际子账号数 2" in msg


def test_ensure_batches_rejects_when_no_sub_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        batches_mod, "batches_path", lambda *a, **kw: tmp_path / "b.json"
    )
    _patch_slot_map(monkeypatch, {})
    bm, msg = batches_mod.ensure_batches(1, symbol="SOL", channel="gate")
    assert bm is None
    assert "未配置子账号" in msg


def test_ensure_batches_accepts_within_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        batches_mod, "batches_path", lambda *a, **kw: tmp_path / "b.json"
    )
    _patch_slot_map(monkeypatch, {"1": "gate_bot1", "2": "gate_bot2"})
    bm, msg = batches_mod.ensure_batches(2, symbol="SOL", channel="gate")
    assert bm is not None
    assert "2 slots" in msg  # 成功路径也带说明消息（现有行为）
    assert len(bm.slots) == 2


# ── save_exec_params 前置校验 ────────────────────────────────────────────

def _isolate_exec_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "exec_params_path", lambda: tmp_path / "exec.json")


def test_save_rejects_scene_batches_over_sub_accounts(tmp_path, monkeypatch):
    _isolate_exec_path(tmp_path, monkeypatch)
    _patch_slot_map(monkeypatch, {"1": "gate_bot1"})
    res = ep.save_exec_params(
        {
            "execution_channel": "gate",
            "scenes": {"high": {"enabled": True, "batches": 5}},
        }
    )
    assert res["ok"] is False
    assert "超过实际子账号数 1" in res["error"]


def test_save_allows_scene_batches_within_capacity(tmp_path, monkeypatch):
    _isolate_exec_path(tmp_path, monkeypatch)
    _patch_slot_map(monkeypatch, {"1": "gate_bot1", "2": "gate_bot2"})
    res = ep.save_exec_params(
        {
            "execution_channel": "gate",
            "scenes": {"high": {"enabled": True, "batches": 2}},
        }
    )
    assert res["ok"] is True


def test_save_skips_capacity_check_for_disabled_scene(tmp_path, monkeypatch):
    """停用场景不运行，其 batches 不参与容量校验（不阻塞保存）。"""
    _isolate_exec_path(tmp_path, monkeypatch)
    _patch_slot_map(monkeypatch, {"1": "gate_bot1"})
    res = ep.save_exec_params(
        {
            "execution_channel": "gate",
            "scenes": {"high": {"enabled": False, "batches": 20}},
        }
    )
    assert res["ok"] is True


def test_capacity_check_skips_shared_pool(tmp_path, monkeypatch):
    """shared_pool 通道不受子账号数限制（逻辑槽位）。"""
    _isolate_exec_path(tmp_path, monkeypatch)
    _patch_slot_map(monkeypatch, {"1": "gate_bot1"})
    monkeypatch.setattr(
        reg,
        "spec_for_channel",
        lambda ch: reg.BrokerSpec(
            name="okx_cex", display="OKX CEX", family="cex", slot_model="shared_pool"
        ),
    )
    merged = {"execution_channel": "okx_cex",
              "scenes": {"high": {"enabled": True, "batches": 100}}}
    assert ep._validate_batch_capacity(merged) is None


def test_capacity_check_skips_unknown_credentials(tmp_path, monkeypatch):
    """凭证不可读（IO 抖动）时不阻塞保存——运行时 ensure_batches 兜底。"""
    _isolate_exec_path(tmp_path, monkeypatch)

    def boom():
        raise OSError("fuse jitter")

    import nanobot_quant.gate_credentials as gc

    monkeypatch.setattr(gc, "load_slot_map", boom)
    merged = {"execution_channel": "gate",
              "scenes": {"high": {"enabled": True, "batches": 100}}}
    assert ep._validate_batch_capacity(merged) is None


# ── td_batches 上限 ──────────────────────────────────────────────────────

def test_td_batches_max_raised_to_200():
    assert ep.PARAM_META["td_batches"]["max"] == 200
    assert ep.validate_exec_param("td_batches", 200) is None
    assert ep.validate_exec_param("td_batches", 201) is not None


# ── okx_cex 数据源 kind ──────────────────────────────────────────────────

def test_okx_cex_kind_stays_research_in_p1():
    """P1 只做基础设施，``kind`` 保留 research；提升为 executable 与
    broker 注册同批（P2）——``tests/test_data_sources.py`` 已锁定该契约。"""
    assert get_data_source("okx_cex").kind == "research"


def test_executable_sources_unchanged():
    """P1 不得新增可执行数据源（零行为变化的判据之一）。"""
    from nanobot_quant.data_sources import executable_sources

    assert executable_sources() == ["gate_cex", "onchainos"]
