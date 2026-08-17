# Evidence — issue #20 / PR #70: cross-signing gate on admin commands

## Tier 1 — Matrix client-server API flow (this repo serves no `/_api/version`)

Changed files are `knock-approver/approver.py` + tests + docs: the approver is a
mautrix-python **client** serving no HTTP routes, and this repo has no `/_api/version`
endpoint (`grep -rniE '_api/version'` → empty — same finding as merged #67/#88/#68).
The generic version-pin anchor is therefore substituted, exactly as the gate accepted
for #68/#88, by a **live read-only call against the deployed homeserver**
(`01-deploy-anchor.txt`: `GET /_matrix/client/versions` → HTTP 200, and the space alias
`#shape-rotator:mtrx.shaperotator.xyz` resolves to `!4FL8uL5OEYLATG1VH4wC2CD3pfIV6BMFId9VT7rmm-g`,
the same room ID recorded in #88's and #68's evidence). The behavior itself is
demonstrated end-to-end over the Matrix client-server HTTP API, encrypted, against a
real continuwuity homeserver running this branch's exact `approver.py`
(bind-mounted by `tests/docker-compose.test.yml` line 69; sha256 in `01-deploy-anchor.txt`).

## What was still missing before this pass, and is now covered

The PR's local verification proved the **refusal** side only (unit 4/4;
`tests/admin_e2ee.py` 7/7 — unverified device refused). Its own docstring says:
"A separate signed-in operator flow is required to prove the positive path."
`02-admin-room-flow.txt` is that flow — **16/16 pass** — driven by
`admin_room_flow.py` (this directory), as a signed-in operator whose device is
genuinely cross-signed (MSK/SSK/USK bootstrapped by the repo's own Paste-B endpoint
`POST /signup/api/crosssign`, the device then signed by the SSK):

| # | Flow step (all in the E2EE admin room, over HTTP) | Result |
|---|---|---|
| 1 | cleartext `!mint` (raw HTTP, no encryption) from the operator | **refused**, no code minted |
| 2 | operator cross-signed via `/signup/api/crosssign` | msk bootstrapped, device signed |
| 3 | **encrypted `!mint` from the cross-signed operator — the positive path** | **ACCEPTED: `minted knock code → …/join?code=…`, no refusal** (first attempt; no retry needed) |
| 4 | encrypted `!mint` from an unverified third party **with PL 100** | **refused**, no code minted (PL is not considered before the trust gate) |
| 5 | re-cross-sign the operator (fresh MSK — **rotated master**) then encrypted `!mint` | **refused, addressed to the operator**, no code minted — fails closed |

`03-approver-log.txt` is the approver's own log for the same run: `[admin] refused …
unverified device` for 1/4/5, `[admin] dispatch !mint … is_admin=True` + the minted-code
reply for 3, and `[crosssign ok]` lines showing the two distinct MSKs (original, then
rotated).

Notes recorded honestly from the transcript:
- Rotation (step 5) required passing the operator's password: continuwuity UIA-gates
  *replacing* existing cross-signing keys. The approver's own error names this
  (`homeserver requires UIA; pass password to /crosssign`); first-time cross-signing
  (step 2) needs no password.
- No device-list bump was needed for the bot to pick up the signing keys: its
  OlmMachine fetches the sender's keys on demand when decrypting the encrypted command
  (a `keys/upload` re-push attempt returned 400 and was removed from the driver as dead
  weight — see transcript of the first run if kept).

## Acceptance mapping (issue #20 `## Acceptance`)

- "Cross-signing verification on admin commands. Refuse commands from unverified
  devices." — steps 1, 4 (refusals) and step 3 (verified sender accepted) — covered on
  a real homeserver running this branch's code.
- "Move knock-approver to a mautrix-based bot (= unblock #7)" — already merged (#68 /
  earlier work); this branch's approver is the mautrix-based bot exercised above.
- "Same for the hermes shape-rotator agent." — **NOT covered here**; separate
  deployment/repo, still outstanding (unchanged from the PR body).
- "Document the threat model in `docs/SECURITY.md`" — in this PR's diff.

## Why not the ephemeral Phala CVM (`docker-compose.staging.yml`)

The rebase push auto-triggered the #69 staging validation on this branch
(`04-ci-staging-deploy-attempt.txt`): it dies in ~2 s with `HTTP: 401 Invalid API key` —
the `PHALA_API_KEY` repo secret is stale (revoked Phala-side since 2026-05-28; the box
profile is rejected too). Only the operator can mint a fresh key.

Independently, the staging vehicle itself is currently broken for this repo's approver
size (`05-staging-compose-argmax.txt`): `APPROVER_B64` (172,216 bytes here; 169,624 on
pre-#70 staging) exceeds the kernel's 131,072-byte per-argument limit, so
`docker-compose.staging.yml`'s `echo "$APPROVER_B64" | base64 -d > /app.py` crash-loops
with `exec /usr/bin/sh: argument list too long` — reproduced live. **Prod's
`docker-compose.yml` line 118 uses the same pattern**, so the next `v*` tag deploy of an
approver this size fails the same way. Pre-existing (not introduced by this PR);
belongs to the #14/deploy lane, reported here for the operator.

## Repro (this box)

```bash
git worktree add /tmp/rw70 origin/ready-20 && cd /tmp/rw70
# bring up the PR-gating E2E stack (same stack CI ran green on ready-20 today)
COMPOSE="docker compose -f tests/docker-compose.test.yml -p rw70e2e"
$COMPOSE up -d --build continuwuity
# ... wait for the one-time bootstrap token in logs, export CONDUWUIT_BOOTSTRAP_TOKEN
$COMPOSE build && $COMPOSE up -d bootstrap knock-approver landing
$COMPOSE run --rm test-runner bash -c '
  set -a; . /shared/test.env; set +a
  export HS=http://landing:80
  DEV_HS=$HS DEV_REG_TOKEN=$CONDUWUIT_REGISTRATION_TOKEN \
  ADMIN_COMMAND_ROOM=$ADMIN_COMMAND_ROOM ADMIN_TOKEN=$ADMIN_TOKEN ADMIN_MXID=$ADMIN_MXID \
  python3 .evidence/issue-20/admin_room_flow.py'
```

## What I could NOT verify (unchanged, operator-only)

- The flow on the **ephemeral Phala CVM** — blocked on a fresh `PHALA_API_KEY` (above).
- The flow on the **live admin room at mtrx.shaperotator.xyz with the operator's real
  identity** (`@socrates1024:matrix.org`, cross-signed in Element) — deploying this
  branch there is a tag-triggered prod deploy, which stays in operator hands.
- The hermes shape-rotator agent half of the acceptance — separate repo.
