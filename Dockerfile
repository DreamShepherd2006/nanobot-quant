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
RUN echo "[bust=4]" && pip install --break-system-packages \
        git+https://github.com/DreamShepherd2006/lumibot.git@v4.5.78 \
    && echo "✅ lumibot v4.5.78"

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
RUN echo "[bust=38]" && pip install --break-system-packages \
        git+https://github.com/DreamShepherd2006/nanobot-legion.git@7e0e1ba1 \
    && python3 -m nanobot_legion.install \
    && echo "✅ nanobot-legion @7e0e1ba1 (upstream staging — PR #48 CEX 账户页路由+钱包分类入口)"

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
RUN echo "[bust=132]" && pip install --break-system-packages \
        git+https://github.com/DreamShepherdCD/nanobot-quant.git@c97963e \
        git+https://github.com/DreamShepherd2006/Vibe-Trading.git@v0.1.12 \
    && echo "✅ nanobot-quant @c97963e (CD fork feat/exit-net-fee 验证中 — 场景出场净值化+止盈默认值, bust=132) + vibe-trading @v0.1.12"

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
