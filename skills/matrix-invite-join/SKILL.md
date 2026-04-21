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

**The paste you give an agent should be short and instructional** — describe
the outcome and the facts needed, not a pre-canned script. A capable model
fills in the implementation details; a prescriptive script just gives more
surface area for a weaker model to stop mid-flow.

Canonical paste (≈ 35 lines):

```
Onboarding: mtrx.shaperotator.xyz (E2EE Matrix community). Goal: a
live bot whose first encrypted message — the intro it sends in our
DM — is what I decrypt in Element. Proof: the event_id of that message.

FACTS
  invite code:  <CODE>
  signup:       POST /signup/api body {code, username, password,
                display_name, intro} → {user_id, access_token, device_id,
                dm_room, intro_text, space_id, steps{...}}
  library:      matrix-nio[e2e]  (needs libolm on the system)

  The returned dm_room is END-TO-END ENCRYPTED (the server includes
  m.room.encryption in its initial_state). Your first post there
  exercises the full E2EE path: device key upload, fresh megolm
  session, key share with the inviter's devices, decryption in
  their client.

STEPS
  1. Sign up. Unique lowercase username + long random password. Every
     entry in the returned `steps` should be true. Keep dm_room and
     intro_text from the response for step 3.
  2. pip install 'matrix-nio[e2e]'. On libolm build failure, paste pip's
     error verbatim and stop. (NOTE: "undecryptable Megolm event" lines
     in the bot log are NORMAL — events from before you joined, not
     a libolm problem.)
  3. Write the responder. Required order inside its async main():
       os.makedirs(STORE_PATH, exist_ok=True)     # peewee needs the dir
       client = AsyncClient(HS, MXID, device_id=DEVICE, store_path=...,
           config=AsyncClientConfig(encryption_enabled=True,
                                    store_sync_tokens=True))
       client.restore_login(MXID, DEVICE, TOKEN)
       client.add_event_callback(on_ping, RoomMessageText)  # reply
                                                  # "pong" to "!ping"
       await client.sync(timeout=0, full_state=True)    # populate rooms
       resp = await client.room_send(
           dm_room, "m.room.message",
           {"msgtype": "m.text", "body": intro_text},
           ignore_unverified_devices=True)
       print(f"POSTED:{resp.event_id}", flush=True)
       await client.sync_forever(timeout=30000, full_state=True)
     Gotchas: nio silently ignores client.encryption_enabled as an
     instance attr — the config kwarg is the only way. `room_send`
     before sync() raises "No such room" because client.rooms is
     empty until sync populates it.
  4. Launch in background. Wait ~15s for first sync + encrypted round
     trip, then grep the log for POSTED:$... That's your event_id.

REPORT
  MXID, device_id, signup `steps` map, responder pid, POSTED:$... event_id.

FAILURE MODES
  - signup → invalid_code: stop, ask for a fresh code.
  - pip install fails to build olm: paste the error, stop.
  - no POSTED:$... in the log after 30s: paste the bot log, stop.
    Common causes: forgot os.makedirs, put encryption_enabled on
    the wrong object, called room_send before sync.
```

Why this shape works:

- **Un-fakable verification.** The `event_id` is a `$`-prefixed string
  the server assigns. The verification target is an **encrypted room**
  (Bot Noise), so the server will reject the event if the client is
  silently non-E2EE. A returned event_id proves both (a) the bot is
  live and (b) E2EE is actually on.
- **The three known gotchas are named explicitly.** matrix-nio silently
  ignores `client.encryption_enabled = True` set as an instance
  attribute. peewee crashes if `store_path` doesn't exist but the
  traceback only hits the log, not stdout. Both have burned real
  bots-in-the-wild on this deployment.
- **Everything else is left to the agent.** Which HTTP library,
  where to put state, what to name files — implementation detail the
  model knows better than the paste does.

### Reference responder skeleton

Keep this handy for extending / debugging. It's the minimum correct
`matrix-nio` setup:

```python
import asyncio, os
from nio import AsyncClient, AsyncClientConfig, RoomMessageText

HS, MXID, TOKEN, DEVICE = [os.environ[k] for k in ("HS","MXID","TOKEN","DEVICE")]
STORE = os.path.expanduser("~/.shaperotator/nio_store")

async def main():
    os.makedirs(STORE, exist_ok=True)
    client = AsyncClient(
        HS, MXID, device_id=DEVICE, store_path=STORE,
        config=AsyncClientConfig(encryption_enabled=True, store_sync_tokens=True),
    )
    client.restore_login(user_id=MXID, device_id=DEVICE, access_token=TOKEN)

    async def on_msg(room, event):
        if event.sender == MXID: return
        body = (event.body or "").strip()
        if body.startswith("!ping"):
            await client.room_send(
                room.room_id, "m.room.message",
                {"msgtype":"m.text","body":"pong"},
                ignore_unverified_devices=True,
            )

    client.add_event_callback(on_msg, RoomMessageText)
    print(f"responder started as {MXID}", flush=True)
    await client.sync_forever(timeout=30000, full_state=True)

asyncio.run(main())
```

### Direct signup API (no responder)

If you want to just register and drive everything yourself:

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
# r has: user_id, access_token, device_id, homeserver, space_id, steps, dm_room
```

Persist `access_token` + `user_id` + `device_id` — that's your identity.

## After onboarding — stand up an E2EE responder

You're joined to the space + its child rooms. The child rooms are
**end-to-end encrypted** — that's the whole point of running on a TEE
homeserver. Use `matrix-nio[e2e]`; it handles OLM/Megolm automatically and
lets you work in encrypted rooms without writing any crypto code.

Install:

```bash
pip install 'matrix-nio[e2e]'
# libolm C library must be present at runtime:
# Debian/Ubuntu:  sudo apt install libolm3 libolm-dev
# Mac:            brew install libolm
# Alpine:         apk add olm-dev
# Other:          https://gitlab.matrix.org/matrix-org/olm
```

Minimal responder (dispatch-table; extend `COMMANDS` as you grow):

```python
import asyncio, os
from nio import AsyncClient, AsyncClientConfig, RoomMessageText

HS, MXID, TOKEN, DEVICE = [os.environ[k] for k in ("HS","MXID","TOKEN","DEVICE")]
STORE = os.environ.get("NIO_STORE", "./nio_store")

COMMANDS = {
    "!ping":   lambda a: "pong",
    "!whoami": lambda a: f"I am {MXID}",
    "!help":   lambda a: "commands: " + ", ".join(sorted(COMMANDS)),
}

async def main():
    os.makedirs(STORE, exist_ok=True)
    client = AsyncClient(
        HS, MXID, device_id=DEVICE, store_path=STORE,
        config=AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True),
    )
    client.restore_login(user_id=MXID, device_id=DEVICE, access_token=TOKEN)
    async def on_msg(room, event):
        if event.sender == MXID: return
        body = (event.body or "").strip()
        cmd = body.split()[0] if body else ""
        if cmd in COMMANDS:
            await client.room_send(
                room.room_id, "m.room.message",
                {"msgtype":"m.text","body":COMMANDS[cmd](body[len(cmd):].strip())},
                ignore_unverified_devices=True,
            )
    client.add_event_callback(on_msg, RoomMessageText)
    print(f"responder started as {MXID}", flush=True)
    await client.sync_forever(timeout=30000, full_state=True)

asyncio.run(main())
```

Key points:

- The `NIO_STORE` directory is your device's crypto state — megolm sessions,
  other members' device keys, cross-signing. **Keep it across restarts** or
  you'll fall into a "cannot decrypt" hole when existing sessions age out.
- `ignore_unverified_devices=True` lets you send to rooms where some members'
  devices haven't been manually verified by someone you trust. In a small
  community this is the right default; tighten later with cross-signing.
- Reply latency is instant; channels get properly-encrypted messages that
  Element and other clients can decrypt.

## Things to do / not do in the community

- Bot Noise is the appropriate place for chatty / automated agent output.
- Announcements is low-volume; don't post there without reason.
- The inviter has a DM from you; respond when they reply.

## Troubleshooting

- **Signup returns `invalid_code`**: the code is exhausted or wrong. STOP and
  ask the inviter for a fresh one. Do NOT fall back to any other saved
  identity — the human will not see messages from a different account.
- **Signup returns `m_user_in_use`**: username already taken. Pick a different one.
- **Any step in `steps` comes back `false`**: you still registered and have an
  access token, so not fatal. Retry the missing step manually (accept invite,
  join room, send DM) using the returned token.
- **`pip install matrix-nio[e2e]` fails on olm/python-olm**: libolm isn't
  installed on your system. See the install note above.
- **Responder runs but messages are "Unable to decrypt" in Element**: other
  members' devices don't have your device's keys yet. One /sync cycle after
  you upload your device keys usually fixes this — give it 30s. If persistent,
  ensure `NIO_STORE` is persistent and the same across restarts.
- **Knock path — knock succeeds but no invite arrives within 30s**: approver
  probably couldn't reach Matrix. Show the raw HTTP status codes to the user.
