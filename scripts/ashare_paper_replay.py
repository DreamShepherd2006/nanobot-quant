"""A 股 ETF 期权模拟账本 —— 历史数据离线回放（CLI 薄封装）。

**全部计算在** :mod:`nanobot_quant.ashare.paper_replay`：页面 ``/config/ashare``
的「历史回放」区与这个脚本调的是同一份 ``run_replay()``，选档 / 出场 / 记账
不会有第二套实现（口径分叉是本项目反复踩过的坑）。

它**不是回测引擎**：成交价用 1m close ± 1 tick 代理、按 100% 全损担保占资金，
与 QMT 侧按平台保证金跑出的净值不可直接相减。

用法::

    python3 scripts/ashare_paper_replay.py \\
        --etf-csv   data/qmt-etf-1m/run_20261127_115411/510050_SH_1m.csv \\
        --opt-dirs  data/qmt-option-1m/run_20261120_012436,\\
                    data/qmt-option-1m/run_20261126_125304_p1120 \\
        --contracts data/qmt-option-1m/meta_run_20261120_012836/contracts_all.csv \\
        --underlying 510050.SH --initial 500000 --arm both \\
        --start 2026-09-01 --end 2026-11-25

数据依赖（QMT 导出，见 skill ``qmt-option-data``）：
- ETF 1m：``stime,close,high,low,open,time,...``（时间列 CST）
- 期权 1m：``ts,code,open,high,low,close,volume,amount``（多合约同分片）
- 合约清单：``undl,code,month,type,strike,expire,mult,tick,...``
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nanobot_quant.ashare.paper_replay import main as _main   # noqa: E402


if __name__ == "__main__":
    raise SystemExit(_main())
