"""朴素贝叶斯闸门：分桶直方图似然 + 静态模型（训练段拟合，不滚动重估）。

融合环境传感器（F1 波动率状态 + 美股交易时段；预留更多特征）：
  P(H0 | features)  H0 = 未来 3h（1h×3 根）内回撤 > 0.5%（被套）

训练流程（调用方）：
  1. 拉训练段 K 线 → sensors.sensor_frame() 算特征 + 时段编码
  2. make_h0_label() 算 H0 标签
  3. NaiveBayesGate.fit(df[features + ["h0"]], discrete_features=[...]) → save()
实盘流程：
  4. load() → predict({"F1": ..., "SESSION": ...}) → P(H0)，经 gate.py 映射红/黄/绿
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

H0_LOOKBACK = 3     # 1h × 3 = 3h（与 F1 传感器实验口径一致）
H0_DRAWDOWN = 0.005  # 3h 内回撤 > 0.5% 视为被套
DEFAULT_FEATURES = ["F1"]


def make_h0_label(df: pd.DataFrame, lookback: int = H0_LOOKBACK,
                  threshold: float = H0_DRAWDOWN) -> pd.Series:
    """H0 标签：未来 lookback 根内最低价较当前收盘回撤超过 threshold。

    返回与 df 等长的 Series（尾部 lookback 根无未来数据为 NaN）。
    输入需含 Close 列（大写）。
    """
    close = df["Close"]
    # 未来 lookback 根最低价：反转滚动取「当前位置及之后 N-1 根」min，
    # 反转回原序再 shift(-1) 排除当前根 → min(close[t+1..t+lookback])
    fmin = close[::-1].rolling(lookback, min_periods=lookback).min()[::-1].shift(-1)
    h0 = (fmin / close - 1) < -threshold
    # NaN 与阈值比较恒为 False，会吞掉窗口不足的尾部——必须显式保留 NaN
    h0 = h0.where(fmin.notna())
    return h0.astype(float)


def session_code(ts_utc) -> pd.Series:
    """美股时段编码（纽约时区）：0=周末, 1=盘前/盘后, 2=盘中(9:30-16:00)。

    输入为 tz-aware UTC DatetimeIndex 或 Series。被套率单调 0<1<2。
    """
    s = ts_utc if isinstance(ts_utc, pd.Series) else ts_utc.to_series()
    et = s.dt.tz_convert("America/New_York")
    wd = et.dt.weekday
    tm = et.dt.time
    open_t, close_t = pd.Timestamp("09:30").time(), pd.Timestamp("16:00").time()
    code = pd.Series(1, index=s.index)
    code[wd >= 5] = 0
    code[(wd < 5) & (tm >= open_t) & (tm <= close_t)] = 2
    return code


@dataclass
class NaiveBayesGate:
    """分桶朴素贝叶斯。feature_edges 存每个特征的分位边界，like 存似然表。

    支持单特征（当前 F1）、多特征融合（features 列表）与离散特征
    （discrete_features 列表：直接按整数值作桶索引，不做分位）。
    """

    features: list = field(default_factory=lambda: list(DEFAULT_FEATURES))
    discrete_features: list = field(default_factory=list)
    n_buckets: int = 5
    feature_edges: dict = field(default_factory=dict)   # {"F1": [...]}
    like: dict = field(default_factory=dict)            # {"F1": np.ndarray[b,2]}
    prior_h0: float = 0.5
    fitted: bool = False

    # ── 拟合 ──
    def fit(self, df: pd.DataFrame, features: list | None = None,
            discrete_features: list | None = None,
            n_buckets: int | None = None) -> "NaiveBayesGate":
        """df 需含特征列 + h0 列（h0 ∈ {0,1}，可含 NaN 尾部）。"""
        if features is not None:
            self.features = list(features)
        else:
            # 未显式指定：拟合 df 中除 h0 外的数值列（排除 sym 等字符串元数据列）
            self.features = [c for c in df.columns if c != "h0"
                             and pd.api.types.is_numeric_dtype(df[c])]
        if discrete_features is not None:
            self.discrete_features = list(discrete_features)
        if n_buckets is not None:
            self.n_buckets = n_buckets
        cols = self.features + ["h0"]
        d = df.dropna(subset=cols).copy()
        if len(d) < 200:
            raise ValueError(f"训练样本不足: {len(d)} < 200")
        d["h0"] = d["h0"].astype(int)
        n_h0 = d["h0"].sum()
        n_h1 = len(d) - n_h0
        if n_h0 < 50 or n_h1 < 50:
            raise ValueError(f"正负样本不均衡: H0={n_h0} H1={n_h1}")
        self.prior_h0 = n_h0 / len(d)
        for feat in self.features:
            if feat in self.discrete_features:
                # 离散特征：每个唯一值一桶，用「值在升序唯一列表中的位置」作桶索引
                # （值域可稀疏，如只有 0/2——位置索引 0/1，绝不用值本身作索引）
                vals = sorted(d[feat].unique())
                self.feature_edges[feat] = vals
                nb = len(vals)
                tbl = np.zeros((nb, 2))
                pos = {int(v): i for i, v in enumerate(vals)}
                for v in vals:
                    sel = d[d[feat] == v]
                    tbl[pos[int(v)], 0] = sel["h0"].sum()
                    tbl[pos[int(v)], 1] = len(sel) - sel["h0"].sum()
                tbl += 1.0  # 拉普拉斯平滑（防 0 概率）
                tbl[:, 0] /= tbl[:, 0].sum()
                tbl[:, 1] /= tbl[:, 1].sum()
                self.like[feat] = tbl
                continue
            bounds = sorted({e.left for e in pd.qcut(d[feat], self.n_buckets, duplicates="drop").cat.categories}
                            | {e.right for e in pd.qcut(d[feat], self.n_buckets, duplicates="drop").cat.categories})
            self.feature_edges[feat] = bounds
            nb = len(bounds) - 1
            tbl = np.zeros((nb, 2))
            for b in range(nb):
                if b == nb - 1:
                    sel = d[(d[feat] >= bounds[b]) & (d[feat] <= bounds[b + 1])]
                else:
                    sel = d[(d[feat] >= bounds[b]) & (d[feat] < bounds[b + 1])]
                tbl[b, 0] = sel["h0"].sum()          # H0 计数
                tbl[b, 1] = len(sel) - sel["h0"].sum()  # H1 计数
            tbl += 1.0  # 拉普拉斯平滑（防 0 概率）
            tbl[:, 0] /= tbl[:, 0].sum()
            tbl[:, 1] /= tbl[:, 1].sum()
            self.like[feat] = tbl
        self.fitted = True
        return self

    # ── 预测 ──
    def bucket_index(self, feat: str, value: float) -> int:
        vals = self.feature_edges[feat]
        if feat in self.discrete_features:
            # 离散：值在升序唯一列表中的位置；未知值钳制到最近邻
            try:
                return [int(v) for v in vals].index(int(value))
            except ValueError:
                if value <= vals[0]:
                    return 0
                if value >= vals[-1]:
                    return len(vals) - 1
                for i in range(1, len(vals)):
                    if vals[i - 1] < value < vals[i]:
                        return i - 1 if value - vals[i - 1] <= vals[i] - value else i
                return len(vals) - 1
        nb = len(vals) - 1
        if value <= vals[0]:
            return 0
        if value >= vals[-1]:
            return nb - 1
        for b in range(nb):
            if vals[b] <= value < vals[b + 1]:
                return b
        return nb - 1

    def predict(self, values: dict) -> float:
        """P(H0 | features) 朴素贝叶斯后验。values: {"F1": float, "SESSION": int, ...}。"""
        if not self.fitted:
            raise RuntimeError("NaiveBayesGate 未拟合")
        p0 = self.prior_h0
        p1 = 1 - self.prior_h0
        for feat in self.features:
            b = self.bucket_index(feat, values[feat])
            p0 *= self.like[feat][b, 0]
            p1 *= self.like[feat][b, 1]
        return float(p0 / (p0 + p1))

    def predict_frame(self, df: pd.DataFrame) -> pd.Series:
        """对含特征列的 DataFrame 批量预测（行内特征缺失 → NaN）。"""
        out = pd.Series(np.nan, index=df.index)
        valid = df[self.features].notna().all(axis=1)
        out[valid] = [self.predict({f: getattr(r, f) for f in self.features})
                      for r in df[valid].itertuples()]
        return out

    # ── 持久化 ──
    def to_dict(self) -> dict:
        return {
            "features": list(self.features),
            "discrete_features": list(self.discrete_features),
            "n_buckets": self.n_buckets,
            "feature_edges": {k: [float(v) for v in vals] for k, vals in self.feature_edges.items()},
            "like": {k: v.tolist() for k, v in self.like.items()},
            "prior_h0": self.prior_h0,
            "fitted": self.fitted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NaiveBayesGate":
        return cls(
            features=list(data.get("features", DEFAULT_FEATURES)),
            discrete_features=list(data.get("discrete_features", [])),
            n_buckets=data["n_buckets"],
            feature_edges={k: list(v) for k, v in data["feature_edges"].items()},
            like={k: np.array(v) for k, v in data["like"].items()},
            prior_h0=data["prior_h0"],
            fitted=data["fitted"],
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "NaiveBayesGate":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
