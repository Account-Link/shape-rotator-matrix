# Tests

## `smoke.py` — end-to-end smoke test

Exercises the two onboarding paths end-to-end against any configured
homeserver (prod by default). Uses only Python stdlib.

Checks:
- `POST /signup/api` returns 200 and every `steps` value is `true`
- The new account's own `/sync` reflects space + child-room membership
- `/knock` with a valid code is auto-approved within 15s
- `/knock` with a bogus code is NOT approved after 8s
- Cleans up by kicking all test users before exit

### Against prod

```bash
ADMIN_TOKEN=<shape-rotator-2 token> \
REG_TOKEN=<continuwuity reg token> \
SIGNUP_CODE=<a code with uses remaining> \
KNOCK_CODE=<a knock code with uses remaining> \
  python3 tests/smoke.py
```

### Against local dev stack

```bash
eval "$(python3 dev/bootstrap.py)"
# (start approver in another shell first, with HTTP_PORT set)

HOMESERVER=http://localhost:46167 \
  ADMIN_TOKEN=$MATRIX_TOKEN \
  REG_TOKEN=$CONDUWUIT_REGISTRATION_TOKEN \
  SIGNUP_CODE=$DEV_SIGNUP_CODE \
  KNOCK_CODE=$DEV_KNOCK_CODE \
  SPACE_ID=$SPACE_ID \
  SPACE_CHILDREN=$SPACE_CHILD_IDS \
  python3 tests/smoke.py
```

Exit code 0 = all checks passed. Any failure prints the specific line that
didn't match plus the server's response so the cause is obvious.
