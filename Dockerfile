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
RUN echo "[bust=17]" && pip install --break-system-packages \
        git+https://github.com/DreamShepherd2006/nanobot-legion.git@v0.1.0 \
    && python3 -m nanobot_legion.install \
    && echo "✅ nanobot-legion v0.1.0"

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

# ── 6. nanobot-quant (strategies + risk + portfolio + backtest) ──
# ⚠️  TEST: pointing to DreamShepherdCD fork feat branch
RUN echo "[bust=5]" && pip install --break-system-packages \
        git+https://github.com/DreamShepherdCD/nanobot-quant.git@feat/signal-schema-and-batch \
    && echo "✅ nanobot-quant @feat/signal-schema-and-batch (DreamShepherdCD)"

# ── 7. Reset marker ───────────────────────────────────────
RUN echo "PURGE_OAUTH=0" > /app/reset-setup.ini

# ── 8. User ───────────────────────────────────────────────
RUN useradd -m -u 1000 -s /bin/bash nanobot \
    && mkdir -p /home/nanobot/.nanobot \
    && chown -R nanobot:nanobot /home/nanobot /app

USER nanobot
ENV HOME=/home/nanobot \
    SQUAD_LEGION=true

EXPOSE 7860
ENTRYPOINT ["/app/entrypoint.sh"]
