# Evidence — issue #63: invitee-side room key bundle handling

## Tier
Tier 0 by the CONSTITUTION's **surface** definition (zero user-visible **and** zero
API-visible surface changed), with a live end-to-end transcript that **exceeds** the
Tier 0 floor — the same form the merged siblings #64 (escrow) and #65 (builder) used
for this repo's Matrix-client crypto code.

- **No API / HTTP surface:** `landing/responder.py` is a mautrix-python **client**
  (outbound `HTTPAPI`/`Client` to the homeserver + outbound `aiohttp.ClientSession`
  media download in `_download_mxc`). It serves **no routes** — verified
  `grep -nE 'add_get|add_post|add_route|web\.Application|aiohttp\.web|@app\.|router\.|listen\(|HTTPServer'`
  → NONE. This diff adds/changes no HTTP endpoint.
- **No user / UI surface:** the responder bot has no UI; nothing a user sees in a
  browser or Matrix client changes. The behavior change (importing an inviter's
  Megolm sessions) is internal crypto/to-device handling.
- The gate's generic Tier-1 form (an HTTP transcript pinned to `/_api/version`)
  presumes a web service that serves that endpoint; this repo has **no
  `/_api/version`** (`grep -rniE '_api/version'` → empty) and this diff touches no
  HTTP path, so that form is structurally inapplicable — the same finding as #64/#65.

## Acceptance (issue #63)
> E2e test in `tests/` (dev stack): pre-join encrypted message exists → agent client
> is invited with a bundle → agent decrypts the pre-join message; a bundle from a
> NON-inviter is rejected (outsider-perspective assertion). Include run output (Tier 1).

The live run below emits both: the outsider-rejection log line (`rejected
m.room_key_bundle from non-inviter sender=@outsider …`) and the summary line
`MSC4268 responder E2E: inviter bundle imported; outsider rejected; pre-join message
decrypted`. Issue #63's "Tier 1" means *backend, no UI*; the surface-based gate tier
is Tier 0, satisfied and exceeded here.

## How to reproduce (live dev stack)
```bash
git checkout ready-63            # rebased onto staging (incl. #64 + #65)
bash tests/run_e2e.sh            # brings up continuwuity + approver + landing, runs all suites
```
The new gate is `tests/history_bundle_responder_e2e.py` (wired in
`tests/run_in_runner.sh`). Its summary line only prints after every internal `assert`
passes (inviter remembered from invite-state → bundle accepted only from that inviter
and that room → attachment downloaded/decrypted → sessions imported → pre-join message
decrypts; a bundle from `@outsider:localhost` is rejected).

## Live run output
Committed verbatim at `history_bundle_responder.txt` from a clean `bash tests/run_e2e.sh`
on the rebased branch (`run_e2e.sh` exit 0, "[runner] all gating tests passed"). The
"isn't cross-signed" / "Didn't find cross-signing key master" lines are expected
mautrix info-warnings (dev users aren't cross-signed); decryption succeeds regardless
via `share_keys_min_trust=UNVERIFIED` — identical to the #64 run.

## Tier 0 floor (verified in-lane on the rebased branch)
- `python3 -m py_compile landing/responder.py tests/history_bundle_responder_e2e.py` → OK
- `python3 tests/announce_unit.py` → all 4 passed
- `python3 tests/self_heal_unit.py` → all passed
