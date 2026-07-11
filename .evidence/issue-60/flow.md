# Evidence — issue #60 (escrow durability) — Tier 1

Issue acceptance: "A test in tests/ showing: bot client receives an encrypted
message -> simulate re-mint (run the actual export/wipe/import functions with
a NEW device_id + fresh store, not a hand-rolled imitation) -> bot still
decrypts the old message. Tier 1: test transcript is the evidence."

## Test
`tests/escrow_durability.py` — runs the ACTUAL `approver.export_inbound_sessions`,
`approver._wipe_crypto_store`, and `approver.import_inbound_sessions` against a
fresh store opened under a NEW device_id (re-mint via /login), proving the
re-minted bot decrypts a pre-wipe message.

## How to run (dev stack)
```
cd dev && docker compose up -d && python3 bootstrap.py   # activates dev-token
docker run --rm --network host -e DEV_REG_TOKEN=dev-token \
  -v "$PWD:/repo" -w /repo $(docker build -q -f tests/Dockerfile tests/) \
  python3 tests/escrow_durability.py
```

## Result
Transcript: `escrow_durability.txt` — 8/8 checks passed, including:
- without escrow, wiped store cannot decrypt (SessionNotFound — the prod bug)
- re-mint logs the bot in under a NEW device_id (BOT_A -> BOT_B)
- **re-minted bot (new device) decrypts the pre-wipe message**

Tier 1: no user-visible surface; the test transcript is the evidence.
