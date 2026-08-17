# Evidence — issue #14 (Ephemeral staging CVM for PR-time deploy validation)

## Tier 0 — no behavior change
This diff adds **only** CI infrastructure + a staging-only compose override:

| File | Role | Touches prod/app code? |
|---|---|---|
| `.github/workflows/staging-validate.yml` | new CI workflow | no |
| `docker-compose.staging.yml` | staging-only compose (subset of prod) | no — `deploy.yml` still uses `docker-compose.yml` exclusively |
| `tests/staging_bootstrap.py` | phase-2 helper called by the workflow | no |
| `PLAN.md` | plan derived from `## Acceptance` | n/a |

Zero application code changes. Zero changes to `docker-compose.yml` or `deploy.yml`.
The continuwuity / knock-approver / landing services in the staging compose mirror the prod
definitions so the staging run exercises the real boot path, but they are only ever deployed to a
short-lived ephemeral CVM that this workflow creates and tears down.

## Local verification (all green on this box)
1. **Workflow YAML parses** — `python3 -c yaml.safe_load` → OK. (`01-yaml.txt`)
2. **Staging compose YAML parses** → OK. (`01-yaml.txt`)
3. **Every inline `run:` script passes `bash -n`** (all 7 steps) — no shell syntax errors. (`02-bash-n.txt`)
4. **`tests/staging_bootstrap.py` + `tests/smoke.py` pass `python3 -m py_compile`**. (`03-pycompile.txt`)
5. **`docker compose -f docker-compose.staging.yml config`** parses + interpolates cleanly with dummy env. (`04-compose-config.txt`)
6. **`phala` CLI command surface confirmed against `--help` (v1.1.19)** and against the **live Phala
   API** via a free `cvms list -j` read (no CVM created): real schema is
   `{success,page,pageSize,total,totalPages,items[]}`, each item `{appId,cvmName,status,uptime}` —
   exactly what the workflow's status-poll parses. (`05-phala-cli.txt`)

## What I could NOT verify on this box (honest)
The workflow's entire purpose is a **live Phala round-trip** — create a CVM, run smoke, tear it
down. That round-trip is inherently:

- **cost-incurring** (real Phala spend; the account already runs 49 CVMs), and
- **self-verifying on first trigger** (a `pull_request` workflow doesn't run for sibling PRs until
  it's merged to the base branch, so the first live proof is a manual `workflow_dispatch`).

I therefore did **not** create a CVM during development (billing semantics are themselves
acceptance item #1, still operator-confirm). The pieces most likely to need a tweak on that first
watched dispatch, called out explicitly in the PR:

- **Endpoint URL** — Phala assigns `<appId>-<port>.<cluster-domain>`. The workflow reads the real
  cluster domain out of `phala cvms get` and builds an explicit `http://<appId>-80.<cluster>` URL
  (so smoke.py's stdlib urllib doesn't have to TLS-verify a phala-issued cert). Falls back to the
  `prod9` cluster with a `::warning::` if the record exposes no endpoint.
- **smoke.py on a fresh RocksDB** — `tests/smoke.py` hardcodes the prod space/children by default;
  the workflow bootstraps a fresh space + 3 children and points smoke at them via env. The E2EE
  cross-signing portions of the knock path are the most likely to behave differently on a
  brand-new server.
- **Phala billing** — path filter + unique-CVM-per-run + guaranteed teardown + orphan sweep bound
  the spend either way; the dashboard check is the operator-only acceptance #1.

## Acceptance mapping
- #1 billing semantics → **operator-confirm** (commented on issue); spend bounded by design.
- #2 create→smoke→teardown → workflow complete; **self-verifies on first `workflow_dispatch`**.
- #3 broken-compose test → follow-up PR the operator pushes once the workflow is on `staging`.
- #4 #13 slow-Phala → handled: the workflow polls JSON status for 10 min and fails loud at PR time
  if the CVM never reaches `running` (the staging analogue of #13's prod recovery).

--- 2026-08-16 addendum (rework pass on PR #69) ---
First dispatch (run 31968806742) failed: stale PHALA_API_KEY -> 401 at provision (06). Patched
phase 1 to fail fast on definitive auth rejection; re-ran checks 01/02 equivalents on the patched
file (07). Blocked on operator: fresh Phala API key in the PHALA_API_KEY secret, then one re-dispatch.

--- 2026-08-16 addendum 2 (rework pass 2 on PR #69) ---
The patch's own push triggered run 31969904168 (pull_request event): the fast-fail fired live,
1.2 s into phase 1, with the actionable error; orphan sweep + always() teardown both ran; no CVM
created, zero spend (08). Fast-fail is now CI-observed, not just grep-tested. Credential still
revoked Phala-side (repo secret unchanged since 2026-05-28; box profile rejected too) — every
acceptance path (#2 round-trip, #3 broken-compose run) requires a key only the operator can mint.
Still blocked; no ready-to-merge set.
