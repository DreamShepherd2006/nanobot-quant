"""nanobot-quant: AI 量化交易 Squad agent 部署层."""

from .order_tracker import OrderTracker, TrackedOrder, TradeRecord
from .signal import batch_calculate
from .signal_schema import SignalRequest, SignalResponse, TickerSignal

__version__ = "0.1.0"

__all__ = [
    "batch_calculate",
    "OrderTracker",
    "SignalRequest",
    "SignalResponse",
    "TickerSignal",
    "TrackedOrder",
    "TradeRecord",
]

# ── Trigger spec registration at import time ───────────────
try:
    from . import okx_spec  # noqa: F401
except ImportError:
    pass
try:
    from . import gate_spec  # noqa: F401
except ImportError:
    pass
try:
    from . import mcp_spec  # noqa: F401
except ImportError:
    pass
