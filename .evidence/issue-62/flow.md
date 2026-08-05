# Evidence — issue #62: MSC4268 send m.room_key_bundle on vetted invites

## Tier
Tier 0 by the CONSTITUTION's **surface** definition (no *new* user-visible or
API-visible surface — this diff adds no route, no page, no request/response
shape change to `/signup/api`, `/join/api`, or `/signup/api/crosssign`; it
wires an already-existing internal builder, `build_room_key_bundle()` from
#61, into invite call sites that already existed pre-#62), with a live
end-to-end transcript that **exceeds** the Tier 0 floor — the same form the
merged siblings #64 (escrow), #65 (builder), and #67 (invitee-side) used for
this repo's Matrix-client crypto code.

## Acceptance (issue #62, verbatim)
> E2e test in `tests/` (dev stack): existing member sends an encrypted
> message → new user is taken through a bot invite path → invitee client
> receives + olm-decrypts `m.room_key_bundle` from the bot, downloads/imports
> the bundle, and decrypts the pre-invite message. Plus: the endorsement
> JSONL contains the edge. Include run output (Tier 1). Element interop
> explicitly out of scope here (operator will test manually).

The live run in `history_bundle_invite.txt` emits 8/8 `[PASS]` lines covering
every clause above, ending with the summary `[invite-e2e] 8/8 checks passed`.

## What this PR wires (approver.py)
- `_room_history_shareable(room_id)` / `_send_room_key_bundle(mxid, room_id)`
  — gate bundle-sending on the room actually being E2EE with
  `history_visibility` shared/world_readable, then build (via #61's
  `build_room_key_bundle`) and olm-send the bundle to every device of the
  invitee, using `_ROOM_KEY_BUNDLE_CLIENT` (the SAME identity/OlmMachine
  that `sync_loop` uses for the main bot — required because MSC4268 only
  trusts a bundle from the room's actual inviter).
- `record_endorsement(...)` / `_endorser_for_code(...)` — append the
  `(endorser, invitee, code_or_manual, room_id, ts)` web-of-trust edge to
  `ENDORSEMENTS_PATH` (`/data/endorsements.jsonl`), same append-only JSONL
  pattern as the existing `audit()`. `cmd_mint` now records `minted_by` so
  code-based endorsers resolve to the code's minter.
- Call sites wired: `_invite_to_children` (bundle + endorsement on a fresh
  200 invite; used by BOTH the vetting and lobby promotion flows),
  `_promote` (endorsement + bundle hook, no-op today since the space isn't
  E2EE), `signup_handler`'s space invite (same), and
  `_lobby_invite_to_space` (endorsement ONLY — see below).

## Why `_lobby_invite_to_space` does NOT get bundle wiring
That invite goes out as the dedicated lobby/onboarding bot (`LOBBY_AUTH`), a
different Matrix identity from the main bot whose OlmMachine
`_send_room_key_bundle` uses. MSC4268 requires the bundle sender to be the
SAME identity that issued the `m.room.member` invite — sending a bundle from
the main bot's crypto for an invite issued by a different mxid would be
correctly rejected by the invitee's own inviter-check (chip #63), so it's
not wired here. The endorsement edge for that invite still IS recorded — the
web-of-trust log is about the invite, not bundle delivery. The E2EE
child-room invites that immediately follow in both the vetting and lobby
flows go through `_invite_to_children`, always issued by the main bot's
identity, and those DO get bundles — verified in the transcript below.

## How to reproduce (live dev stack)
```bash
git checkout ready-62            # rebased onto staging (#59/#64/#65/#67 already merged)
cd dev && docker compose up -d && python3 bootstrap.py
cd .. && docker build -t sr-test-runner -f tests/Dockerfile .
docker run --rm --network dev_default \
  -e DEV_HS=http://<continuwuity-container>:6167 -e DEV_REG_TOKEN=dev-token \
  -v "$(pwd):/repo" -w /repo \
  sr-test-runner python3 tests/history_bundle_invite_e2e.py
```
The new gate is `tests/history_bundle_invite_e2e.py`, wired into
`tests/run_in_runner.sh` right after `history_bundle_responder_e2e.py`, so
`bash tests/run_e2e.sh` runs it as part of the normal PR gate.

## Regression coverage
This diff touches shared functions other chips' gating tests already
exercise. `tests/vetting_e2e.py` (22/22) and `tests/lobby_e2e.py` (27/27)
both passed unchanged against the dev stack with this diff applied — see
`history_bundle_invite.txt` for the note on how they were run (the full
`run_e2e.sh` wrapper hit a pre-existing CRLF/`pipefail` issue specific to
this Windows checkout, unrelated to this diff — worked around by running the
two affected suites directly against a live approver.py + landing nginx).

## Review-fix pass
A first-draft-diff review flagged three things, all fixed before this PR
went up (see `history_bundle_invite.txt` for detail and the re-verified
transcript):
1. `_lobby_invite_to_space`'s endorser fallback was a literal `"bot"`
   string, inconsistent with `_invite_to_children`/`_promote`'s `OUR_MXID`
   fallback — changed to match. Grepped the file for other such literals;
   none found.
2. Audited every `_promote()` call site to confirm `client` is always the
   same identity as the module-level `_ROOM_KEY_BUNDLE_CLIENT` that
   `_send_room_key_bundle` actually uses — it is (the only call site is
   `process_vetting_room`, invoked from `sync_loop` with `sync_loop`'s own
   `client`). Added a docstring note making that assumption explicit rather
   than leaving it implicit.
3. Confirmed `entry.get("inviter")` in `signup_handler`'s endorsement call
   reads a real, pre-existing field (`_mint_welcome_signup_code` sets it,
   unrelated to this PR) — not dead code, left as-is.

All three tests (`history_bundle_invite_e2e.py`, `vetting_e2e.py`,
`lobby_e2e.py`) re-run clean after these fixes.
