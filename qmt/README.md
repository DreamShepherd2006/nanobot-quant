# qmt —— A 股 ETF 期权卖方策略（券商端）

跑在**券商 QMT（君弘君智投研端）内置 Python** 里的单文件策略：用 ETF 现货算 TD 低9，卖虚值 put，
权利金回落止盈买平，到期 ITM 走自然行权接实股。与云端 `src/nanobot_quant/ashare/`（期权盘口
采集器 / 分析）是同一条 A 股线，但运行环境完全不同，所以单独成目录。

## 为什么不在 `src/` 里

| 约束 | 云端（HF Space） | QMT 券商端 |
|---|---|---|
| Python | 3.12 | **3.6.8** |
| 编码 | UTF-8 | 策略文件必须 **GBK** |
| 依赖 | pandas / numpy / 官方 SDK | 只有 xtquant 内置模块，无第三方库 |
| 网络 | 有 | 无（且 `socket` 被白名单拦） |
| 文件 I/O | 自由 | **只允许新建**：目标路径已存在 ⇒ `PermissionError: Forbidden FileIO` |
| 界面 | WebUI | 无 GUI/页面 API，只能 `print` |

因此 `qmt/` 不进 pip 包（`pyproject.toml` 的 `packages.find where=["src"]`），Docker 构建与空间运行时
都不会碰到它。

## 目录

```
qmt/
  ashare_put_v5_4.py         当前版本（UTF-8，可编辑/可 review）
  deliver/ashare_put_v5_4.py 交付版本（GBK + CRLF，整份粘进 QMT 的就是它）
  build_deliver.py           一键从当前版本生成交付版本
  archive/                   历史版本 v1 … v5_3（只读，供与旧日志对照 / 复盘）
  probes/                    QMT 侧只读探针（环境/期权链/1m 取数/回测盘口）
  selftest/                  逐版本本地自测（不依赖 QMT，断言式 OK/FAIL）
  CHANGELOG.md               逐版变更
```

## 交付流程

```bash
python3 qmt/build_deliver.py ashare_put_v5_4      # 生成 deliver（GBK+CRLF，校验可 compile）
python3 qmt/selftest/selftest_v5_4.py             # 本地自测，必须全绿
```

`build_deliver.py` 会打印 `bytes / crlf / build` 三个数字，selftest 断言 `crlf == 行数`、`bare_lf == 0`、
GBK 可 `compile`。交付时**只粘 `deliver/` 那份**——UTF-8 版本粘进 QMT 会中文乱码甚至 SyntaxError。

QMT 交易机通常不能上外网，所以 **GitHub 只作审计与回溯，不作交付通道**：交付仍是「生成 GBK 文件 →
粘进 QMT 策略编辑器」。

## 变更纪律（两档）

| 改动类型 | 流程 |
|---|---|
| 探针、日志、展示层、文档、自测 | 可直接进 `main` |
| **策略行为改动**（入场 / 出场 / 闸门 / 仓位 / 记账口径） | 走 `feat/qmt-*` 分支 → PR → 由用户合并，PR 内**必须贴一版同参数回测对比**（改前 / 改后 净盈亏、开平笔数、闸门拦截统计） |

理由：策略改动只有回测证据能证明它按设计工作；日志/探针改坏了不影响钱。

## 版本标识

- 文件名带版本：`ashare_put_v5_4.py`（改脚本**不覆盖**旧版，新版另存新文件，旧版移入 `archive/`）
- 代码里 `BUILD = '20261122-1610'` / `VERSION_LABEL = 'v5_4'`，INIT 行会打印，日志能直接对上版本
- git tag `qmt-v5_4` 指向该版本入库的 commit

## 数据不入仓

期权/ETF 1m 历史（`data/qmt-option-1m` 约 361M、`qmt-etf-1m` 约 24M、`qmt-ashare-cache` 约 64M）
不进 git：本地数据在持久卷，每次导出的区间/行数/异常日统计见各 run 目录下的 `EXPORT_REPORT.txt`。
探针与导出脚本进仓（`probes/`），这样「数据从哪来」是可复现的。

## 已知口径（详见各版本文件头注释）

- 回测撮合：成交价 = bar close；滑点参数对期权 `passorder` 无效；保证金按平台比例严重低估
- 平台余额含义务仓市值重估，**不能当盈亏依据**；净 = 自建账本「已实现 + 浮盈亏」
- 价差在报告层用 `_spread_cost()` 并入逐笔成本（虚值/浅价 1 元/张，实值 px×10000×1.25%，上限 60 元/张）
- 手续费按张：卖开 1.20 / 买平 1.70
- 实盘双锁：界面选实盘 **且** `LIVE_CONFIRM = 1` 才发单
