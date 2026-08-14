# PLAN — issue #62: MSC4268: send m.room_key_bundle on vetted invites

Branch: `ready-62` (base `staging`). Tier 1 (backend/bot; no UI surface).
Depends on #61 (`build_room_key_bundle()`, merged) and #63 (invitee-side
import, merged) — this chip is purely the inviter-side wiring: call the
already-built builder after the bot's own invites and actually send the
bundle.

## Acceptance (from issue #62)
E2e test in `tests/` (dev stack): existing member sends an encrypted
message → new user is taken through a bot invite path → invitee client
receives + olm-decrypts `m.room_key_bundle` from the bot, downloads/imports
the bundle, and decrypts the pre-invite message. Plus: the endorsement
JSONL contains the edge. Run output required (Tier 1). Element interop
explicitly out of scope.

## Design notes
- MSC4268 requires the bundle sender to be the SAME identity that issued
  the `m.room.member` invite. In this codebase that means: only invites
  issued via the main bot's token (`TOKEN`/`AUTH`, the same identity as
  `sync_loop`'s mautrix `client`) can carry a bundle. `_admin_invite` (used
  by `_invite_to_children` and `signup_handler`) and `_promote` all use
  that identity — safe to wire.
- `_lobby_invite_to_space` invites as the *dedicated onboarding bot*
  (`LOBBY_AUTH`), a different identity with no OlmMachine. Sending a
  bundle from the main bot's crypto for an invite issued by a different
  mxid would be silently rejected by the invitee's inviter-check (correct
  behavior per MSC4268) — so this path does NOT get bundle wiring. It
  still records the endorsement edge for the space invite. The actual
  E2EE child-room invites in the lobby flow go through `_invite_to_children`
  right after (main-bot identity), which does get the bundle.
- Gate bundle-sending on the target room actually being E2EE with
  `history_visibility` shared/world_readable (`_room_history_shareable`) —
  a plain invite to the (non-encrypted) space is a correct no-op.
- Endorsement JSONL reuses the existing `_load`/`_save`/`audit()` file
  pattern (append-only JSONL, same shape as `LOG_PATH`) rather than a new
  storage layer. `cmd_mint` gains a `minted_by` field on newly-minted code
  entries so knock/lobby code-based endorsements can resolve the minter;
  bot-autonomous invites (no resolvable code, e.g. `INITIAL_CODES`-seeded
  entries) fall back to the bot's own mxid as endorser.

## Tasks
- [x] Read PLAN.md / STATE.md / MATRIX_ONBOARDING.md / issue #62 text.
- [x] Locate every invite call site: `_invite_to_children`, `_promote`,
      `_lobby_invite_to_space`, `_admin_invite` (signup_handler step 3).
- [x] Add `_ROOM_KEY_BUNDLE_CLIENT` global (mirrors `_ROOM_KEY_BUNDLE_STORE`),
      set in `sync_loop()`.
- [x] Add `_room_history_shareable(room_id)` (encryption + history_visibility
      check) and `_send_room_key_bundle(mxid, room_id)` (build + olm-send to
      every device of `mxid`, best-effort, never raises into the caller).
- [x] Add `ENDORSEMENTS_PATH` + `record_endorsement(endorser, invitee,
      code_or_manual, room_id)` (JSONL append, same pattern as `audit()`).
- [x] Wire `_invite_to_children`: on a fresh (200) child invite, send the
      bundle + record the endorsement; skip on 403 (already member).
- [x] Wire `_promote`: send bundle + record endorsement after a successful
      space invite (no-op in practice since the space isn't E2EE, but the
      hook is identity-correct if that ever changes).
- [x] Wire `_lobby_invite_to_space`: record endorsement only (see design
      notes — no bundle, identity mismatch).
- [x] Wire `signup_handler` step 3 (`_admin_invite(mxid, SPACE_ID)`): send
      bundle + record endorsement on success.
- [x] `cmd_mint`: persist `minted_by` on newly minted codes.
- [x] Thread `endorser` / `code_or_manual` through `process_vetting_room` and
      `process_lobby_room` call sites (resolve from the code's `minted_by`,
      fall back to the bot's own mxid).
- [x] **tests/history_bundle_invite_e2e.py**: extend the chip-#61/#63 test
      shape (`tests/history_bundle_e2e.py`, `tests/history_bundle_responder_e2e.py`)
      to go through an actual `approver.py` invite path (not a hand-built
      bundle): alice sends an encrypted message pre-invite → drive
      `_invite_to_children` (or the vetting flow it's called from) for a
      fresh invitee → invitee's `responder.py`-style client receives +
      imports the olm-encrypted `m.room_key_bundle` → decrypts the
      pre-invite message → assert the endorsement JSONL has the edge.
      8/8 checks passed against the dev stack (transcript captured).
- [x] Wired into `tests/run_in_runner.sh` (the PR gate) after
      `history_bundle_responder_e2e.py`.
- [x] Run against the dev stack (`dev/docker-compose up -d && python3
      dev/bootstrap.py`), capture transcript as PR evidence. Also ran the
      full `bash tests/run_e2e.sh` gate as a regression check (touched
      shared functions `_invite_to_children`/`_promote`/
      `_lobby_invite_to_space`/`signup_handler`/`cmd_mint` that other
      gating tests exercise).
- [x] Evidence written to `.evidence/issue-62/flow.md` +
      `.evidence/issue-62/history_bundle_invite.txt` (repo convention from
      #60/#61/#63).
- [ ] Commit (incl. transcript), push, open PR to staging. Remove `ready`
      label. (Left to the operator — see commit message / PR description
      handed back in-conversation.)

---

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
