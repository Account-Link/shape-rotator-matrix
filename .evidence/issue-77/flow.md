# Evidence — issue #77 (session_id → age index, #76 chip 1) — Tier 1

Issue `## Acceptance`: a test in `tests/` against the dev stack (style of
`history_e2ee_repro.py` / `escrow_durability.py`) showing the bot builds a
cleartext `room_id -> {session_id: earliest origin_server_ts}` index off
`m.room.encrypted` events as they arrive in `/sync`, keeps the min ts per
session, persists to `/data`, and survives a restart.

Tier 1: **no user-visible surface** (internal crypto-state index consumed by
later retention chips). The test transcript is the evidence — same convention
as `tests/escrow_durability.py` (the #60 test this issue cites).

## What was built
- `knock-approver/approver.py`:
  - `SESSION_INDEX_PATH` (`/data/session_age_index.json`, same `/data` trust
    domain as `bot_crypto.db` and the escrow — #60 precedent).
  - `record_session(room_id, session_id, ts)` — keeps the **minimum** ts,
    persists atomically via the existing `_load`/`_save` helpers.
  - `session_age_index(room_id) -> {session_id: earliest_ts}` — reads live
    from the persisted file (restart-survival is structural, not a special case).
  - `iter_encrypted_events(rooms_data)` — yields `(room_id, session_id,
    origin_server_ts)` for every megolm `m.room.encrypted` timeline event,
    cleartext (no decryption).
  - Hooked into `sync_loop` after `handle_sync`; a write fault logs loudly
    without killing the sync heartbeat. Missing file = first-boot `{}` (not an
    error); a corrupt file RAISES via `_load` → `json.loads` (no silent skip of
    state loss — the #60 convention).
- `tests/session_age_index.py` — the acceptance test.

## How to run (dev stack)
```bash
cd dev && docker compose up -d                # continuwuity on 127.0.0.1:46167
docker run --rm --network host -v "$PWD:/repo" -w /repo \
  shape-rotator-e2e-test-runner:latest python3 tests/session_age_index.py
```

## Result
Transcript: `session_age_index.txt` — **9/9 checks passed, exit 0** (re-run on the
branch rebased onto `staging` @ `128c5c8`; isolated dev lane
`DEV_STACK_SUFFIX=-rw86 DEV_HS_PORT=46168` per #84):

- forced rotation produced **2 distinct megolm session ids** (acceptance #2)
- **every** `crypto_megolm_inbound_session` row has an index entry (acceptance #3)
- each indexed ts == the earliest `origin_server_ts` for that session,
  cross-checked against an authoritative `/messages` back-pagination (acceptance #4)
- a **fresh Python process** re-imports `approver` against the persisted file and
  re-reads the same index → survives a restart (acceptance #5)
- the persisted `/data/session_age_index.json` exists on disk (size=190)

Note: the `RuntimeError: database pool has been stopped` traceback printed
*after* the `9/9 checks passed` line is mautrix teardown noise (the test closes
`bot_db` then the background syncer handler fires once more). It occurs after
all assertions and the `exit 0`; it is not a test failure.

## Out of scope (per issue)
Backfill of existing rooms, and any *use* of the index — those are retention
epic chips 3 and 4.
