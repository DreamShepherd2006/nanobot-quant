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
        FieldSpec("wallet_address", "钱包地址 (Wallet Address)", type="text", placeholder="Solana: E71V4... 或 EVM: 0x..."),
        FieldSpec("chain", "链 (Chain)", type="text", placeholder="solana (默认)", required=False),
    ],
    docs_url="https://www.okx.com/account/my-api",
)

register(OKX_SPEC)
