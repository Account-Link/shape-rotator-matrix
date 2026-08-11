# Evidence — issue #78: retention room factory (#76 chip 2)

Tier: **1 (API / backend behavior change, no user-visible UI).**

The Shape Rotator approver is a Matrix bot, not a web app, so it has no
`/_api/version` endpoint. Per `specs/matrix-ready-worker.md` Step 4, the lane's
verification is the acceptance test transcript against the dev continuwuity
stack. This file maps the transcript (`transcript.txt`) to the issue's
`## Acceptance` criteria and states honestly what was and was not verified.

## What ran

```
DEV_HS=http://localhost:46167 DEV_REG_TOKEN=dev-token \
  python3 tests/retention_room_e2e.py
```

Against the dev continuwuity stack (`dev-continuwuity-1`, server name
`localhost:46167`, continuwuity reporting up to `v1.18` — see
`devstack-versions.txt`). Base commit: `d496460` (origin/staging).

Result: **21/21 PASS** (full output in `transcript.txt`).

## Acceptance criteria → evidence

| # | Criterion (from issue body) | Evidence (check name in transcript) |
|---|---|---|
| 1 | Room created via the command/endpoint | `1a` (factory core) + `1b` (`!retention-room` command wrapper creates a room + replies), `1c`/`1d` (arg/duration parse errors rejected) |
| 2 | `/state`: bot PL100, no other user ≥50, `users_default: 0` | `2a/2b/2c/2d` — `ge50={'@bot':100}`, `users_default=0` |
| 3 | `m.room.encryption` + `m.room.retention` (window) present | `3a` (`m.megolm.v1.aes-sha2`), `3b` (`max_lifetime=604800000` ms = 7d) |
| 4 | Pinned policy message; text matches window; honest | `4a` (one pinned), `4b` (window `1w` + `604800s` in text), `4c` (states "DOES NOT … deletion"), `4d` (== bot `_retention_policy_text`) |
| 5 | Space child; a space member joins without invite | `5a` (`m.space.child` via), `5b` (join_rule `restricted` to space), `5c` (member joined, status 200) |
| 6 | Later `PUT` of `m.room.retention` by another PL100 (simulated) does not change bot's in-force policy | `6a` (elevated attacker to PL100), `6b` (PUT accepted), `6c` (**room state DID change** to `86400000` ms = 1d, proving the PUT landed), `6d` (**bot in-force store UNCHANGED**: `window_seconds=604800`, `max_lifetime_ms=604800000`) |

## The immutability proof (criterion 6), in words

The test simulates "another PL100 user" by inviting + joining an attacker and
elevating them to PL100 via the bot token (test scaffolding — a real retention
room has no other PL100 by construction). The attacker PUTs `m.room.retention`
to `86400000` ms (1d). The homeserver accepts it and the **room state changes**
(`6c` = `86400000`). But the bot's in-force policy — the write-once record in
`RETENTION_PATH` that chip 3 (#79) will read — is **unchanged** at 7d
(`6d` = `604800`s / `604800000` ms). That is the contract: the room's state
event is mutable on the wire; the bot's enforced policy is not.

## What I could NOT verify (honest)

- **The running-bot tamper notice** (`_detect_retention_tamper`, the "log and
  post a notice if one is attempted" work item) is implemented and wired into
  `sync_loop`, but it fires inside the live `/sync` loop of a running approver.
  The import-based test proves the stronger property directly — the store is
  immutable regardless of room state — so it does not drive the running loop.
  Confirming the in-room notice lands is a follow-up for a live-stack run.
- **`history_bundle_e2e.py` fails on this box's host Python** with
  `share_keys() missing 1 required positional argument: 'current_otk_count'`.
  This is **pre-existing** mautrix version drift (reproduces on base `staging`
  at `18e3cd5`) and is unrelated to this change — the test-runner container
  pins its own mautrix. Not introduced here; `approver` imports cleanly.
- **Prod deploy / prod retention room**: out of scope (chip 2 is the factory;
  enforcement is chip 3, undeployed). The retention room is intentionally inert
  until chip 3 lands (per epic #76).
