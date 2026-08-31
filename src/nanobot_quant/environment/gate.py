"""闸门配置：红/黄/绿三区映射（查表阈值 + 贝叶斯后验两种模式）。

查表模式（默认，阈值网格实验验证）：
  绿灯 = X3<=1.5 且 F1<=1.2
  黄灯 = 1.5<X3<=2.0 或 1.2<F1<=1.5
  红灯 = X3>2.0 或 F1>1.5

贝叶斯模式：zone_from_prob(p) —— P(H0) < 0.35 绿 / 0.35-0.6 黄 / >=0.6 红
（默认阈值可在 config 中调整；红灯 = 禁止 1m 做多）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

ZONES = ("green", "yellow", "red")

DEFAULT_LOOKUP_CFG = {
    "x3_yellow": 1.5,
    "x3_red": 2.0,
    "f1_yellow": 1.2,
    "f1_red": 1.5,
}
DEFAULT_PROB_CFG = {
    "yellow_min": 0.35,
    "red_min": 0.60,
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
        cfg.prob.update(data.get("prob", {}))
        return cfg


def zone_from_lookup(x3: Optional[float], f1: Optional[float], cfg: GateConfig | None = None) -> str:
    """查表三区。任一传感器缺失（NaN/None）→ 保守返回 red（fail-closed）。"""
    cfg = cfg or GateConfig()
    if x3 is None or f1 is None or pd_isna(x3) or pd_isna(f1):
        return "red"
    if x3 > cfg.lookup["x3_red"] or f1 > cfg.lookup["f1_red"]:
        return "red"
    if x3 > cfg.lookup["x3_yellow"] or f1 > cfg.lookup["f1_yellow"]:
        return "yellow"
    return "green"


def zone_from_prob(p: Optional[float], cfg: GateConfig | None = None) -> str:
    """贝叶斯后验三区。概率缺失 → 保守返回 red（fail-closed）。"""
    cfg = cfg or GateConfig()
    if p is None or pd_isna(p):
        return "red"
    if p >= cfg.prob["red_min"]:
        return "red"
    if p >= cfg.prob["yellow_min"]:
        return "yellow"
    return "green"


def pd_isna(v) -> bool:
    try:
        import math
        if isinstance(v, float):
            return math.isnan(v)
    except Exception:
        pass
    return v is None
