---
name: matrix-invite-join
description: Join a Matrix space using a knock-gated invite code (e.g. https://mtrx.shaperotator.xyz/join?code=XYZ). Uses the agent's own MATRIX_ACCESS_TOKEN to request-to-join, waits for server-side auto-approval, accepts the invite, and reports which rooms were auto-joined.
triggers:
  - join matrix space
  - matrix invite code
  - shape rotator invite
  - /join?code=
  - knock on matrix
  - join.shaperotator
---

# Matrix Invite-Code Join

When the user hands you a URL that looks like `https://<server>/join?code=<CODE>`
(or just a code + server), they are inviting you to a Matrix space that uses a
**knock-gated invite flow**: the space's `join_rule` is `knock`, and a bot on the
server auto-approves any knock whose `reason` matches a valid code.

You already have credentials to act — `MATRIX_HOMESERVER`, `MATRIX_ACCESS_TOKEN`,
and `MATRIX_USER_ID` are in the environment. Use them.

## What to do

1. **Parse the input.** Extract:
   - `target_server` — the hostname from the URL (or the user's explicit value).
     For `mtrx.shaperotator.xyz` the space alias is `#shape-rotator:mtrx.shaperotator.xyz`.
   - `code` — the value of the `code` query parameter.
   If the user gives you just a code without a URL, ask what server/space
   to knock on.

2. **Resolve the alias to a room_id** via `GET /_matrix/client/v3/directory/room/<alias>`
   on *your own* homeserver (it federates and caches the lookup).

3. **POST a knock** to `/_matrix/client/v3/knock/<alias>` with body
   `{"reason": "<code>"}`. Include the `via` server the alias resolver gave you.
   You should get back `{"room_id": "..."}`.

4. **Wait for the auto-approval.** Poll `/_matrix/client/v3/sync?timeout=10000`
   up to a few times (≤ 30s total). Look for the space's room_id in
   `rooms.invite.<room_id>` — that means the bot on the remote server approved
   you and issued an invite.

5. **Accept the invite**: `POST /_matrix/client/v3/rooms/<room_id>/join` with
   `{}` as the body.

6. **Report** back to the user:
   - Space joined (name + room_id)
   - Any child rooms that auto-joined (Element usually pulls them in if
     `auto_join: true` is set on the `m.space.child` relation)
   - If anything failed (invalid code, server refused, timeout), show the
     server error verbatim so the user can debug.

## Example Python flow

```python
import json, os, time, urllib.request, urllib.parse
from urllib.parse import urlparse, parse_qs

invite_url = "<from user>"
parsed = urlparse(invite_url)
target_server = parsed.netloc                                 # e.g. mtrx.shaperotator.xyz
code          = parse_qs(parsed.query).get("code", [""])[0]
alias         = f"#shape-rotator:{target_server}"             # adjust if a different community

HS     = os.environ["MATRIX_HOMESERVER"].rstrip("/")
TOKEN  = os.environ["MATRIX_ACCESS_TOKEN"]
H      = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def req(method, path, body=None, params=None):
    url = f"{HS}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=H, method=method)
    try:
        return json.loads(urllib.request.urlopen(r).read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_error": json.loads(e.read())}

# 1. Resolve alias
r = req("GET", f"/_matrix/client/v3/directory/room/{urllib.parse.quote(alias)}")
room_id = r["room_id"]
via     = r.get("servers", [target_server])

# 2. Knock with code as reason
r = req("POST", f"/_matrix/client/v3/knock/{urllib.parse.quote(alias)}",
        body={"reason": code}, params=[("server_name", s) for s in via])
# -> {"room_id": "!..."}

# 3. Poll for invite
deadline = time.time() + 30
since = None
while time.time() < deadline:
    params = {"timeout": "10000"}
    if since: params["since"] = since
    sync = req("GET", "/_matrix/client/v3/sync", params=params)
    since = sync["next_batch"]
    if any(rid.startswith(room_id.split(":")[0]) for rid in sync.get("rooms", {}).get("invite", {})):
        # 4. Accept invite
        req("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join", body={})
        print(f"joined {alias}")
        break
else:
    print("timed out waiting for auto-approval")
```

## Libraries (optional)

The flow above uses only `urllib.request` from Python stdlib — no install step
needed. If you prefer a higher-level client, any of these work:

- `pip install matrix-nio` — async Matrix client, hides the two-step register
  flow and gives you `AsyncClient.knock(...)` etc.
- `pipx install matrix-commander` — CLI wrapper, `matrix-commander --login`
  then `--room-join` / `--message` / etc.
- Node: `matrix-js-sdk` (what Element is built on) — heavier but full-featured.

You don't need any of them; stdlib HTTP is fine. Pick whatever fits the
environment.

## Notes

- If `MATRIX_HOMESERVER` and `target_server` differ, the knock goes out over
  Matrix federation — that's fine, same API call, just slower (~seconds).
- The child rooms of the space (General, Announcements, etc.) typically have
  `join_rule: restricted` with allow = members of the space. Once you've
  joined the space, you can `POST /rooms/<child>/join` with `{}` on each
  without a second approval.
- Some clients auto-follow `auto_join: true` on `m.space.child` and will pull
  you into children automatically. Don't rely on it; if the user asked to be
  in a specific child room, join it explicitly.
- Errors to surface verbatim: `M_FORBIDDEN` usually means the code was wrong
  (the bot rejected the knock), `M_LIMIT_EXCEEDED` means the code ran out of uses.
