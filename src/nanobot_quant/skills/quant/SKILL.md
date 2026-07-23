# Quant Agent — TD Sequential Analysis Pipeline

## When to Call

Neo should relay to `quant` when the user:
- Asks to analyze a stock ("分析 AAPL" / "analyze NVDA")
- Requests a batch signal scan ("扫描纳斯达克前10")
- Wants a risk assessment before trading

## How to Call

Send a relay message to `target="quant"`:

```
!exec python3 -c "
from nanobot_quant.pipeline import AnalysisPipeline
import json

p = AnalysisPipeline()
results = p.run(['AAPL', 'TSLA'], period='6mo', portfolio_value=100000)
for r in results:
    print(f'{r.ticker}: {r.signal.recommendation} score={r.signal.score:.1f} risk_ok={r.risk_passed}')
    if r.suggested_order:
        print(f'  → {r.suggested_order[\"action\"]} {r.suggested_order[\"quantity\"]} @ \${r.suggested_order[\"price\"]:.2f}')
    else:
        print(f'  → BLOCKED: {r.risk_details}')
"
```

Or use the structured response format:

```
!exec python3 -c "
from nanobot_quant.pipeline import AnalysisPipeline
p = AnalysisPipeline()
resp = p.run_to_response(['AAPL'], period='1y')
print(resp.to_summary())
"
```

## Parameters

| Param | Default | Description |
|:---|:---|:---|
| `tickers` | required | List of stock symbols |
| `period` | `6mo` | yfinance data period |
| `portfolio_value` | `100000` | Hypothetical PV for sizing |
| `max_position_pct` | `0.20` | Max % of PV per position |
| `max_drawdown_pct` | `0.15` | Skip entries when DD exceeds |
| `stop_loss_pct` | `0.10` | Exit threshold |

## Output

Each `AnalysisResult` contains:
- `ticker` — symbol
- `signal` — TD Sequential result (recommendation, score, setup/cd values)
- `risk_passed` — True if all gates passed
- `risk_details` — per-gate reasons
- `suggested_order` — `{action, quantity, order_type, price, reason}` (None if blocked)
