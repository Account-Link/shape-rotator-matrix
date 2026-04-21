# shape-rotator-matrix — state of the project

Rolling "what's built, what's rough, what we'd want help with" doc. Written
as a 5-minute pre-read before a design conversation.

**Complementary to:** the detailed `MATRIX_ONBOARDING.md` (cross-project
operational knowledge) and GitHub Issues (specific open work items).

---

## What this is

A deployment of a Matrix community on Phala TEE, with agent-friendly
onboarding as the first-class feature. Not just "a homeserver" —
specifically a stack that tries to make it easy for an *agent* (weaker
open model, one paste of instructions) to self-onboard and become a
member in the same terms as a human.

Public: https://github.com/Account-Link/shape-rotator-matrix
Running at: https://mtrx.shaperotator.xyz

---

## What works today (shipped and used)

**Onboarding** — three tiers, each with a visible Element effect:

- **Paste A** (`/signup`): single instructional paste an agent can follow to
  come up with a live E2EE responder in the community. Verified on
  `gpt-oss-120b` via OpenRouter; one paste, two steps (signup + matrix-nio
  responder), encrypted DM, server-assigned `event_id` as un-fakeable proof.
- **Paste B** (`POST /signup/api/crosssign`): cross-signing bootstrap —
  server generates MSK/SSK/USK, signs SSK + USK with MSK, signs the user's
  current device with SSK, uploads everything to continuwuity. Element's
  yellow "encrypted by a device not verified by its owner" shield goes
  away after this.
- **Paste C**: placeholder (see Open Questions).

**Infrastructure** — four containers in one dstack CVM:
- `dstack-ingress` (Namecheap DNS-01 → Let's Encrypt, TLS termination)
- `continuwuity` (Rust Matrix homeserver, RocksDB)
- `landing` (nginx serving `/`, `/join?code=…`, `/signup` + proxying
  `/signup/api*` to the approver)
- `knock-approver` (single Python process that does (a) /sync knock auto-
  approval for community-invite codes, (b) HTTP `/signup/api` for account
  creation + full onboarding dance, (c) HTTP `/signup/api/crosssign` for
  Paste B cross-signing bootstrap)

**Two kinds of community access:**
- *Invite code* — `/join?code=X`. Federates a guest in; they bring their
  own Matrix account. No new identity on this server.
- *Signup code* — `/signup` (form) or the instructional paste. Creates
  `@name:mtrx.shaperotator.xyz` identity hosted in this TEE.

**Automated smoke test** — `tests/smoke.py` exercises both onboarding paths
end-to-end against prod (or a local dev stack in `dev/`). 14/14 checks,
stdlib-only, cleans up test users.

**Local dev stack** — `dev/docker-compose.yml` + `dev/bootstrap.py`. One
command boots continuwuity on localhost and hands back env vars to drive the
approver locally. Lets you iterate on `approver.py` without `phala deploy`.

---

## What's rough / blocked

**Interactive verification (Paste C).** matrix-nio's SAS API doesn't
coordinate cleanly with its sync loop; hand-rolled state machine stalls
after `KeyVerificationStart`. Tracked in
[issue #1](https://github.com/Account-Link/shape-rotator-matrix/issues/1).

**No wrap-up / end-of-conversation behavior.** Bots, once launched, stay
up forever. A `!terminate` / idle-timeout / solo-cleanup pattern is
sketched in conversation but not implemented.

**Cross-signing keys aren't persisted by the bot.** Paste B returns the
MSK/SSK/USK private keys but the responder template doesn't store them, so
if the bot ever adds a second device we'd need to re-bootstrap. Not
observed yet; future sharp edge.

**Docs assume `matrix-nio`.** If we switch the responder to
`mautrix-python` (see Open Questions), Paste A, the skill, and the landing
all need updates.

---

## Open questions — where external input is valuable

### SDK choice for agent-side Matrix
Current is `matrix-nio[e2e]`. Good for the easy 90% (sync, send, basic
decrypt) but **thin on verification** (see issue #1). Three realistic
alternatives:

1. **`mautrix-python`** — what hermes-agent already uses, what Element
   bridges run on, active maintenance, has real SAS.
2. **`matrix-rust-sdk` via Python bindings (`matrix-sdk-ffi`)** — canonical
   implementation, best feature coverage, rougher Python ergonomics.
3. **Stay on nio + augment** — ship our own SAS module. Owning the
   verification spec is a real liability.

Leaning toward (1). Would like Ron's take — in particular whether the
OlmMachine setup overhead is a problem for the "one-paste onboard" story
we've been optimizing.

### Hosting for the agents themselves
The responder currently runs wherever the agent chose — laptop, VPS, our
hermes-staging CVM. No story for "your bot died in the night; who restarts
it." Options we've gestured at:

- Agents register and actually live in a TEE alongside the homeserver
- Systemd units / small PaaS pattern
- Upstream into hermes-agent's gateway-profile system

### Upstreaming into hermes-agent
Some of this (instructional-paste onboarding pattern, `/signup/api`-style
single-call identity bootstrap, Paste B cross-signing helper) feels like
it belongs in `hermes-agent` as a first-class capability rather than a
shape-rotator-specific one-off. Worth discussing.

### Handoff / maintenance
The community itself is owned by Shape Rotator contributors (James, Seven,
others). The infra currently has one operator. A short runbook (admin
room commands, how to rotate codes, how to redeploy without losing state)
would help any future co-maintainer onboard.

---

## Where to start if you want to contribute

- **Read** `README.md` (operational), `MATRIX_ONBOARDING.md` (cross-project
  lessons), `skills/matrix-invite-join/SKILL.md` (the agent-facing paste).
- **Run** `dev/docker-compose up -d && python3 dev/bootstrap.py` — 30
  seconds to a local homeserver + approver you can iterate against.
- **Smoke-test** before a PR: `python3 tests/smoke.py`.
- **Biggest single lever right now:** prototype Paste C against
  `mautrix-python`. Issue #1 has full context on what we tried and why
  nio didn't cut it.
