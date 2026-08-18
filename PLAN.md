# PLAN — issue #77: session_id → age index (#76 chip 1)

Derived from the issue's `## Acceptance`. Base: `staging`. Branch: `ready-77`.

## Goal
The bot must be able to tell how old a megolm session is. Build a cleartext
index `room_id -> {session_id: earliest_origin_server_ts}` off `m.room.encrypted`
events as they arrive in `/sync` (no decryption needed — `session_id` and
`origin_server_ts` are cleartext). Persist to `/data`; survive restart; raise on
read failure (no silent skip — issue #60 convention).

## Work items (from the issue)
- [x] `record_session(room_id, session_id, ts)` keeping the **minimum** ts, and
      `session_age_index(room_id) -> {session_id: earliest_ts}` — added to
      `knock-approver/approver.py` (matches the existing monolithic layout: the
      #60 escrow helpers live here too).
- [x] `iter_encrypted_events(rooms_data)` generator (style of
      `iter_knock_events`) + hook into `sync_loop` after `handle_sync`, where
      `m.room.encrypted` events are present.
- [x] Persist to `/data/session_age_index.json` via the atomic `_load`/`_save`
      helpers; the disk file is the source of truth on every call, so restart-
      survival is structural, not a special case. Missing file = first-boot `{}`;
      corrupt file propagates (raises) — no silent skip.

## Acceptance (restate) + how each is verified
A test in `tests/` against the dev stack (style of `history_e2ee_repro.py` /
`escrow_durability.py`) → `tests/session_age_index.py`.

1. Bot creates a room and is present from event 0.
2. Send messages, force at least one megolm rotation so >1 session exists.
   → force rotation by `await alice.crypto.crypto_store
     .remove_outbound_group_sessions([room_id])` before the 2nd send (mautrix
     then mints a new outbound session_id on the next encrypt).
3. Every session present in `crypto_megolm_inbound_session` has an index entry.
   → query `SELECT session_id FROM crypto_megolm_inbound_session WHERE
     account_id=bot AND room_id=room AND withheld_code IS NULL`, assert each is
     in `session_age_index(room_id)`.
4. Each indexed timestamp equals the `origin_server_ts` of the earliest event
   that used that session.
   → back-paginate `/messages`, group `m.room.encrypted` by `session_id`,
     take `min(origin_server_ts)`, assert == indexed value for every session.
5. Index survives a restart of the bot.
   → fresh Python subprocess imports `approver` against the same persisted file
     and re-reads `session_age_index(room_id)`; compare to in-process result.

Evidence: transcript captured to `.evidence/issue-77/` (Tier 1 — no user-visible
surface; matches the `escrow_durability.py` precedent cited by the issue via #60).

## Out of scope (per issue)
Backfill of existing rooms. Any *use* of the index — that's chips 3 and 4.
