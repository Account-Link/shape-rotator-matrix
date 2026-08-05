# Evidence — issue #43 (deploy.yml: validate creds before the 10-min sleep)

**Tier 0** — CI/workflow-only change. Zero deployed-app behavior change (the issue
confirms: "Tier 0 on the deployed app — no runtime behavior changes"). The only runtime
effect is that the deploy job can now abort a few seconds earlier, before the 10-minute
pre-deploy sleep, when a secret is stale.

## What changed
- `deploy/validate-creds.sh` (new) — checks `KNOCK_APPROVER_TOKEN` (via `whoami`) and
  `PHALA_API_KEY` (via `phala cvms get dstack-matrix`). **Accumulates** failures and names
  every bad secret in one run (the issue's proposed snippet `exit 1`s on the first failure,
  which would NOT satisfy "two stale secrets cost one cycle, not two"). Distinguishes a
  definitive auth failure (whoami 401/403; phala "Invalid API key") from an indeterminate
  one (timeout / 5xx) so a homeserver hiccup is not mislabeled a stale token. Prints no
  secret values.
- `.github/workflows/deploy.yml` — one new step `validate creds (fail fast on stale
  secrets)`, placed after `install phala CLI` (needs the binary) and before `build .env` /
  the 10-minute `pre-deploy heads-up + delay` sleep.

## Local verification (this box, real endpoints)
Run with deliberately-wrong, non-secret test values (no real secrets used).

### 01-both-bad.txt — BOTH stale (definitive auth failures)
- `KNOCK_APPROVER_TOKEN` = bogus → whoami **401**
- `PHALA_API_KEY` = bogus → `phala cvms get` exits 1, stderr `✗ Invalid API key`
- Result: names **KNOCK** stale AND **PHALA** stale in ONE run; exit **1**; **~1.6s**
  (≪ 30s, and before the 10-min sleep by step placement); **no secret value** printed.
- This is the acceptance's core case: "two stale secrets cost one cycle, not two."

### 02-indeterminate.txt — homeserver 500 (NOT a stale token) + bad phala key
- whoami returns **500** (mocked local server) → reported honestly as
  "could not be validated — probably NOT a stale token" (NOT mislabeled stale); deploy
  still aborts (fail-closed).
- phala bad key → reported stale.
- Demonstrates the no-masking rule: a network/server error is not blamed on the secret.

### phala stderr-capture idiom (branch logic)
`phala_err=$(phala cvms get … 2>&1 >/dev/null)` captures rc=1 and stderr containing
`Invalid API key`; the `grep -qi 'invalid api key'` match routes it to the "stale" branch.

## What I could NOT verify on this box (operator-run; see issue comment)
The issue requests "a link to a workflow run with a deliberately-wrong secret + a passing
run." That evidence is inherently **post-merge / operator-gated**:
- The deploy workflow only fires on `v*` tag push or `workflow_dispatch`, and then performs
  a **real production deploy** of the `dstack-matrix` CVM. No prod credentials exist on this
  box (box-inventory), and workers must not trigger production deploys.
- The new step cannot run inside the workflow until the branch is merged and a tag is pushed.
- Producing the "wrong secret" variant requires the operator to write a deliberately-bad GH
  secret (a repo-secret write, not available to the worker).

So the pre-merge Tier-0 evidence above is the strongest verification possible from this box;
the real-workflow-run evidence is left as the explicit operator step in the issue comment.

## The "all good → creds validated" branch
Not exercised locally (would require the real `PHALA_API_KEY`, a repo secret). Logic is
trivial: both checks pass → `failures` stays 0 → prints `creds validated` → exit 0. The
deploy workflow itself exercises this on every successful tag deploy once merged.
