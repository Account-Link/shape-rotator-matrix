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
