# Test-runner image. Carries libolm + every Python crypto dep needed by
# the E2EE tests. Source code is bind-mounted at /repo at runtime — no
# COPY here, so a code change is a fast container restart, not a rebuild.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libolm3 libolm-dev gcc curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        aiohttp \
        cryptography \
        'mautrix[e2be]' \
        asyncpg \
        aiosqlite \
        'matrix-nio[e2e]' \
        unpaddedbase64 \
        base58 \
        pyaes \
        python-olm

WORKDIR /repo
