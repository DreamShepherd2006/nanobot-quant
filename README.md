---
title: nanobot-quant
emoji: 📈
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
hf_oauth: true
pinned: false
---

# 📈 nanobot-quant — AI 量化交易 Agent

[![Release](https://img.shields.io/badge/release-v1.0.0-blue)](https://github.com/DreamShepherd2006/nanobot-quant/releases/tag/v1.0.0)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

基于 [CAG](https://github.com/DreamShepherd2006/cloud-agent-gateway) + [nanobot-legion](https://github.com/DreamShepherd2006/nanobot-legion) 的多智能体量化交易系统。

交易决策全部由确定性 Python 完成（信号 → 风控 → 下单 → 记账 → 对账），LLM 只做编排；回测与实盘共用同一套决策代码。执行通道为 OKX CEX（现货与期权）。

设计概要见 [DESIGN.md](DESIGN.md)。

## 架构

```
用户（微信 / Telegram / Matrix / WebUI）
                 │
                CAG（OAuth · 通道 · 持久卷）
                 │
             gatekeeper ── neo（编排 · 路由）
                              │
              ┌───────────────┴───────────────┐
              │                               │
            quant                        vt_research
      （策略 · 执行 · 回测）          （Swarm 研究）
              │
    ┌─────────┼──────────┐
TD live 循环  分析 pipeline  回测 driver
（自主运行）  （风控 → 执行）  （与实盘同函数）
    └─────────┴──────────┘
              │
    brokers（现货 / 期权） · 数据源注册表
```

## 能力概览

- **现货 TD live**：多场景独立周期与资金池，TD 信号多道闸门过滤后建仓；出场按止盈止损 / 衰竭信号 / 日界规则分层；批次台账 + 启动对账，支持实时监控、交易记录、参数快照与手动平仓。
- **期权资金链**：卖 put 现金担保 → 到期结算 → 自动补买 → covered call，全链路 fail-closed；配套期权链、盘口深度模拟、到期判定与台账。
- **回测与研究**：逐 bar 重放（与实盘同一策略函数）、规则预设可复现、对外指标汇总与 markdown 报告；Vibe-Trading Swarm 与各类传感器研究默认只读诊断，不进入交易路径。

## 安全原则

- AI 不下单：执行链是确定性代码，AI 只输出意图
- 实盘双保险：agent 意图与页面开关同时满足才真正下单，否则强制 dry-run
- 关键开关（循环、自动化、实盘）一律由用户在页面手动开启
- 新增机制默认关闭，回测裁决后才启用
- 取价 / 余额 / 订单状态异常一律 fail-closed 并留痕
- 不加杠杆；期权仅做现金担保的卖方

## 技术栈

| 组件 | 用途 |
|:---|:---|
| [Lumibot](https://github.com/Lumiwealth/lumibot) | 策略执行引擎（fork，含加密期权修复） |
| TD Sequential | 自研纯 Python 实现（多口径变体） |
| [cloud-agent-gateway](https://github.com/DreamShepherd2006/cloud-agent-gateway) | 平台抽象 / OAuth / relay |
| [nanobot-legion](https://github.com/DreamShepherd2006/nanobot-legion) | Squad 多智能体部署层 |
| [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | Swarm 研究（MCP） |
| gate-api · python-okx | 交易所官方 SDK |

## 使用

1. 从仓库 `main` 取整份 `Dockerfile` 上传到空间 → Factory Rebuild
2. OAuth 登录 → 「系统配置」完成首次 setup
3. 到执行参数页勾选场景并「启动」→ 保存（**重建后不会自动恢复，需手动点一次**）

WebUI 侧入口：执行参数 · TD 分析 · 回测 · 期权 · 代币管理 · 凭证 / 账户。

## 版本

当前 **v1.0.0**（[Release](https://github.com/DreamShepherd2006/nanobot-quant/releases/tag/v1.0.0)）。版本语义：patch = 修 bug / 文档 / 默认值；minor = 新能力不破口径；major = 破坏性变更。部署仍 pin commit hash（零漂移）。

## 文档与相关仓库

- [DESIGN.md](DESIGN.md) — 分层、决策链、安全模型与已知限制
- 完整方案与实证记录属项目内部文档，不随本仓库分发
- [cloud-agent-gateway](https://github.com/DreamShepherd2006/cloud-agent-gateway) · [nanobot-legion](https://github.com/DreamShepherd2006/nanobot-legion) · [nanobot](https://github.com/HKUDS/nanobot)

## 许可证

MIT
