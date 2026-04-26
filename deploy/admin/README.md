# `deploy/admin/` — operator scripts

One-shot helpers for administering the Shape Rotator Matrix deployment.
Each is invoked manually by an operator with the relevant secret in env;
nothing here runs in CI.

## `mint_token.sh` — rotate `KNOCK_APPROVER_TOKEN`

Mints a fresh access token for `@shape-rotator-2:mtrx.shaperotator.xyz`,
syncs it into both your local `.env` and the GitHub Actions secret of
the same name, then re-enables and triggers the deploy workflow.

Use when:
- The running approver is sync-401'ing (`phala logs --cvm-id dstack-matrix dstack-knock-approver-1` shows `M_UNKNOWN_TOKEN`).
- You're rotating the bot's password and need a clean device.
- A previous CVM was scrubbed and the sealed env was lost.

```bash
SHAPE_ROTATOR_2_PASSWORD='…' bash deploy/admin/mint_token.sh
```

The token is never echoed to stdout. If `/whoami` doesn't return
`@shape-rotator-2`, the script aborts before touching `.env` or the GH
secret — so a typo'd password just fails out.

## Convention

Scripts here:
- Read secrets from env vars only — no flags, no positional args, no
  files-in-repo containing values.
- Validate before mutating (e.g. `/whoami` before pushing the token).
- Are idempotent — running twice is the same as once.
