"""``ashare.paper_replay`` 单测：数据集扫描 / 输入校验 / 回放 / 结果留存。

只测「模块行为与结构不变量」，不测策略经济性（回放不是回测引擎）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from nanobot_quant import credential_registry as creg
from nanobot_quant.ashare import paper_replay as prp


# ── 合成数据 ──────────────────────────────────────────────


def _write_etf(path, n=120, start_price=3.0, step=-0.002, t0=datetime(2026, 10, 1, 9, 30)):
    rows = ["stime,amount,close,high,low,open,openInterest,preClose"]
    for i in range(n):
        t = t0 + timedelta(minutes=i)
        px = start_price + step * i
        rows.append(f"{t:%Y%m%d%H%M%S},1,{px:.4f},{px + 0.001:.4f},"
                    f"{px - 0.001:.4f},{px:.4f},0,{px:.4f}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return n


def _write_contracts(path, underlying="510050.SH", code="510050SH_202612P02800",
                     expiry="20261223"):
    path.write_text(
        "undl,code,month,type,strike,expire,mult,tick\n"
        f"{underlying},{code},202612,P,2.80,{expiry},10000,0.0001\n",
        encoding="utf-8")


def _write_option_shard(path, code="510050SH_202612P02800", n=120, px=0.05,
                        t0=datetime(2026, 10, 1, 9, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ["ts,code,open,high,low,close,volume,amount"]
    for i in range(n):
        t = t0 + timedelta(minutes=i)
        rows.append(f"{t:%Y%m%d%H%M%S},{code},{px},{px},{px},{px},10,1")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


@pytest.fixture()
def dataset(tmp_path):
    root = tmp_path / "ashare_data"
    etf_run = root / "qmt-etf-1m" / "run_20261001_090000"
    etf_run.mkdir(parents=True)
    etf_csv = etf_run / "510050_SH_1m.csv"
    _write_etf(etf_csv)

    opt_run = root / "qmt-option-1m" / "run_20261001_100000"
    shard = opt_run / "opt1m_510050SH_202612_p01.csv"
    _write_option_shard(shard)

    contracts = root / "qmt-option-1m" / "meta_run_20261001" / "contracts_all.csv"
    contracts.parent.mkdir(parents=True)
    _write_contracts(contracts)

    return {"root": root, "etf_csv": etf_csv, "opt_dir": opt_run,
            "shard": shard, "contracts": contracts}


# ── 数据集扫描 ────────────────────────────────────────────


def test_list_datasets_scans_runs(dataset):
    d = prp.list_datasets(dataset["root"], force=True)
    assert d["root"] == str(dataset["root"])
    assert [r["name"] for r in d["etf_runs"]] == ["run_20261001_090000"]
    f = d["etf_runs"][0]["files"][0]
    assert f["symbol"] == "510050.SH"
    assert f["first"].startswith("20261001")
    assert [r["name"] for r in d["opt_runs"]] == ["run_20261001_100000"]
    assert d["opt_runs"][0]["shards"] == 1
    assert d["opt_runs"][0]["underlyings"] == ["510050SH"]
    assert d["contracts"][0]["rows"] == 1
    assert d["contracts"][0]["months"] == ["202612"]
    assert d["defaults"]["underlying"] == "510050.SH"


def test_list_datasets_missing_dirs_reports_notes(tmp_path):
    d = prp.list_datasets(tmp_path / "empty", force=True)
    assert d["etf_runs"] == [] and d["opt_runs"] == [] and d["contracts"] == []
    assert d["notes"]  # 必须显式说明缺什么，不能静默给空下拉


# ── 输入校验 ──────────────────────────────────────────────


def test_resolve_inputs_ok_and_defaults(dataset):
    kw = prp.resolve_inputs({
        "etf_csv": str(dataset["etf_csv"]),
        "opt_dirs": [str(dataset["opt_dir"])],
        "contracts_csv": str(dataset["contracts"]),
    }, root=dataset["root"])
    assert kw["underlying"] == "510050.SH"
    assert kw["arms"] == "both"
    assert kw["initial"] == prp.DEFAULT_INITIAL
    assert kw["opt_dirs"] == [str(dataset["opt_dir"])]


def test_resolve_inputs_rejects_outside_root(dataset, tmp_path):
    outside = tmp_path / "outside.csv"
    outside.write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="必须位于数据根目录"):
        prp.resolve_inputs({
            "etf_csv": str(outside),
            "opt_dirs": [str(dataset["opt_dir"])],
            "contracts_csv": str(dataset["contracts"]),
        }, root=dataset["root"])


def test_resolve_inputs_rejects_empty_opt_dirs(dataset):
    with pytest.raises(ValueError, match="opt_dirs"):
        prp.resolve_inputs({
            "etf_csv": str(dataset["etf_csv"]),
            "opt_dirs": [],
            "contracts_csv": str(dataset["contracts"]),
        }, root=dataset["root"])


def test_resolve_inputs_rejects_bad_symbol(dataset):
    with pytest.raises(ValueError, match="标的格式"):
        prp.resolve_inputs({
            "etf_csv": str(dataset["etf_csv"]),
            "opt_dirs": [str(dataset["opt_dir"])],
            "contracts_csv": str(dataset["contracts"]),
            "underlying": "510050",
        }, root=dataset["root"])


# ── 回放 ──────────────────────────────────────────────────


def _run(dataset, **kw):
    base = dict(etf_csv=str(dataset["etf_csv"]), opt_dirs=[str(dataset["opt_dir"])],
                contracts_csv=str(dataset["contracts"]), underlying="510050.SH",
                initial=500000.0, arms="both", min_dte=6, max_dte=400)
    base.update(kw)
    return prp.run_replay(**base)


def test_run_replay_both_arms_end_to_end(dataset):
    seen = []
    p = _run(dataset, progress=lambda ev: seen.append(ev["stage"]))
    assert p["ok"] is True
    assert set(p["arms"]) == {"A", "B"}
    a = p["arms"]["A"]
    assert a["fills"] >= 1                      # 连续下跌应触发买9 → 卖出开仓
    assert a["consistency"]["ok"] is True
    assert p["arms"]["A"]["daily"], "按日净值必须非空（页面/报告都要用）"
    w = p["data_window"]
    assert w["etf_bars"] > 0 and w["opt_codes"] == 1
    assert "有效重叠区间" in p["markdown"]
    assert "口径与数据限制" in p["markdown"]
    assert "load_etf" in seen and "replay" in seen


def test_run_replay_slice_reduces_bars(dataset):
    full = _run(dataset, arms="a")
    half = _run(dataset, arms="a", start="2026-10-01", end="2026-10-01")
    assert half["data_window"]["etf_bars"] <= full["data_window"]["etf_bars"]
    # 只跑一天 → 60 根（09:30 起 120 分钟的上午）
    assert half["data_window"]["etf_bars"] == 120


def test_run_replay_rejects_reversed_window(dataset):
    with pytest.raises(ValueError, match="区间非法"):
        _run(dataset, start="2026-10-02", end="2026-10-01")


def test_run_replay_rejects_bad_arms(dataset):
    with pytest.raises(ValueError, match="arms"):
        _run(dataset, arms="c")


def test_run_replay_warns_when_options_start_later(dataset):
    """期权批次比 ETF 晚 → 必须显式提示「该段信号不会成交」。"""
    shard = dataset["opt_dir"] / "opt1m_510050SH_202612_p01.csv"
    _write_option_shard(shard, n=30, t0=datetime(2026, 10, 1, 10, 30))
    p = _run(dataset, arms="a")
    notes = " ".join(p["data_window"]["notes"])
    assert "无期权行情" in notes


# ── 结果留存 ──────────────────────────────────────────────


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(creg, "_get_storage_dir", lambda: str(tmp_path))
    return tmp_path


def test_save_list_load_result(dataset, storage):
    p = _run(dataset, arms="a")
    rid = prp.save_result(p)
    assert rid.startswith("rp-")
    rows = prp.list_results()
    assert [r["run_id"] for r in rows] == [rid]
    assert rows[0]["arms"]["A"]["equity"] == p["arms"]["A"]["equity"]
    back = prp.load_result(rid)
    assert back["run_id"] == rid and back["markdown"] == p["markdown"]


def test_load_result_rejects_bad_run_id(storage):
    for bad in ("../../etc/passwd", "rp-x", ""):
        with pytest.raises(ValueError):
            prp.load_result(bad)


def test_prune_keeps_latest_n(dataset, storage):
    """超过上限自动清理，且保留下来的都来自本批次（不误删无关文件）。"""
    p = _run(dataset, arms="a")
    p.pop("run_id", None)                      # 不带 run_id 才会各写一份
    rids = [prp.save_result(dict(p)) for _ in range(prp.KEEP_RESULTS + 3)]
    files = sorted(x.stem for x in prp.results_dir().glob("rp-*.json"))
    assert len(files) == prp.KEEP_RESULTS
    assert all(f in rids for f in files)


def test_job_single_flight(dataset, storage, monkeypatch):
    monkeypatch.setitem(prp._JOB, "running", True)
    with pytest.raises(ValueError, match="已有回放"):
        prp.start_job(etf_csv=str(dataset["etf_csv"]),
                      opt_dirs=[str(dataset["opt_dir"])],
                      contracts_csv=str(dataset["contracts"]))


def test_page_has_replay_block():
    """页面必须带区块与端点（结构性锁定：缺了就是「页面能开但没入口」）。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "src" / "nanobot_quant" /
            "ashare_page.html").read_text(encoding="utf-8")
    for marker in ("rp-wrap", "rp-etf", "rp-opts", "rp-contracts", "rp-symbol",
                   "rp-start", "rp-end", "rp-initial", "rp-arms", "rp-go",
                   "rp-progress", "rp-result", "rp-history", "rp-copy"):
        assert marker in html, marker
    assert "/config/ashare/paper/replay/datasets" in html
    assert "/config/ashare/paper/replay/start" in html
    assert "/config/ashare/paper/replay/state" in html
    assert "/config/ashare/paper/replay/result" in html
    assert html.count("</script") == 1


# ── HTTP 端点（接线检查：结构对不对、错误码是否 fail-closed） ──


def _client(tmp_path, monkeypatch, *, authorized=True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nanobot_quant import ashare_handlers as ah
    from nanobot_quant.ashare import paper_runner as pr
    from nanobot_quant.ashare import tape

    monkeypatch.setattr(creg, "_get_storage_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pr, "sync", lambda: None)
    monkeypatch.setattr(tape, "sync", lambda: None)
    (tmp_path / "ashare_data").mkdir(exist_ok=True)
    monkeypatch.setattr(prp, "data_root", lambda: tmp_path / "ashare_data")
    # 鉴权单测在 test_ashare_page 覆盖；这里绕过 session，只测端点本身
    monkeypatch.setattr(ah, "_authorized",
                        lambda req, gk: (None, True) if authorized else ("请先登录", False))

    app = FastAPI()

    class GK:
        class _P:
            @staticmethod
            def is_commander(u):
                return authorized

        _platform = _P

    ah.register_ashare_routes(app, GK())
    return TestClient(app)


def test_replay_endpoints_wired(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)

    r = c.get("/config/ashare/paper/replay/datasets")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["root"].startswith(str(tmp_path))

    r = c.get("/config/ashare/paper/replay/state")
    assert r.status_code == 200
    j = r.json()
    assert "running" in j and isinstance(j["results"], list)

    r = c.get("/config/ashare/paper/replay/result", params={"run_id": "../x"})
    assert r.status_code == 400 and "非法" in r.json()["error"]

    r = c.post("/config/ashare/paper/replay/start", json={})
    assert r.status_code == 400 and "etf_csv" in r.json()["error"]


def test_replay_endpoints_require_login(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, authorized=False)
    for path in ("/config/ashare/paper/replay/datasets",
                 "/config/ashare/paper/replay/state",
                 "/config/ashare/paper/replay/result?run_id=rp-20260101-000000-0000"):
        assert c.get(path).status_code == 403
    assert c.post("/config/ashare/paper/replay/start", json={}).status_code == 403
