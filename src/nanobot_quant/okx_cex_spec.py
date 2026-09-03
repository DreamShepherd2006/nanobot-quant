"""OKX CEX credential spec — registers with credential_registry for WebUI management.

NOTE: this is the OKX **exchange (CEX)** account credential (trading sub-account),
completely independent from ``okx`` (OKX OnchainOS / Web3 market-data credential).
Stored at ``credentials/okx_cex.json`` (never merged with ``okx.json``).
"""

from .credential_registry import CredentialSpec, FieldSpec, register

OKX_CEX_SPEC = CredentialSpec(
    name="okx_cex",
    display="OKX CEX（期权/合约）",
    description=(
        "OKX 交易所实盘子账户凭证，用于期权（卖 put/平仓）等 CEX 交易。"
        "与 okx（OnchainOS）凭证相互独立，请勿混填。"
    ),
    icon="🔑",
    fields=[
        FieldSpec("api_key", "API Key", placeholder="OKX 子账户 API Key"),
        FieldSpec("secret_key", "Secret Key"),
        FieldSpec("passphrase", "Passphrase", placeholder="创建 Key 时设置的密码"),
    ],
    docs_url="https://www.okx.com/account/my-api",
)

register(OKX_CEX_SPEC)
