"""闸门配置：红/黄/绿三区映射。

推荐模式：贝叶斯后验（zone_from_prob）——特征独立乘积结构免疫
「缺失格子/零概率/非单调」三类落地坑：
  · 拉普拉斯平滑在 NaiveBayesGate.fit() 内（tbl += 1.0），似然表无零
  · 缺失组合由边际似然乘积自动回退，无需联合计数
  · 后验对连续特征严格单调（各桶似然比单调）

查表模式（zone_from_lookup_f1sess）仅作诊断/对照：
  对 (F1 桶 × 时段) 联合频率表查红黄绿；格子缺失时回退到边缘频率
  （先时段边缘、后 F1 边缘，防零概率）。生产不依赖查表。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

ZONES = ("green", "yellow", "red")

# 后验阈值（按融合模型样本外校准，先验 P(H0)≈29.7%）：
#   green  < 0.20   低风险（样本外最低组 3~8%）
#   yellow 0.20~0.45 中风险
#   red    >= 0.45  高风险（样本外最高组 52~57%）
DEFAULT_PROB_CFG = {
    "yellow_min": 0.20,
    "red_min": 0.45,
}

# 查表模式：按训练模型的分桶边界/似然比网格定义（诊断用）
DEFAULT_LOOKUP_CFG = {
    "f1_buckets": [0.86, 0.92, 0.95, 0.99, 1.06],   # F1 分位边界（4 内界）
    "session_red": {2: 0.45, 1: 0.45, 0: 0.40},      # 各时段红阈（后验网格近似）
}


@dataclass
class GateConfig:
    """红黄绿映射配置（JSON 可持久化）。"""

    lookup: dict = field(default_factory=lambda: dict(DEFAULT_LOOKUP_CFG))
    prob: dict = field(default_factory=lambda: dict(DEFAULT_PROB_CFG))

    def to_dict(self) -> dict:
        return {"lookup": dict(self.lookup), "prob": dict(self.prob)}

    @classmethod
    def from_dict(cls, data: dict) -> "GateConfig":
        cfg = cls()
        cfg.lookup.update(data.get("lookup", {}))
        # JSON roundtrip 会把 session_red 的 int 键变字符串——转回 int
        sr = cfg.lookup.get("session_red")
        if isinstance(sr, dict):
            cfg.lookup["session_red"] = {int(k): v for k, v in sr.items()}
        cfg.prob.update(data.get("prob", {}))
        return cfg


def zone_from_prob(p: Optional[float], cfg: GateConfig | None = None) -> str:
    """贝叶斯后验三区（生产主模式）。概率缺失 → 保守返回 red（fail-closed）。"""
    cfg = cfg or GateConfig()
    if p is None or pd_isna(p):
        return "red"
    if p >= cfg.prob["red_min"]:
        return "red"
    if p >= cfg.prob["yellow_min"]:
        return "yellow"
    return "green"


def _edge_rate(like_h0: float, like_h1: float, prior: float) -> float:
    """由似然比 + 先验算边缘后验（查表回退用）。"""
    p0 = prior * like_h0
    p1 = (1 - prior) * like_h1
    return p0 / (p0 + p1)


def zone_from_lookup_f1sess(f1: Optional[float], session: Optional[int],
                            gate, cfg: GateConfig | None = None) -> str:
    """查表模式（诊断/对照）：按 F1 桶 × 时段查模型后验网格。

    缺失组合回退顺序：时段边缘（该时段 F1 未知）→ F1 边缘 → 先验。
    任一输入缺失 → red（fail-closed）。gate 为已拟合 NaiveBayesGate。
    """
    cfg = cfg or GateConfig()
    if f1 is None or session is None or pd_isna(f1) or pd_isna(session):
        return "red"
    b = gate.bucket_index("F1", f1)
    s = gate.bucket_index("SESSION", session)
    p = _cell_posterior(gate, b, s)
    return zone_from_prob(p, cfg)


def _cell_posterior(gate, f1_bucket: int, session: int) -> float:
    """后验；若某特征的该桶在训练集中无样本（平滑后仍为纯先验），
    回退到另一特征的边缘后验（由边缘似然计算）。"""
    prior = gate.prior_h0
    p0, p1 = prior, 1 - prior
    f1_ok = f1_bucket < len(gate.like["F1"]) and gate.like["F1"][f1_bucket].sum() > 0
    s_ok = session < len(gate.like["SESSION"]) and gate.like["SESSION"][session].sum() > 0
    if f1_ok:
        p0 *= gate.like["F1"][f1_bucket, 0]
        p1 *= gate.like["F1"][f1_bucket, 1]
    if s_ok:
        p0 *= gate.like["SESSION"][session, 0]
        p1 *= gate.like["SESSION"][session, 1]
    if p0 + p1 == 0:
        return prior
    return p0 / (p0 + p1)


def pd_isna(v) -> bool:
    try:
        import math
        if isinstance(v, float):
            return math.isnan(v)
    except Exception:
        pass
    return v is None
