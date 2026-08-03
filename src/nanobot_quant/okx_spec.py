"""OKX credential spec — registers with credential_registry for WebUI management."""

from .credential_registry import CredentialSpec, FieldSpec, register

OKX_SPEC = CredentialSpec(
    name="okx",
    display="OKX OnchainOS",
    description="OKX DEX API 凭证，用于获取链上行情数据（K线、实时价格）和交易执行",
    icon="🔑",
    fields=[
        FieldSpec("api_key", "API Key", placeholder="从 OKX 开发者中心获取"),
        FieldSpec("secret_key", "Secret Key"),
        FieldSpec("passphrase", "Passphrase"),
        FieldSpec(
            "wallet_address",
            "个人钱包地址（仅展示）",
            type="text",
            placeholder="由 API Key 绑定的 Agentic Wallet 决定，无需填写",
            required=False,
            readonly=True,
        ),
        FieldSpec(
            "chain",
            "链 (Chain)",
            type="select",
            options=["solana", "ethereum", "xlayer"],
            required=False,
        ),
    ],
    docs_url="https://www.okx.com/account/my-api",
)

register(OKX_SPEC)
