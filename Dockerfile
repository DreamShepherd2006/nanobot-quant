FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# ── 1. System: Node 20 + git ──────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git bubblewrap openssh-client gcc g++ python3-dev && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── 2a. Quant: lumibot first (to pin deps before nanobot) ──
# ibapi (Interactive Brokers API) needs gcc/g++ to build from source.
# lumibot pulls: yfinance, pandas, matplotlib, scipy, polars, plotly, etc.
RUN echo "[bust=8]" && pip install --break-system-packages \
        git+https://github.com/DreamShepherd2006/lumibot.git@651a394f \
    && echo "✅ lumibot @651a394f (upstream dev — Asset 支持显式期权乘数；美股期权默认 100 不变，OKX 加密期权一张 = 0.1 SOL / 0.01 BTC·ETH·XAU)"

# 验证 lumibot 期权乘数补丁在运行时生效（构建期断言，失败即构建失败）
# 美股期权不传 multiplier → 100（与改动前逐字节等价）；加密期权显式传入 → 生效
RUN python3 -c "\
from datetime import date; \
from lumibot.entities import Asset; \
_eq = Asset(symbol='SPY', asset_type='option', strike=400, expiration=date(2026, 1, 16), right='CALL'); \
_crypto = Asset(symbol='SOL', asset_type='option', strike=94, expiration=date(2026, 9, 18), right='PUT', multiplier=0.1); \
_stock = Asset(symbol='AAPL'); \
assert _eq.multiplier == 100, f'equity option default broken: {_eq.multiplier}'; \
assert _crypto.multiplier == 0.1, f'crypto option multiplier broken: {_crypto.multiplier}'; \
assert _stock.multiplier == 1, f'stock default broken: {_stock.multiplier}'; \
print('[LUMIBOT-MULTIPLIER-OK] equity_default=100 crypto_explicit=0.1 stock_default=1')"

# ── 2b. nanobot (force-reinstall to override lumibot conflicts) ──
# lumibot downgrades pypdf→6.14.2 and websockets→15.0.1;
# --force-reinstall restores nanobot's version constraints.
ENV NANOBOT_SKIP_WEBUI_BUILD=1
RUN pip install --break-system-packages --force-reinstall \
        git+https://github.com/DreamShepherd2006/nanobot.git@dbdb146f \
    && echo "✅ nanobot @dbdb146f"

# ── 3. CAG + channel patches ──────────────────────────────
RUN pip install --break-system-packages \
        git+https://github.com/DreamShepherd2006/cloud-agent-gateway.git@v0.2.0 \
    && python3 -m cloud_agent_gateway.deploy.cloud.patch_qq_reload \
    && python3 -m cloud_agent_gateway.deploy.cloud.patch_feishu_reload \
    && python3 -m cloud_agent_gateway.deploy.cloud.patch_dingtalk_reload \
    && python3 -m cloud_agent_gateway.deploy.cloud.patch_weixin_reload \
    && echo "✅ CAG v0.2.0"

# ── 4. nanobot-legion: patches + webui source + assets ───
RUN echo "[bust=48]" && pip install --break-system-packages \
        git+https://github.com/DreamShepherd2006/nanobot-legion.git@d66f7912 \
    && python3 -m nanobot_legion.install \
    && echo "✅ nanobot-legion @d66f7912 (upstream staging — PR #51 业务管理 chat 分组重排, bust=48)"

# ── 4b. Build Legion webui from source ────────────────────
RUN cd /app/legion_webui_src \
    && npm install && npm run build \
    && mkdir -p /app/legion_webui \
    && cp -r /app/nanobot/web/dist/* /app/legion_webui/ \
    && rm -rf /app/nanobot/web/dist \
    && rm -rf /app/legion_webui_src \
    && echo "✅ legion webui built"

# ── 5. WhatsApp bridge ────────────────────────────────────
RUN NANOBOT_DIR=$(python3 -c "import nanobot, os; print(os.path.dirname(nanobot.__file__))") \
    && cp -r "$NANOBOT_DIR/bridge" /app/bridge \
    && cd /app/bridge \
    && git config --global --add url."https://github.com/".insteadOf ssh://git@github.com/ \
    && git config --global --add url."https://github.com/".insteadOf git@github.com: \
    && npm install && npm run build \
    && cd /app && rm -rf /app/bridge/node_modules \
    && echo "✅ whatsapp bridge"

# ── 5b. OnchainOS (OKX) CLI — MCP server for crypto market data ──
# Mirror of OKX official v4.3.1.
# SHA256: 31214c9bdeff283df66493c6391a01ddd57c67ee5167c8fd8f7db949e632e773
RUN ONCHAINOS_VERSION="v4.3.1" \
    && ONCHAINOS_CHECKSUM="31214c9bdeff283df66493c6391a01ddd57c67ee5167c8fd8f7db949e632e773" \
    && ONCHAINOS_URL="https://github.com/DreamShepherd2006/onchainos-skills/releases/download/${ONCHAINOS_VERSION}/onchainos-x86_64-unknown-linux-gnu" \
    && curl -sSL "${ONCHAINOS_URL}" -o /usr/local/bin/onchainos \
    && echo "${ONCHAINOS_CHECKSUM}  /usr/local/bin/onchainos" | sha256sum -c - \
    && chmod +x /usr/local/bin/onchainos \
    && echo "✅ onchainos ${ONCHAINOS_VERSION}"

# ── 6. nanobot-quant + Vibe-Trading (Research Agent) ──
RUN echo "[bust=592]" && pip install --break-system-packages \
        'mcp<2' \
        git+https://github.com/DreamShepherd2006/nanobot-quant.git@735c970 \
        git+https://github.com/DreamShepherd2006/Vibe-Trading.git@v0.1.12 \
    && echo "✅ nanobot-quant @735c970 (upstream main — 期权回测数据层改走官方归档成交 + IV 曲面重定价, PR #360; bust=592) + vibe-trading @v0.1.12"

# ── 6b. Patch Vibe-Trading: create artifact parent dirs ──
# backtest engines/base.py writes validation.json without mkdir,
# causing FileNotFoundError on first swarm run.
RUN python3 -c "import backtest,os; p=os.path.join(os.path.dirname(backtest.__file__),'engines','base.py'); c=open(p).read(); c=c.replace(\"v_path.write_text\",\"v_path.parent.mkdir(parents=True,exist_ok=True)\\n            v_path.write_text\"); open(p,'w').write(c); print('patched backtest/engines/base.py')"

# ── 6c. Patch Vibe-Trading: inject OnchainOS enrichment into grounding ──
# Adds chain-level data (real-time price, holder distribution, token risk)
# to the grounding block that VT injects into every swarm worker's system
# prompt. OnchainOS failures are swallowed gracefully — swarm runs proceed
# with OHLCV-only + warning.
RUN python3 -m nanobot_quant.patches.patch_vt_grounding

# ── 7. Reset marker ───────────────────────────────────────
RUN echo "PURGE_OAUTH=0" > /app/reset-setup.ini

# ── 8. User + .swarm dir ──────────────────────────────────
RUN useradd -m -u 1000 -s /bin/bash nanobot \
    && mkdir -p /home/nanobot/.nanobot \
    && mkdir -p /usr/local/lib/python3.12/site-packages/.swarm/runs \
    && chown -R nanobot:nanobot /home/nanobot /app /usr/local/lib/python3.12/site-packages/.swarm \
    && echo "✅ .swarm dir ready for nanobot"

USER nanobot
ENV HOME=/home/nanobot \
    SQUAD_LEGION=true

EXPOSE 7860
ENTRYPOINT ["/app/entrypoint.sh"]
