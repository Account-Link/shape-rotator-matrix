# PLAN — issue #14: Ephemeral staging CVM for PR-time deploy validation

Repo: teleport-computer/shape-rotator-matrix · base: `staging` · branch: `ready-14`
One issue, then stop (per matrix-ready-worker.md).

## Acceptance (copied from issue #14)
- [ ] Phala billing semantics confirmed.
- [ ] Workflow opens, creates a CVM, runs smoke, tears down on every PR.
- [ ] Tested with a deliberately-broken compose change — workflow fails, CVM still gets torn down.
- [ ] Tested with the real PR #13 recovery path — staging would have caught it (simulate Phala slowness).

## What I can verify on this box (the verifiable subset — box-inventory scope-down rule)
- [x] Workflow YAML parses; every inline `run:` script passes `bash -n`.
- [x] `docker-compose.staging.yml` interpolates/parses via `docker compose config`.
- [x] `phala` CLI command/flag surface confirmed against `phala --help` (v1.1.19):
      `deploy --name` (create, no --cvm-id) · `cvms list --search <name> -j` · `cvms delete <cvm> -f` · `cvms get <cvm> -j`.
- [x] `phala cvms list -j` real schema confirmed against the live Phala API (free read, no CVM created):
      `{success,page,pageSize,total,totalPages,items[]}`, each item `{appId,cvmName,status,uptime}`.
      → my JSON status-poll matches reality.

## What self-verifies on the FIRST live run (operator-triggered; this is the workflow's function)
- [ ] Acceptance #2 (create→smoke→teardown): dispatch `staging-validate` once on `staging` and watch.
      `pull_request` triggers won't fire for this workflow until it's merged to the base branch, so the
      first proof is a manual `workflow_dispatch`. Costs one short-lived Phala CVM.
- [ ] Acceptance #3 (broken-compose test): push a branch that breaks `docker-compose.staging.yml`,
      open a PR, confirm the run goes red AND teardown still fires.
- [ ] Acceptance #4 (#13 slow-Phala): the JSON status-poll with a 10-min ceiling is the staging analogue
      of #13's recovery — if Phala is slow the run goes red at PR time instead of stranding prod.

## Operator-only (commented back to the issue, not blockable on this box)
- [ ] Acceptance #1 (billing semantics): needs a glance at the Phala dashboard. Mitigation already in place:
      path-filtered trigger (only deploy-relevant paths) + unique CVM per run_id + guaranteed teardown +
      orphan sweep, so spend is bounded regardless of per-second vs per-hour billing.

## Tier
**Tier 0 — no behavior change.** The diff adds a CI workflow + a staging-only compose override; it
touches ZERO application code and ZERO prod deploy path (`docker-compose.yml` and `deploy.yml` are
unchanged; `docker-compose.staging.yml` is never used by prod). The live Phala round-trip is the
workflow's own function and is inherently an operator-triggered, cost-incurring validation — flagged
explicitly in the PR rather than masked.
