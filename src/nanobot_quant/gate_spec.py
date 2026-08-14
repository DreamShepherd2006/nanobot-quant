"""Gate.io credential spec — registers with credential_registry for WebUI management."""

from .credential_registry import CredentialSpec, FieldSpec, register

GATE_SPEC = CredentialSpec(
    name="gate",
    display="Gate.io",
    description="Gate CEX 交易凭证（执行通道=cex 时使用）。主账号 API Key 用于签名 REST 下单；子账号 Key 后续扩展",
    icon="🏛️",
    fields=[
        FieldSpec(
            "api_key",
            "主账号 API Key",
            placeholder="Gate.io → API 管理（需交易权限）",
        ),
        FieldSpec("api_secret", "主账号 API Secret"),
        FieldSpec(
            "uid",
            "主账号 UID",
            type="text",
            placeholder="Gate.io 个人中心显示（如 15119093）",
        ),
    ],
    docs_url="https://www.gate.io/myaccount/api_keys",
)

register(GATE_SPEC)
