"""Broker registry — unified construction of execution brokers.

设计（docs/quant-system.md §19，2026-08-17 定稿）：

- 执行通道按**大类**分层：``dex``（链上地址 + TEE 签名）与 ``cex``
  （交易所 UID + API key）。当前实例：``okx_dex``（OnchainOSBroker，
  OKX DEX/onchainos）与 ``gate``（CexBroker，Gate.io）。
- 通道值即 broker spec 实例名（方案 C，2026-08-17）：``spec_for_channel``
  直接按名解析，family 由 spec 表达；旧值 dex/cex 经
  ``normalize_execution_channel`` 自动迁移。td_live、pipeline live 分支
  等全部从这条路径取，加新执行所 = 注册一个 spec + EXECUTION_CHANNELS/
  enum_groups 加一项，上层（TD 信号、执行链）零分叉。
- 未知通道 fail-closed（KeyError），绝不静默回退到别所下单。
- ``family`` 注入策略层 ``parameters.channel_family``——批次逻辑按大类
  分叉（dex：wallet_switch 子钱包；cex：子账号 key），不用 isinstance。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from nanobot_quant.exec_params import normalize_execution_channel


class BrokerSpec:
    """An execution broker (dex / cex family).

    ``create_broker(**kw)`` builds a ready-to-use broker instance; accepted
    keyword args are the common execution knobs (``tokens_json``,
    ``slippage``, ``sol_buffer_pct``, ``data_source``) plus any provider
    specific extras — providers ignore what they don't consume.
    """

    def __init__(
        self,
        name: str,
        display: str,
        family: str,
        exchange: Optional[str] = None,
        quote: str = "USDC",
        data_source: Optional[str] = None,
        create_broker: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.name = name
        self.display = display
        self.family = family          # "dex" | "cex"
        self.exchange = exchange      # "okx" | "gate" | None
        self.quote = quote            # broker 计价货币（USDC / USDT）
        self.data_source = data_source  # 数据源注册名（onchainos / gate_cex）
        self._create_broker = create_broker

    def create_broker(self, **kw: Any) -> Any:
        """Build the broker instance (provider factories lazy-import)."""
        if self._create_broker is None:
            raise NotImplementedError(f"{self.name}: create_broker 未实现")
        return self._create_broker(**kw)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BrokerSpec {self.name} [{self.family}]>"


REGISTRY: dict[str, BrokerSpec] = {}


def register(spec: BrokerSpec) -> BrokerSpec:
    """Register a broker (idempotent by name)."""
    REGISTRY[spec.name] = spec
    return spec


def get_broker(name: str) -> BrokerSpec:
    if name not in REGISTRY:
        raise KeyError("未知 broker %r（可选：%s）"
                       % (name, ", ".join(REGISTRY)))
    return REGISTRY[name]


def list_brokers() -> list[str]:
    return list(REGISTRY)


# ── 工厂（延迟 import：brokers 依赖 lumibot，测试容器无 lumibot）───────

def _create_gate(*, tokens_json: Optional[list[dict]] = None,
                 slippage: str = "0.01",
                 data_source: Any = None, **kw: Any) -> Any:
    from nanobot_quant.brokers.cex_broker import CexBroker
    from nanobot_quant.data.cex_data_source import CexDataSource

    tokens = tokens_json or []
    return CexBroker(
        tokens_json=tokens,
        slippage=str(slippage),
        data_source=data_source or CexDataSource(tokens_json=tokens),
    )


def _create_okx_dex(*, tokens_json: Optional[list[dict]] = None,
                    slippage: str = "0.01",
                    sol_buffer_pct: float = 0.0,
                    data_source: Any = None, **kw: Any) -> Any:
    from nanobot_quant.brokers.onchainos_broker import OnchainOSBroker
    from nanobot_quant.data.onchainos_data_source import OnchainOSDataSource

    tokens = tokens_json or []
    return OnchainOSBroker(
        tokens_json=tokens,
        slippage=str(slippage),
        sol_buffer_pct=float(sol_buffer_pct),
        data_source=data_source or OnchainOSDataSource(tokens_json=tokens),
    )


register(BrokerSpec(
    name="gate",
    display="Gate CEX",
    family="cex",
    exchange="gate",
    quote="USDT",
    data_source="gate_cex",
    create_broker=_create_gate,
))
register(BrokerSpec(
    name="okx_dex",
    display="OKX DEX（链上）",
    family="dex",
    exchange="okx",
    quote="USDC",
    data_source="onchainos",
    create_broker=_create_okx_dex,
))


# execution_channel（实例名）→ broker spec：直接按名解析，结构绑定退化（方案 C，2026-08-17）。
# 未来新增执行所（如 OKX CEX）：注册一个 BrokerSpec + EXECUTION_CHANNELS/enum_groups 加一项即可，
# 上层（td_live / pipeline / 策略层）零改动。旧值 dex/cex 经 normalize_execution_channel 自动迁移。


def spec_for_channel(channel: str) -> BrokerSpec:
    """Resolve the broker spec bound to an execution channel (instance name).

    Legacy ``dex``/``cex`` values are normalized first. Raises KeyError for
    unknown channels — fail-closed, never falls back to another exchange's
    broker.
    """
    name = normalize_execution_channel(channel)
    if name not in REGISTRY:
        raise KeyError("执行通道 %r 未绑定 broker（可选：%s）"
                       % (channel, ", ".join(REGISTRY)))
    return get_broker(name)


def broker_for_channel(channel: str, **kw: Any) -> Any:
    """Build the broker bound to an execution channel (fail-closed)."""
    return spec_for_channel(channel).create_broker(**kw)
