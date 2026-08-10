# Local dev stack

A minimal continuwuity instance on `localhost:46167` so you can iterate on
`approver.py`, `landing/*.html`, and `nginx.conf` without `phala deploy`.

## What it gives you

- continuwuity homeserver (no TLS, no federation, registration open behind a
  token) on `http://localhost:46167`
- An `@admin:localhost:46167` user with PL 100 in a fresh `Shape Rotator (dev)`
  space + `general` / `announcements` / `bot-noise` children (join rules match
  prod: space=knock, children=restricted)
- A signup code and a knock code, both with 99 uses, printed as env vars

## Usage

```bash
cd dev
docker compose up -d
eval "$(python3 bootstrap.py)"      # sets HS, MATRIX_TOKEN, SPACE_ID, etc.

# Now run the approver directly against the local stack:
cd ..
python3 knock-approver/approver.py

# In another shell, hit the signup API:
curl -X POST http://localhost:$HTTP_PORT/signup/api -H 'Content-Type: application/json' \
  -d "{\"code\":\"$DEV_SIGNUP_CODE\",\"username\":\"test\",\"password\":\"longpassword12345\"}"
```

State lives in `dev/.dev-state.json` + the `continuwuity-dev-data` Docker
volume. Re-running `bootstrap.py` is idempotent.

## Reset

```bash
cd dev
docker compose down -v          # drops the rocksdb volume too
rm .dev-state.json
```

## Smoke test against the dev stack

The main `tests/smoke.py` takes env vars so it runs against either environment:

```bash
eval "$(python3 dev/bootstrap.py)"
# Start approver in the background first (HTTP_PORT=18001)
HOMESERVER=http://localhost:46167 \
  ADMIN_TOKEN=$MATRIX_TOKEN \
  SIGNUP_CODE=$DEV_SIGNUP_CODE KNOCK_CODE=$DEV_KNOCK_CODE \
  REG_TOKEN=$CONDUWUIT_REGISTRATION_TOKEN \
  SPACE_ID=$SPACE_ID SPACE_CHILDREN=$SPACE_CHILD_IDS \
  python3 tests/smoke.py
```

## Known quirks

- **Continuwuity bootstrap token**: on first boot the homeserver prints a
  one-time registration token in its logs; the configured
  `CONDUWUIT_REGISTRATION_TOKEN` doesn't work until the first user has signed
  up with that one-time value. `bootstrap.py` scrapes it from
  `docker logs dev-continuwuity-1`.
- **Room-id suffix form**: continuwuity sometimes returns suffixed
  (`!foo:host:port`) and sometimes unsuffixed (`!foo`) room IDs from
  `createRoom`, and its `/rooms/{id}/invite` endpoint rejects the wrong form
  for that server. `bootstrap.py` passes through whatever the server returned;
  don't normalize.
- **Port choice**: we use `46167` (rather than the tutorial-default `16167`)
  because other throwaway repro containers in the workspace tend to grab the
  lower ports.

## Running two dev stacks at once

Parallel agent lanes on one box need separate compose projects and separate
host ports, or `docker compose down -v` in one lane wipes the other's rocksdb:

```bash
# lane A — defaults, exactly as before
docker compose up -d
python3 bootstrap.py

# lane B — concurrent, fully isolated
DEV_STACK_SUFFIX=-b DEV_HS_PORT=46168 docker compose up -d
DEV_HS=http://localhost:46168 python3 bootstrap.py
```

`DEV_STACK_SUFFIX` scopes the compose project name and its named volume;
`DEV_HS_PORT` moves the published port. Neither is required — with both unset
this behaves as it always has.

`CONDUWUIT_SERVER_NAME` stays `localhost:46167` in every lane on purpose. It is
the server's identity (it appears in mxids and in `bootstrap.py`'s `via`
entries), not an endpoint anyone dials, and federation is off in dev.

`tests/run_e2e.sh` derives its own compose project from the checkout path, so
two worktrees running the e2e suite concurrently do not collide. Override with
`E2E_PROJECT` if you need a specific name.
