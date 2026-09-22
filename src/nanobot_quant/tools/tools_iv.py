"""``analyze_iv_leadlag`` — IV 领先-滞后诊断（只读研究工具）。

研究背景（2026-09-21 ~ 09-22，见 docs/quant-system.md §33.37）
------------------------------------------------------------
要回答的问题：**crypto 的 IV 是不是反应迟钝**——即是否存在
``realized → implied`` 的方向性（与 SPX 上普遍观察到的
``implied → realized`` 相反）。

* **IV 序列来源**：OKX 期权**归档成交**（逐笔，官方滚动保留约 30 天）→
  反解 IV（虚值侧优先 + ``max_iv`` / ``max_abs_delta`` 质量门）→
  近 ATM 中位数（``atm_median``）或恒定 tenor 微笑插值（``smile``）。
* **对照组**：现货已实现波动（EWMA）、F1 波动率状态、Deribit 官方 DVOL
  （长历史外部指数；**只有 BTC/ETH 有官方指数**，其余家族借 BTC 作市场基准，
  报告里会显式标注 —— 否则读者会误以为那是本标的的对照）。
* **显著性**：**循环平移零假设**（不假设 iid）—— 重叠窗口 + 强自相关下
  朴素 t 检验会虚抬显著性。

**已校准实证（2026-09-22，14 天归档，500 次平移）**：BTC / ETH 在 5m / 15m /
1H 三个桶下，「已实现波动 → IV」峰值相关只有 0.01–0.12、p = 0.11–0.91。
**这不是「没有方向性」，而是「归档太稀测不出来」**：BTC 15m 只有 20% 的桶
有成交，其余靠前向填充，IV 被打成阶梯常数。同一份数据上「已实现波动 ↔ F1」
显著（0.27–0.29，p ≈ 0.01）—— 说明**工具本身有功效**，测不到的偏偏是 IV。

**结论与前提**：本工具现在只能给出「测不出来」的结论，因为 OKX 归档的成交
密度不够。要真正测 IV 的领先-滞后，前置条件是 ``option_tape`` 采集器
（bulk ticker 每 60s 落盘 bid/ask/markVol，一天一文件）先积累样本 ——
A（采集）是 B（分析）的前提，不是锦上添花。

口径与局限
----------
* 前向填充**人为增加 IV 的平滑度、压低其领先能力**（对 H1 是保守方向）。
* 已实现波动来自**现货**、IV 来自**期权盘口**：同源标的、不同市场层。
* ``dvol_currency`` 留空 = 按家族推导；显式指定时会标注是否为借用。

工具职责
--------
给定家族（如 ``SOL-USD_UM``）+ 天数 + 桶，跑 H1（已实现波动 → IV）、
H2（F1 → IV）、H3（已实现波动 vs DVOL）三组领先-滞后检验，返回各 lag 的
相关系数与循环平移 p 值，并附 markdown 报告（``markdown`` 字段）。

**只读**：仅拉公开归档 / K 线 / DVOL，不触碰任何交易路径。归档缓存在
``{data_root}/legion/backtests/opt_data/``（Factory Rebuild 不丢）：**首次
运行要下载归档（约 30–60 秒，可能触及 MCP 30s 超时）**，之后每次约 6 秒。
"""

from __future__ import annotations

import sys
from typing import Optional

from nanobot_quant.analysis import iv_leadlag as _ill


def _log(msg: str) -> None:
    """诊断一律走 stderr（MCP stdio 通道不能被污染）。"""
    print(f"[IV-LL] {msg}", file=sys.stderr, flush=True)


def analyze_iv_leadlag(family: str = "SOL-USD_UM", days: int = 14,
                       bucket: str = "15m", window: int = 12,
                       max_lag: int = 6, target_tenor_days: float = 3.0,
                       mode: str = "atm_median", band_pct: float = 10.0,
                       include_dvol: bool = True, dvol_currency: str = "",
                       n_iter: int = 500) -> dict:
    """跑一次 IV 领先-滞后诊断（只读）。

    Args:
        family: 期权家族，如 ``SOL-USD_UM`` / ``BTC-USD_UM`` / ``ETH-USD_UM``。
        days: 回看天数（归档滚动保留约 30 天，默认 14）。
        bucket: 桶粒度，取 ``1m/3m/5m/15m/30m/1H/2H/4H/1D``。
        window: 已实现波动的 EWMA 窗口（bar 数，默认 12）。
        max_lag: 最大 lag（两个方向各测，默认 6）。
        target_tenor_days: ``smile`` 模式下插值的目标剩余期限（天）。
        mode: ``atm_median``（默认，近 ATM 中位数，桶覆盖率高）或
            ``smile``（恒定 tenor 微笑插值，口径干净但更稀疏）。
        band_pct: ATM 带宽（%，仅 ``atm_median`` 生效）。
        include_dvol: 是否拉 Deribit DVOL 作外部对照。
        dvol_currency: DVOL 币种；留空 = 按家族推导（BTC/ETH 用自己，
            其余借 BTC 并在报告里标注「不是本标的自己的指数」）。
        n_iter: 循环平移次数（默认 500，上限 2000）。

    Returns:
        dict：``ok`` / ``family`` / ``bucket`` / ``series``（IV 覆盖度等）/
        ``tests``（H1/H2/H3 各 lag 的 corr 与 p 值）/ ``dvol`` / ``notes`` /
        ``markdown``。``ok=False`` 时含 ``error``。
    """
    fam = str(family or "").strip() or "SOL-USD_UM"
    try:
        res = _ill.analyze_iv_leadlag(
            fam,
            days=max(1, min(int(days), 30)),
            bucket=str(bucket or "15m"),
            window=max(2, int(window)),
            max_lag=max(1, min(int(max_lag), 24)),
            target_tenor_days=float(target_tenor_days),
            mode=str(mode or "atm_median"),
            band_pct=float(band_pct),
            include_dvol=bool(include_dvol),
            dvol_currency=(str(dvol_currency).strip() or None),
            n_iter=max(50, min(int(n_iter), 2000)),
            progress=_log,
        )
    except Exception as exc:  # 取数/参数异常必须显式返回，静默不可接受
        _log(f"分析失败：{type(exc).__name__}: {exc}")
        return {"ok": False, "family": fam, "bucket": str(bucket),
                "error": f"{type(exc).__name__}: {exc}"}

    if not isinstance(res, dict):
        return {"ok": False, "family": fam, "error": "分析返回了非 dict 结果"}
    if res.get("ok") and not res.get("markdown"):
        try:
            res["markdown"] = _ill.markdown(res)
        except Exception as exc:  # markdown 只是展示层，失败不掩盖结果
            _log(f"markdown 渲染失败：{type(exc).__name__}: {exc}")
    _log(f"完成 family={fam} bucket={res.get('bucket')} ok={res.get('ok')}")
    return res
