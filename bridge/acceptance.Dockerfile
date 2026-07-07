# bridge/acceptance.Dockerfile — runner image for bridge/acceptance.sh.
#
# bridge/acceptance.sh is the live round-trip gate for the Matrix<->Telegram
# relay. It drives bridge/mx.py, bridge/tg.py and bridge/relay.py against the
# single fixture pair in ~/.teleport-travel/test-fixtures.json. Those three
# need an image that has BOTH:
#
#   - the E2EE stack from tests/Dockerfile (libolm3 + mautrix[e2be] +
#     python-olm + aiosqlite + asyncpg), and
#   - the relay's two extra deps (telethon + python-dotenv) documented in
#     bridge/README.md ("Dependencies").
#
# tests/Dockerfile is the minimal E2EE-gate image and intentionally ships
# neither telethon nor python-dotenv (the E2EE tests don't talk to Telegram).
# Rather than duplicate the whole base here, acceptance.sh builds
# tests/Dockerfile first (tag `shape-bridge-runner`) and this image layers the
# two Telethon deps on top — so a libolm/mautrix bump in tests/Dockerfile flows
# in automatically and there is exactly one source of truth for the crypto
# stack.
#
# Source is bind-mounted at /repo at runtime; nothing is COPY'd here, so a code
# change is a fast container restart, not a rebuild.
FROM shape-bridge-runner
RUN pip install --no-cache-dir telethon python-dotenv
