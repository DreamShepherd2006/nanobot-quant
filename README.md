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

基于 [CAG](https://github.com/DreamShepherd2006/cloud-agent-gateway) + [nanobot-legion](https://github.com/DreamShepherd2006/nanobot-legion) 的 Squad 多智能体量化交易系统。

## 架构

```
用户（微信 / Telegram / Web）
         │
        CAG
         │
  Neo（Coordinator）
         │
  ┌──────┼──────┐
  │      │      │
Data  Research  Quant
Agent  Agent   Agent
  │      │      │
  └──────┼──────┘
         │
  Risk Engine + Portfolio Engine
         │
     Lumibot → Broker
```

## Agent 分工

| Agent | 角色 | 说明 |
|:---|:---|:---|
| **neo** | Coordinator | 调度、用户交互、全局决策 |
| **data** | Data Agent | 统一数据源（yfinance → 缓存） |
| **research** | Research Agent | AI 研究/新闻/宏观 → Hypothesis |
| **quant** | Quant Agent | demark 择时 + 多因子打分 → Signal |
| **risk** | Risk Engine | 仓位/VaR/止损 硬门控（纯 Python） |
| **portfolio** | Portfolio Engine | 资产配置/再平衡（纯 Python） |

## 核心原则

- **AI 不下单**：AI 输出 confidence + reason，下单由确定性代码执行
- **Agent 间传 JSON**：Signal Schema 统一协议，模型可互换
- **fail-closed**：Risk/Portfolio 异常 → 拒绝所有订单

## 技术栈

| 组件 | 用途 |
|:---|:---|
| [demark-patterns](https://github.com/ggoni/demark-patterns) | TD Sequential 择时 |
| [Lumibot](https://github.com/Lumiwealth/lumibot) | 回测/模拟/实盘执行 |
| [yfinance](https://github.com/ranaroussi/yfinance) | 行情数据 |
| [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | AI 研究（Phase A） |

## 使用

打开空间 → OAuth 登录 → 侧边栏「系统配置」对话中添加 Agent → 通过微信/Telegram/Web 下达指令：

- "扫描今天 A 股的买入信号"
- "回测 AAPL TD9 策略近 6 个月"
- "查看当前持仓和风险敞口"

## 相关

- [量化 Agent 完整方案](https://github.com/DreamShepherd2006/nanobot-legion) — 设计文档
- [cloud-agent-gateway](https://github.com/DreamShepherd2006/cloud-agent-gateway) — 框架底层
- [nanobot-legion](https://github.com/DreamShepherd2006/nanobot-legion) — Squad 多智能体部署

## 许可证

MIT
