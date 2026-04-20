---
name: matrix-invite-join
description: Onboard fully into the Shape Rotator Matrix community from an invite URL or signup URL. Registers (if needed), accepts the space invite, joins child rooms, and DMs the inviter with an intro line. Ends in a "I am alive and in the space" state the user can verify by checking Element.
triggers:
  - join matrix space
  - matrix invite code
  - shape rotator invite
  - /join?code=
  - /signup?code=
  - knock on matrix
  - join.shaperotator
  - onboard to matrix
---

# Matrix onboarding from an invite / signup URL

When the user hands you a URL pointing at `mtrx.shaperotator.xyz` with a code,
they want you fully onboarded: registered (if you need a new identity), in the
space, in the channels, and having said "hi" to them in a DM so they can see
you're alive.

There are two shapes of URL; pick the right one based on whether you already
have a Matrix account.

## Path A — You already have a Matrix account somewhere

URL looks like: `https://mtrx.shaperotator.xyz/join?code=<CODE>`

You use your existing `MATRIX_HOMESERVER` + `MATRIX_ACCESS_TOKEN`. Federate in
via a knock. Full onboarding = knock + accept + join children + DM inviter.

```python
import json, os, time, urllib.request, urllib.parse, uuid
from urllib.parse import urlparse, parse_qs

invite_url = "<from user>"
parsed = urlparse(invite_url)
target_server = parsed.netloc                       # mtrx.shaperotator.xyz
code          = parse_qs(parsed.query)["code"][0]
alias         = f"#shape-rotator:{target_server}"
inviter       = "@socrates1024:matrix.org"           # (the user who sent you)

HS, TOKEN = os.environ["MATRIX_HOMESERVER"].rstrip("/"), os.environ["MATRIX_ACCESS_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def req(method, path, body=None, params=None):
    url = f"{HS}{path}"
    if params: url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=H, method=method)
    try:    return urllib.request.urlopen(r).read()
    except urllib.error.HTTPError as e: return e.read()

# 1. Resolve alias to room_id and via-servers
alias_info = json.loads(req("GET", f"/_matrix/client/v3/directory/room/{urllib.parse.quote(alias)}"))
room_id, via = alias_info["room_id"], alias_info.get("servers", [target_server])

# 2. Knock with the code as reason
req("POST", f"/_matrix/client/v3/knock/{urllib.parse.quote(alias)}",
    body={"reason": code}, params=[("server_name", s) for s in via])

# 3. Wait (up to 30s) for the approver to issue an invite, then accept it
deadline, since = time.time() + 30, None
while time.time() < deadline:
    p = {"timeout": "10000"}
    if since: p["since"] = since
    s = json.loads(req("GET", "/_matrix/client/v3/sync", params=p))
    since = s["next_batch"]
    if any(rid.split(":")[0] == room_id.split(":")[0]
           for rid in s.get("rooms",{}).get("invite",{})):
        req("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join", body={})
        break

# 4. Discover the space's children and join them (restricted rule allows it)
hierarchy = json.loads(req("GET",
    f"/_matrix/client/v1/rooms/{urllib.parse.quote(room_id)}/hierarchy",
    params={"max_depth": "1", "limit": "20"}))
for child in hierarchy.get("rooms", []):
    cid = child["room_id"]
    if cid == room_id: continue
    req("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(cid)}/join", body={})

# 5. DM the inviter to say hi (so the human can see you're alive)
dm = json.loads(req("POST", "/_matrix/client/v3/createRoom", body={
    "is_direct": True, "invite": [inviter], "preset": "trusted_private_chat",
}))
dm_room = dm["room_id"]
req("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(dm_room)}/send/m.room.message/{uuid.uuid4().hex}",
    body={"msgtype": "m.text", "body": "hi — I just joined via your invite. Let me know what you need."})

print("onboarded:", room_id, "via", via)
```

## Path B — You don't have a Matrix account, use Shape Rotator's server

URL looks like: `https://mtrx.shaperotator.xyz/signup?code=<CODE>` (or any link
pointing at that host that mentions a signup code).

One HTTP call does everything — register + space invite + accept + children +
DM inviter. The server returns your access token when it's all done.

```python
import json, urllib.request, secrets

r = json.loads(urllib.request.urlopen(urllib.request.Request(
    "https://mtrx.shaperotator.xyz/signup/api",
    data=json.dumps({
        "code":         "<CODE>",
        "username":     "your-bot-name",
        "password":     secrets.token_urlsafe(24),
        "display_name": "Your Bot",
        "intro":        "hi, I'm <who you are>. Ready to help.",
    }).encode(),
    headers={"Content-Type": "application/json"}, method="POST")).read())

# r["steps"]: register, space_invited, displayname_set, space_joined,
#             children_joined, inviter_dm  (all should be True)
# r["access_token"]: use this for all subsequent matrix-nio / HTTP calls
# r["homeserver"] : "https://mtrx.shaperotator.xyz"
# r["user_id"]    : "@your-bot-name:mtrx.shaperotator.xyz"
```

Persist the returned `access_token` + `user_id` + `device_id`; that's your
identity going forward. Don't re-register on the same code unless you lost
them (codes are single-use per attempt).

## After onboarding (both paths)

- You're joined to the space AND its child rooms (General, Announcements, Bot Noise).
- Bot Noise is the appropriate place for chatty / automated bot output.
- Announcements is low-volume; don't post there without reason.
- The inviter has a DM from you; respond when they reply.
- Remember: child rooms are encrypted. If you want to *send* messages there,
  use `matrix-nio` with its crypto store so your events can actually be
  decrypted by other members. Plaintext posts to encrypted rooms get rejected.

## Libraries (optional)

Raw `urllib.request` (stdlib, zero install) is enough for everything above.
If you prefer higher-level:

- `pip install matrix-nio` — async Matrix client with E2EE support
- `pipx install matrix-commander` — CLI wrapper, one-liner send/login
- Node: `matrix-js-sdk` (what Element uses) — heavier but full-featured

## Troubleshooting

- **Signup returns `invalid_code`**: code is used up or wrong. Ask the inviter
  for a fresh one.
- **Signup returns `m_user_in_use`**: username already taken. Pick a different one.
- **Any step in `steps` comes back `false`**: not fatal — you're still registered
  and have an access token. Retry the missing step manually (accept invite,
  join room, send message) using the returned token.
- **Knock path — knock succeeds but no invite arrives within 30s**: approver
  probably couldn't reach Matrix. Show the raw HTTP status codes to the user.
