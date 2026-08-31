"""贝叶斯闸门单测：单特征（F1）拟合/预测/持久化 + 多特征融合预留。"""
import json

import numpy as np
import pandas as pd
import pytest

from nanobot_quant.environment.bayes import NaiveBayesGate, make_h0_label, session_code
from nanobot_quant.environment.gate import (
    GateConfig,
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


def test_laplace_smoothing_no_zero_likelihood():
    """① 拉普拉斯平滑：构造训练集某特征桶在 H0/H1 下零计数，
    拟合后似然表不得出现零概率（防后验 0/1 崩溃）。"""
    rng = np.random.default_rng(7)
    n = 2000
    f1 = rng.lognormal(0, 0.3, n)
    sess = np.where(rng.random(n) < 0.9, 2, 0)   # 盘前盘后(1) 几乎缺失
    prob = np.clip(0.3 + 0.6 * (f1 - 1.2), 0.01, 0.99)
    h0 = (rng.random(n) < prob).astype(int)
    df = pd.DataFrame({"F1": f1, "SESSION": sess, "h0": h0})
    gate = NaiveBayesGate().fit(df, features=["F1", "SESSION"], discrete_features=["SESSION"])
    for feat in ["F1", "SESSION"]:
        assert not (gate.like[feat] == 0).any(), f"{feat} 似然表存在零概率"
    # 从未出现的组合（盘前盘后）→ 后验必须有定义（非 0 非 1）
    p = gate.predict({"F1": 1.0, "SESSION": 1})
    assert 0 < p < 1


def test_missing_cell_fallback_marginal():
    """② 缺失格子回退：联合格子缺失时后验由边际似然乘积给出，
    等价于回退到边缘（时段/F1）后验，不会崩为 0 或 1。"""
    rng = np.random.default_rng(13)
    n = 3000
    f1 = rng.lognormal(0, 0.4, n)
    sess = rng.integers(0, 3, n)
    base = {0: 0.2, 1: 0.45, 2: 0.6}
    prob = np.clip(0.4 + 0.5 * (f1 - 1.2) + np.array([base[s] for s in sess]) - 0.42, 0.02, 0.98)
    h0 = (rng.random(n) < prob).astype(int)
    df = pd.DataFrame({"F1": f1, "SESSION": sess, "h0": h0})
    gate = NaiveBayesGate().fit(df, features=["F1", "SESSION"], discrete_features=["SESSION"])
    p_combined = gate.predict({"F1": 0.7, "SESSION": 2})   # F1Q0 × 盘中（低 F1 盘中样本可能稀缺）
    p_session_only = _marginal_posterior(gate, "SESSION", 2)
    p_f1_only = _marginal_posterior(gate, "F1", gate.bucket_index("F1", 0.7))
    # 组合后验应在两个边缘后验之间（朴素贝叶斯插值），且非极端
    assert 0 < p_combined < 1
    assert min(p_session_only, p_f1_only) - 0.15 <= p_combined <= max(p_session_only, p_f1_only) + 0.15


def _marginal_posterior(gate, feat: str, bucket: int) -> float:
    p0, p1 = gate.prior_h0, 1 - gate.prior_h0
    if feat == "F1":
        p0 *= gate.like["F1"][bucket, 0]
        p1 *= gate.like["F1"][bucket, 1]
    else:
        p0 *= gate.like["SESSION"][bucket, 0]
        p1 *= gate.like["SESSION"][bucket, 1]
    return p0 / (p0 + p1)


def test_posterior_monotone_in_f1():
    """③ 后验单调性：模型后验对连续特征 F1 严格单调（非联合查表）。"""
    rng = np.random.default_rng(17)
    n = 4000
    f1 = rng.lognormal(0, 0.5, n)
    sess = rng.integers(0, 3, n)
    prob = np.clip(0.35 + 0.7 * (f1 - 1.2) + np.array([{0: 0.2, 1: 0.45, 2: 0.6}[s] for s in sess]) - 0.42, 0.01, 0.99)
    h0 = (rng.random(n) < prob).astype(int)
    df = pd.DataFrame({"F1": f1, "SESSION": sess, "h0": h0})
    gate = NaiveBayesGate().fit(df, features=["F1", "SESSION"], discrete_features=["SESSION"])
    mids = [0.8, 0.95, 1.1, 1.3, 1.6]
    for s in range(3):
        ps = [gate.predict({"F1": m, "SESSION": s}) for m in mids]
        assert ps == sorted(ps), f"时段 {s} 后验非单调: {ps}"


def test_gate_zone_monotone_and_fail_closed():
    """红黄绿映射：后验阈值单调分离；缺失输入 fail-closed red。"""
    from nanobot_quant.environment.gate import zone_from_prob, GateConfig
    cfg = GateConfig()   # yellow_min=0.20, red_min=0.45
    assert zone_from_prob(0.10, cfg) == "green"
    assert zone_from_prob(0.30, cfg) == "yellow"
    assert zone_from_prob(0.60, cfg) == "red"
    assert zone_from_prob(None, cfg) == "red"
    assert zone_from_prob(float("nan"), cfg) == "red"


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


def test_zone_prob_basic():
    cfg = GateConfig()   # yellow_min=0.20, red_min=0.45（融合模型样本外校准）
    assert zone_from_prob(0.10, cfg) == "green"
    assert zone_from_prob(0.30, cfg) == "yellow"
    assert zone_from_prob(0.60, cfg) == "red"
    assert zone_from_prob(None, cfg) == "red"


def test_gate_config_roundtrip():
    cfg = GateConfig()
    cfg2 = GateConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert cfg2.prob == cfg.prob
    assert cfg2.lookup == cfg.lookup
