# PLAN — issue #78: retention room factory (#76 chip 2)

Base: `staging`. Branch: `ready-78`. One issue, then stop.

## Goal
Bot-created "retention room": bot is sole PL100, E2EE on, `m.room.retention`
state carries the policy window, restricted-join to the space + space-child link,
a pinned plain-language policy message that does NOT overstate, and the policy is
immutable at creation (the bot's in-force record is write-once; later
`m.room.retention` changes are logged + noticed but never honored).

Chip 3 (#79) will READ this in-force record to filter keys; chip 2 only creates
the room + records the policy + refuses to honor later changes.

## Acceptance (from issue body) → checkboxes
- [ ] 1. Room created via the command (`!retention-room <name> <duration>`).
- [ ] 2. `/state` shows bot at PL100 and **no other user at ≥50**; `users_default: 0`.
- [ ] 3. `m.room.encryption` present; `m.room.retention` present with the window.
- [ ] 4. Pinned policy message exists; text matches the configured window + is honest.
- [ ] 5. Room is a space child; a space member can join without an invite.
- [ ] 6. A later `PUT` of `m.room.retention` by another PL100 user (simulated)
      does NOT change the bot's in-force policy record.

## Work items
1. `knock-approver/approver.py`:
   - `RETENTION_PATH` config (default `/data/retention_rooms.json`) + load/save.
   - `_parse_duration("7d")` → seconds (s/m/h/d/w).
   - `_render_window(seconds)` → human label.
   - `_retention_policy_text(name, window_seconds)` → pinned message (honest,
     matches epic #76 "does / does not claim").
   - `_create_retention_room(name, window_seconds)` → createRoom (bot sole PL100,
     encryption, m.room.retention, restricted join to SPACE_ID), space-child link,
     pinned policy message, write-once record. Returns dict with room_id.
   - `cmd_retention_room(client, room_id, sender, args)` → parses, calls core,
     returns reply. Register `"!retention-room"` in COMMANDS.
   - sync_loop watcher: detect `m.room.retention` changes in retention rooms →
     audit log + notice to OPERATOR_NOTIFY_ROOM; never mutate the store.
2. `tests/retention_room_e2e.py` — self-contained, import-based (precedent:
   `history_bundle_e2e.py` imports `approver` and calls the builder). Asserts 1–6.
3. `tests/run_in_runner.sh` — wire the new test into the gate.

## Verification (Tier 1 — API/backend behavior, no UI)
Run `tests/retention_room_e2e.py` against the dev continuwuity stack
(`localhost:46167`); capture transcript to `.evidence/issue-78/`. This service
has no `/_api/version` (it's a Matrix bot, not a web app) — the lane-specific
gate is the acceptance test transcript (matrix-ready-worker Step 4); I'll state
that explicitly in the PR.

## Out of scope (per epic #76)
- Enforcement / key withholding — chip 3 (#79).
- In-band transparency digests — chip 4 (#80).
- Redaction sweep — chip 5 (#81).
