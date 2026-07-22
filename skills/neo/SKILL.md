---
name: quant-signal-request
description: Send a batch of stock tickers to the Quant Agent for TD Sequential analysis and review the returned signals.
---

# Quant Signal Request

## Workflow

### 1. Send Request

When a user asks for stock analysis, relay a `SignalRequest` JSON to the quant agent:

```json
{
  "request_id": "<uuid-short>",
  "tickers": ["AAPL", "GOOGL", "NVDA"],
  "period": "6mo"
}
```

Use the squad relay with `sender="neo"` and `target="quant"`.

### 2. Review Response

Quant returns a `SignalResponse`:

```json
{
  "request_id": "<same>",
  "generated_at": "2026-07-22T16:30:00",
  "signals": [...]
}
```

Review each signal:
- 🟢 **BUY** signals: verify setup_buy=9 or cd_buy=13 + score > 3
- 🔴 **SELL** signals: verify setup_sell=9 or cd_sell=13
- No strong signal: tell user to hold/monitor

### 3. Report to User

Summarize findings in a table:

| Ticker | Signal | Confidence | Score | Price |
|:---|:---|:---|:---:|:---|
| AAPL | 🟢 BUY | Setup Complete | 6.5 | $150.25 |
| GOOGL | 🔴 SELL | Overbought | 2.1 | $185.30 |
| NVDA | ⚪ HOLD | — | 4.0 | $120.80 |

**Do not** forward raw JSON to the user — always translate to readable summary.

## Important

- You are the decision layer: Quant provides data, **you** decide whether to act on it
- Quant's `recommendation` is a technical signal, not a trading order
- Always consider risk context before approving any BUY/SELL action
