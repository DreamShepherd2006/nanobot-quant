"""贝叶斯闸门单测：单特征（F1）拟合/预测/持久化 + 多特征融合预留。"""
import json

import numpy as np
import pandas as pd
import pytest

from nanobot_quant.environment.bayes import NaiveBayesGate, make_h0_label, session_code
from nanobot_quant.environment.gate import (
    DEFAULT_LOOKUP_CFG,
    GateConfig,
    zone_from_lookup,
    zone_from_prob,
)


def make_training_df(n=3000, seed=7):
    """合成训练数据：F1 越高 → H0 概率越高（模拟实验结论）。"""
    rng = np.random.default_rng(seed)
    f1 = rng.lognormal(0, 0.4, n)
    logit = 1.2 * (f1 - 1.2) - 0.2
    prob = 1 / (1 + np.exp(-logit))
    h0 = (rng.random(n) < prob).astype(int)
    return pd.DataFrame({"F1": f1, "h0": h0})


def test_fit_single_feature_and_predict_monotonic():
    gate = NaiveBayesGate().fit(make_training_df())
    assert gate.features == ["F1"]
    p_lo = gate.predict({"F1": 0.8})
    p_hi = gate.predict({"F1": 2.5})
    assert p_hi > p_lo
    assert 0.0 <= p_lo <= 1.0 and 0.0 <= p_hi <= 1.0


def test_fit_multi_feature_fusion():
    """多特征融合预留：追加独立特征后 predict 用两特征。"""
    rng = np.random.default_rng(3)
    n = 3000
    f1 = rng.lognormal(0, 0.4, n)
    x3 = rng.normal(0, 1.5, n)
    logit = 1.2 * (f1 - 1.2) + 0.8 * x3 - 0.2
    h0 = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int)
    df = pd.DataFrame({"F1": f1, "X3": x3, "h0": h0})
    gate = NaiveBayesGate().fit(df)
    assert set(gate.features) == {"F1", "X3"}
    assert gate.predict({"F1": 2.5, "X3": 2.0}) > gate.predict({"F1": 0.8, "X3": -2.0})


def test_fit_discrete_feature_session():
    """离散特征（美股时段 0/1/2）：盘中被套率最高，周末最低。"""
    rng = np.random.default_rng(11)
    n = 3000
    f1 = rng.lognormal(0, 0.4, n)
    sess = rng.integers(0, 3, n)
    base = {0: 0.20, 1: 0.45, 2: 0.60}      # 时段基线被套率
    prob = 0.5 + 0.5 * (f1 - 1.2) * 0.6      # F1 上调风险
    prob = np.clip(prob + (np.array([base[s] for s in sess]) - 0.42), 0.02, 0.98)
    h0 = (rng.random(n) < prob).astype(int)
    df = pd.DataFrame({"F1": f1, "SESSION": sess, "h0": h0})
    gate = NaiveBayesGate().fit(df, features=["F1", "SESSION"], discrete_features=["SESSION"])
    p_weekend = gate.predict({"F1": 1.0, "SESSION": 0})
    p_intraday = gate.predict({"F1": 1.0, "SESSION": 2})
    assert p_intraday > p_weekend
    # 离散桶索引
    assert gate.bucket_index("SESSION", 2) == 2
    assert gate.bucket_index("SESSION", 5) == 2   # 越界钳制
    assert gate.bucket_index("SESSION", -1) == 0


def test_session_code():
    """美股时段编码：周末 0、盘前盘后 1、盘中 2。"""
    ts = pd.to_datetime(["2026-08-29 14:00:00",      # 周六 → 周末
                         "2026-08-31 12:00:00",      # 周一 12:00 UTC = 08:00 ET → 盘前
                         "2026-08-31 14:30:00",      # 周一 14:30 UTC = 10:30 ET → 盘中
                         "2026-08-31 21:00:00"],     # 周一 21:00 UTC = 17:00 ET → 盘后
                        utc=True)
    code = session_code(ts)
    assert list(code) == [0, 1, 2, 1]


def test_fit_requires_enough_samples():
    with pytest.raises(ValueError):
        NaiveBayesGate().fit(make_training_df(50))


def test_save_load_roundtrip(tmp_path):
    gate = NaiveBayesGate().fit(make_training_df())
    path = tmp_path / "gate.json"
    gate.save(str(path))
    loaded = NaiveBayesGate.load(str(path))
    assert loaded.predict({"F1": 1.5}) == pytest.approx(gate.predict({"F1": 1.5}), abs=1e-12)
    assert loaded.prior_h0 == pytest.approx(gate.prior_h0)
    assert loaded.features == gate.features


def test_predict_requires_fit():
    with pytest.raises(RuntimeError):
        NaiveBayesGate().predict({"F1": 1.0})


def test_predict_frame():
    gate = NaiveBayesGate().fit(make_training_df())
    df = pd.DataFrame({"F1": [0.8, 1.5, 2.5]})
    ps = gate.predict_frame(df)
    assert len(ps) == 3
    assert ps.iloc[2] > ps.iloc[0]


def test_make_h0_label():
    close = np.concatenate([np.full(30, 100.0), np.linspace(100, 95, 12), np.full(6, 100.0)])
    df = pd.DataFrame({"Close": close})
    h0 = make_h0_label(df, lookback=12, threshold=0.005)
    assert h0.iloc[-12:].isna().all()      # 尾部无未来数据
    # 平段（索引 0，未来 12 根全 100）→ 无回撤 → 0
    assert h0.iloc[0] == 0.0
    # 下跌段起点（索引 30，未来 12 根最低 95，回撤 5% > 0.5%）→ 1
    assert h0.iloc[30] == 1.0
    # 下跌段内（索引 35，未来仍跌到 95）→ 1
    assert h0.iloc[35] == 1.0


def test_zone_lookup_basic():
    cfg = GateConfig()
    assert zone_from_lookup(0.5, 1.0, cfg) == "green"
    assert zone_from_lookup(1.8, 1.0, cfg) == "yellow"
    assert zone_from_lookup(2.5, 1.0, cfg) == "red"
    assert zone_from_lookup(0.5, 1.4, cfg) == "yellow"
    assert zone_from_lookup(0.5, 2.0, cfg) == "red"


def test_zone_lookup_fail_closed():
    assert zone_from_lookup(None, 1.0) == "red"
    assert zone_from_lookup(0.5, float("nan")) == "red"


def test_zone_prob_basic():
    cfg = GateConfig()
    assert zone_from_prob(0.20, cfg) == "green"
    assert zone_from_prob(0.45, cfg) == "yellow"
    assert zone_from_prob(0.80, cfg) == "red"
    assert zone_from_prob(None, cfg) == "red"


def test_gate_config_roundtrip():
    cfg = GateConfig()
    cfg2 = GateConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert cfg2.lookup == DEFAULT_LOOKUP_CFG
