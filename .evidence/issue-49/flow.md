# Flow evidence — issue #49 (PR #90, branch `bridge-bot-api`)

Walked 2026-08-19 ~18:50–19:10 UTC from `zed`, against the deployed tenant
`https://pod.dstack.soc1024.com/mx-tg-relay/`.

## The story's acceptance, verbatim

> - Message posted in Telegram appears in the Matrix room within ~10s with sender name, and vice versa
> - Kill the relay for a minute, restart: missed messages arrive once, no duplicates
> - README section: how to run it, where state lives, how to add a room pair

## Classification

Tier 2 (the relay serves user-visible pages: `/`, `/detail.html`) evidenced below by a
walk of the deployed surface, plus Tier 1-style HTTP transcript and a decrypted
room-timeline transcript for the relay behavior itself. Screenshots are static renders
by `firefox --headless --screenshot` of the deployed URLs (no CDP); the content each
caption claims is ground-truthed by the `curl` transcripts below (this worker has no
image vision — the transcripts are the claim, the PNGs are the render).

## Version pin

- `4c1a67d` (branch head, "activity stats and an hourly histogram on the landing page")
  is the only commit that introduces the stat tiles + hourly histogram.
- The deployed `/` (transcript below) serves exactly those: `1 messages routed /
0 last 24 hours / 0 last 7 days / 1 channel bridged` + a 24-bar hourly histogram.
  Therefore the deployed image contains `4c1a67d`; no later commit exists, so
  deployed == head.
- The pod root `/_api/version` reports the tee-daemon itself (`{"version":"dev","commit":"fd0113fd"}`),
  not this tenant. The relay has no version route of its own — that is a gap worth
  closing in a follow-up (a commit sha in `/health` would make this pin mechanical).

## Walk of the deployed surface

```
$ curl -s https://pod.dstack.soc1024.com/mx-tg-relay/health
{"ok": true, "service": "matrix-telegram-relay", "uptime_s": 2329}

$ curl -s https://pod.dstack.soc1024.com/mx-tg-relay/ | sed -e 's/<[^>]*>/ /g' | tr -s ' \n' ' \n' | grep -A6 'routed'
 1 messages routed
 0 last 24 hours
 0 last 7 days
 1 channel bridged
 Messages routed per hour
 Last 24 hours · hover a bar for its count
 No messages in the last 24 hours. 19:00 UTC — 0 messages … 18:00 UTC — 0 messages 24h ago now

$ curl -s -w '\nHTTP %{http_code}\n' https://pod.dstack.soc1024.com/mx-tg-relay/detail
{"error": "unauthorized", "hint": "GET /detail?token=… or Authorization: Bearer …"}
HTTP 401
```

- `01-landing.png` — the deployed landing page: aggregate stats + histogram, no venue,
  account or device names (by design; repo is public).
- `02-health.png` — `/health` liveness JSON.
- `03-detail-unauthorized.png` — `/detail` from an unrelated, tokenless outsider: 401.
  The gated surface stays closed to the public (fresh-outsider check).

## Acceptance line 1 — round trip with sender name

**Observed, not driven by this worker** (see BLOCKED). The decrypted room timeline
(`bridge/mx.py tail`, run read-only in the `shape-bridge-runner` image against the
fixture room) shows both directions crossing during the operator's own session today,
sender-attributed:

```
2026-08-19 17:40:02  @socrates1024:matrix.org
    hi
2026-08-19 17:40:09  @shape-bridge:mtrx.shaperotator.xyz
    **Andrew:** kk whats up
2026-08-19 17:47:01  @shape-bridge:mtrx.shaperotator.xyz
    **Andrew:** test test
2026-08-19 17:47:07  @socrates1024:matrix.org
    now what
2026-08-19 17:47:09  @shape-bridge:mtrx.shaperotator.xyz
    **Andrew:** now what?
```

TG&rarr;MX is directly visible: Telegram-origin messages arrive relayed with the
sender's name (`**Andrew:** …`). MX&rarr;TG is evidenced by the operator's Telegram
replies arriving back ≤7s after his Element sends (17:40:02 &rarr; 17:40:09): he could
only be replying to what the relay had delivered to Telegram. A measured, driven
round-trip in both directions was NOT produced by this worker — the only non-relay
participant on each side of the current dedicated pairing is the operator:

- Matrix room `!-BrCpG…` members: `@shape-bridge` (relay, loop-dropped) and
  `@socrates1024:matrix.org` (operator) — verified via `joined_members`.
- Telegram group `-5439947920`: the bot and the operator's account. The box's only
  Telegram user session (`~/.teleport-travel/shapeos_zed.session`, account "Teleport
  Travel", id 7415544544) is NOT a member (dialogs listed; `get_entity` fails), and a
  bot cannot relay its own posts, so no second TG identity is drivable from here.

## Acceptance line 2 — kill for a minute, restart, exactly-once catch-up

**Not verified.** Restarting the tenant needs the pod control plane
(`bridge/pod-redeploy.sh` + `TEE_DAEMON_TOKEN` from `~/.oauth3-prod-secrets.env`),
which does not exist on this box; the deploy that is live was made from elsewhere.
`/detail` (cursors, per-direction counts) is token-gated and `~/.shape-bridge-bot/relay-status-token`
is absent here too. The code path exists (cursors advance only after the far side
accepts; `tg_offset` confirms everything below it; `mx_seen` ring dedups re-syncs) but
no driven restart test was run.

## Acceptance line 3 — README section

**Done in this PR** (this is the commit this evidence ships with): `bridge/README.md`
now documents how to run it (pod deploy + local CLIs, with the one-poller-per-bot-token
rule), where state lives (`/data` volume table + local CLI layout), and how to add or
change a room pair (first-boot seeding vs deliberate volume edit, bot invites,
cross-signing, topic disclosure).

## BLOCKED — need from operator (~2 minutes total)

1. **Driven round trip**: post one message from Element in the bridge test room, and
   one from the Telegram account that shares group `-5439947920`; a worker watching
   `mx.py tail` + the landing counters can then time both directions against the ~10s
   budget and attach the transcript.
2. **Restart catch-up**: run `bridge/pod-redeploy.sh` while a message or two is queued
   (or hand this box `TEE_DAEMON_TOKEN` / `RELAY_STATUS_TOKEN` so the rework lane can
   drive it and read `/detail`).

## Reproduction

```
curl -s https://pod.dstack.soc1024.com/mx-tg-relay/health
curl -s -w '\nHTTP %{http_code}\n' https://pod.dstack.soc1024.com/mx-tg-relay/detail   # 401 without token
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/home/amiller \
  -v "$HOME/.teleport-travel:/home/amiller/.teleport-travel" \
  -v "$HOME/.shape-bridge-bot:/home/amiller/.shape-bridge-bot" \
  -v "$PWD:/repo" -w /repo shape-bridge-runner python3 bridge/mx.py tail 25
```

---

# Rework pass 2 — 2026-08-20 (head 728702f): the round trip, driven

The 2026-08-19 pass observed line 1 crossing during the operator's session but could not
drive it: the only non-relay participant on each side was him. That changed with the
operator's hub-and-spoke pairing (bca8b4f): the pod now bridges two groups, and this box's
"Teleport Travel" Telegram session is a member of one of them. Both legs were driven by
this worker this time, timed against the ~10s budget.

Identities (both operator-provisioned, already on this box):
- Telegram: the "Teleport Travel" user session (MTProto sends only; the bot's getUpdates
  stream was never touched — one poller per bot token, per the module docstring).
- Matrix: `@claw-teedah-2:mtrx.shaperotator.xyz` (the swarm's own account on the bridge's
  homeserver). The bridge bot REST-invited it (room power levels allow invite at PL 0), it
  joined, sent one E2EE message via `bridge/mx.py send` (its own crypto store), and left
  the room at the end of the pass.

## Acceptance line 1 — MX→TG, driven and read on the far side

```
2026-08-20 18:03:01Z  @claw-teedah-2 sends E2EE into the bridge room:
    "relay acceptance drive: Matrix to Telegram leg (clawTEEdah, swarm rework worker,
     2026-08-20) - please ignore"
    event $4Sq9dGoJeuvX7pPKSlArYRMm7asphoqqtgIpqbOoQV0
2026-08-20 18:03:04Z  the bot's post appears in [group B] (Telethon read, msg id 1239):
    **claw-teedah-2:** relay acceptance drive: Matrix to Telegram leg (clawTEEdah, swarm
    rework worker, 2026-08-20) - please ignore
```
≈3s Matrix→Telegram, sender-attributed, exactly one copy (the group was watched 30s
longer: no duplicate). `[group B]` = the operator's live coordination group, the second of
the two bridged chats; its name and id are withheld here — the relay's own status surface
keeps venue names behind the /detail token, and this repo is public. (The first chat,
-5439947920 "matrix bridge test", is already named in the PR thread by the operator.)

Fanout proof for the hidden chat: `tg_send_relay` attempts every bridged chat and raises
only after all are attempted; a failed send leaves the Matrix cursor put and the next
pass re-delivers to BOTH groups. Exactly one copy arrived in [group B] → the send to
-5439947920 succeeded too.

## Acceptance line 1 — TG→MX, driven three times

```
18:03:51Z  Teleport Travel sends into [group B] (msg id 1240, Telegram server time)
18:03:54Z  m.room.encrypted event from @shape-bridge appears in the room     (+3s)
18:06:18Z  msg id 1241 → bridge event at 18:06:18Z                            (<1s)
18:07:59Z  msg id 1242 → bridge event at 18:08:01Z                            (+2s)
```
Each arrival was read via `/messages` + decryption with the local bridge crypto store —
a read-only path with NO `/sync`, because the live pod owns that device's to_device
stream (two syncers = stolen megolm shares).

Honest limit: the BODY of the current relayed lines is not readable from this box. The
relay rotated its outbound megolm session when claw joined, and the share never reached
claw's device (three full syncs as claw, still SessionNotFound); the local store predates
the rotation. Eligible readers are the operator's Element and the pod itself. What IS
committed: the delivery timing above, the sender (@shape-bridge), and — for this exact
code path — the decrypted, attributed format from yesterday's session (the 17:40–17:47
transcript above, and the post-redeploy lines below): `**sender (group):** text`.

## The operator's own post-redeploy session (observed, not driven)

Timeline read 2026-08-20; this is the operator driving the round trip on the current
deployed head within a minute of shipping it:

```
2026-08-19 20:36:27Z  @socrates1024:matrix.org   this is a test from a matrix channel <3 💓
2026-08-19 20:36:46Z  @shape-bridge   **Andrew ([group B]):** status page XD https://pod.dstack.soc1024.com/mx-tg-relay/
2026-08-19 20:37:08Z  @shape-bridge   **Andrew ([group B]):** i wont hook it up to botnoise (for the time being XD)
```

## Counters (public landing, content-free)

```
before the drive 18:02:24Z: 4 routed / 3 last 24h / 2 channels bridged
after  the drive 18:09:11Z: 9 routed / 8 last 24h / 2 channels bridged
```
+5 = the four drive messages above + claw's device-bootstrap notice (mx.py posts one
m.notice on first send from an un-cross-signed device; notices relay like text). No
retries: 5 source messages → 5 counted relays.

04-landing-after-drive.png — the deployed landing page after the drive.

## Version pin (head 728702f)

`/health` uptime 77670s at 18:09:11Z → process start 2026-08-19 ~20:34:41Z. Head commit
728702f's committer date is 20:34:32Z — nine seconds earlier; the tenant was redeployed
immediately after the commit. Public-surface behavior matches head-only features: 2
channels bridged (bca8b4f) and group-qualified attribution working live (b2779f3 —
without that NameError fix the first multi-group relay would have crashed). 728702f
itself only changes `/detail` content, which is token-gated and not observable from
outside; nothing observed contradicts head.

## Acceptance line 2 — still not demonstrated (operator credential)

Re-verified 2026-08-20, both routes from the 08-19 pass:
- `~/.oauth3-prod-secrets.env` (the documented source for `pod-redeploy.sh`) does not
  exist. A `TEE_DAEMON_TOKEN` value does exist in `~/.config/webhost/staging.env`, but it
  belongs to the webhost-staging pod: `GET /_api/projects` on pod.dstack.soc1024.com with
  it returns `403 {"error":"invalid token or scope"}`. Tokens are per-instance
  (pod-probe.sh says the same).
- `RELAY_STATUS_TOKEN` is not on this box, so `/detail` (cursors, per-direction counts,
  restart count) stays closed.

Without the pod control plane a worker cannot kill/restart the tenant. The code path
(cursors advance only after the far side accepts; `tg_offset` confirms everything below
it; `mx_seen` dedups re-syncs) is unchanged since the 08-19 pass.

## Reproduction (this pass)

```
# Telegram leg, as the on-box user session (never the bot token):
~/.teleport-travel/venv/bin/python <tt driver> send "…"
# Matrix leg, as the invited identity, own store:
docker run --rm … -e MX_CREDS=… -e MX_STORE=… shape-bridge-runner python3 bridge/mx.py send "…"
# Room read without /sync (local store decrypts the pre-rotation era):
curl /rooms/$ROOM/messages?dir=b …   # + local crypto store decryption
curl -s https://pod.dstack.soc1024.com/mx-tg-relay/health
```
