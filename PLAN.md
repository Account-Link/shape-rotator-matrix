# PLAN — issue #2: Path A inviter hardcode

Base: origin/staging @ 32d5978. Tier 1 (paste-flow change; observable halt vs wrong-DM, no deployed UI).

## State found
- skills/matrix-invite-join/SKILL.md:41 still hardcodes `@socrates1024:matrix.org`.
- The `:356` hit named in the issue no longer exists on staging (removed by #67 / Paste C rework). Only :41 remains.

## Checklist (from ## Acceptance)
- [x] `grep -rn socrates1024 skills/` returns nothing → replace :41 with `<INVITER>` placeholder + adjacent substitution instruction
- [x] Unmodified Path A paste halts on the unreplaced placeholder and asks for the inviter (explicit guard, raise SystemExit — not assert, which `-O` strips); never DMs a literal default
- [x] Path B untouched — approver.py still derives `inviter = entry.inviter || ONBOARDING_INVITER_MXID`
- [x] Verify: py_compile the paste body as extracted; simulate unmodified run → halt; simulate substituted run against homeserver-less stub (DM step would need real creds — checked by transcript of the halt + grep)

## Files
- skills/matrix-invite-join/SKILL.md (line 41 region only)
