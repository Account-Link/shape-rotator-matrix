# Issue #61 — MSC4268 build + upload the room key bundle

## Acceptance (from issue #61, verbatim)
> Test in `tests/` against the dev stack: bot holding >=2 sessions for a room
> (own + another sender's) builds and uploads the bundle; a fresh client
> downloads it, decrypts the attachment, imports the sessions, and decrypts a
> pre-join message from EACH sender. Include run output in the PR (Tier 1).

The issue labels this "Tier 1" meaning *backend, no UI*. This repo serves no
`/_api/version` endpoint (the bot's HTTP surface is `/signup/api`,
`/signup/api/crosssign`, `/join/api`, `/health`), and this diff wires the new
function to no served route, so the gate's generic Tier-1 form (an HTTP
transcript pinned to `/_api/version`) is structurally inapplicable — same
finding as the merged sibling PR #64. The PR is therefore classified **Tier 0
— no user-visible or API-visible surface** (CONSTITUTION surface definition),
with the dev-stack round-trip below exceeding the Tier 0 floor. See the PR
body for the full justification.

## How to reproduce the round-trip (needs the dev stack)
The test runs inside the test-runner container against the dev continuwuity
stack, exactly like the other E2E tests, and is wired into the gating runner
by this PR (tests/run_in_runner.sh):

    # bring up the dev stack (conduwuity + knock-approver + landing nginx)
    cd dev && docker compose up -d --build
    # the runner boots the stack, waits for approver /health, then runs:
    DEV_HS="$HS" DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
      python3 tests/history_bundle_e2e.py

Expected stdout on success (single line; all asserts gate it):
    MSC4268 bundle round-trip: 2 sessions exported, uploaded, downloaded, imported, decrypted

## What this rework pass verified vs. what needs the dev stack
- Verified in-lane (this pass): `py_compile` of approver.py +
  history_bundle_e2e.py; `announce_unit.py` + `self_heal_unit.py` all pass;
  `build_room_key_bundle` has no production caller (Tier 0 surface claim).
- NOT re-run in this lane (no live homeserver provisioned): the live
  media-upload + fresh-client decrypt round-trip. Its prior real dev-stack
  output is recorded verbatim in `history_bundle.txt`.
