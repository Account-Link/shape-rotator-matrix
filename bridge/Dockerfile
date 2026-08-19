# bridge/Dockerfile — the Matrix<->Telegram relay as a dstack pod tenant.
#
# Deployed to a tee-daemon pod with runtime "image" (see webhost-apps
# DEVELOPER_GUIDE.md "Image runtime"), NOT as a handler: the relay is a
# long-running daemon, not a request/response app.
#
# All mutable state lives under /data, which MUST be backed by a named volume.
# `ctx.dataDir` survives restarts but not CVM redeploys; a named volume is
# adopted idempotently, so crypto.db and the relay cursors persist. Losing
# /data means a re-minted device and a lost megolm session.
#
# Credentials arrive as env vars, never baked into the image:
#   TELEGRAM_BOT_TOKEN     BotFather token (the ONLY Telegram credential)
#   MATRIX_BRIDGE_USER     e.g. shape-bridge
#   MATRIX_BRIDGE_PASSWORD used once to mint creds.json into /data on first boot
#   MATRIX_HOMESERVER      e.g. https://mtrx.shaperotator.xyz
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libolm3 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        aiohttp \
        'mautrix[e2be]' \
        aiosqlite \
        asyncpg \
        python-olm

WORKDIR /app
COPY mx.py tg.py relay.py /app/bridge/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Point every path at the persistent volume. These are the same env overrides
# the CLIs already honour, so no code is container-specific.
ENV MX_CREDS=/data/creds.json \
    MX_STORE=/data/store \
    MX_FIXTURES=/data/test-fixtures.json \
    MX_RELAY_CURSOR=/data/store/relay_next_batch \
    TELEPORT_DIR=/data \
    TEST_FIXTURES_PATH=/data/test-fixtures.json \
    RELAY_STATE=/data/relay-state.json \
    TELEGRAM_BOT_TOKEN_PATH=/data/telegram-bot-token \
    HEALTH_PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "bridge/relay.py"]
