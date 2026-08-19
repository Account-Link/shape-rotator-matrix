# bridge/ — Matrix &harr; Telegram relay for Shape OS coordination

One `runtime: image` tenant on a tee-daemon pod (`pod.dstack.soc1024.com/mx-tg-relay/`),
mirroring plain-text messages between ONE Matrix room and the Telegram group(s) named
in the fixtures. The Telegram credential is a BotFather **bot token** — no user
session, no `api_id`/`api_hash`, works from any host.

Production pairing is an operator-only config change — there is no flag to override
either target, by design.

**Topology is hub-and-spoke.** A Matrix message fans out to every configured group, and
each group mirrors into the room, but the groups do **not** see each other: a message the
relay posts into Matrix is dropped by the Matrix poller as its own, so it never continues
on to a sibling group. When more than one group feeds the room the source group is
appended to the sender (`Andrew (Ops): hi`), since otherwise Matrix readers cannot tell
which group a line came from.

One `getUpdates` stream serves every chat the bot is in, so additional groups need no
extra polling, token, or cursor.

| file | role |
| --- | --- |
| `relay.py`    | Long-running bidirectional relay + HTTP surface |
| `mx.py`       | E2EE Matrix bot CLI — `send` / `tail` the bridged room |
| `tg.py`       | Telegram Bot API CLI — `send` / `tail` / `whoami` |
| `Dockerfile`  | the tenant image (python:3.11-slim + libolm3 + mautrix) |
| `entrypoint.sh` | seeds fixtures + bot token into `/data` on first boot |
| `deploy-pod.sh`  | build, push, deploy the tenant (sealed env carries secrets) |
| `pod-redeploy.sh` / `pod-logs.sh` / `pod-probe.sh` | pod control / logs / inventory |
| `acceptance.sh` | round-trip + restart gate (issue #53) — **assertions predate the Bot API rewrite; see Not done** |

## How to run it

Deployed (the real thing):

```
bash bridge/deploy-pod.sh
```

Builds `bridge/Dockerfile`, pushes it, and POSTs an `image`-runtime tenant manifest to
the pod daemon. Secrets are read from disk at run time and travel only in the manifest's
sealed env — never committed, never printed:

- `TEE_DAEMON_TOKEN` &larr; `~/.oauth3-prod-secrets.env`
- `TELEGRAM_BOT_TOKEN` &larr; `~/.shape-bridge-bot/telegram-bot-token`
- `MATRIX_BRIDGE_PASSWORD` &larr; `~/.shape-bridge-bot/matrix-password`
- `MATRIX_RECOVERY_KEY`, `RELAY_STATUS_TOKEN` &larr; same dir (optional but expected)

The bot mints its own Matrix `creds.json` from the password on first boot (see
`bootstrap_creds_from_password()` in `mx.py`), so no access token is ever copied to the
pod. `oci_runtime: runc` per `tee-daemon/deploy/README.md`.

Local CLIs against the same pairing (diagnostics):

```
python3 bridge/mx.py send "hello"     # as the bridge bot, E2EE
python3 bridge/mx.py tail [N]         # last N decrypted room messages
python3 bridge/tg.py send "hello"     # as the bot, Bot API
python3 bridge/tg.py whoami
```

**Never run `relay.py` or `tg.py tail` locally while the pod relay is live** — both call
`getUpdates`, and Telegram allows exactly one poller per bot token: the second caller
terminates the first (`409 Conflict`), and a local offset acknowledges updates the pod
relay has not processed yet, silently dropping them. One relay per bot token, anywhere.

## Where state lives

All tenant state is under `/data`, backed by a named volume (`mx-tg-relay-data`).
`ctx.dataDir` survives restarts but not CVM redeploys; the volume does. Losing `/data`
means a re-minted device and a lost megolm session — permanently undecryptable messages
(see MATRIX_ONBOARDING.md "Device churn leaves permanent debris").

| path (in `/data`) | purpose |
| --- | --- |
| `test-fixtures.json` | the ONLY `matrix_room_id` + `telegram_chat_ids` bridge code may touch |
| `telegram-bot-token` | BotFather token (volume copy wins after first boot) |
| `creds.json` | `user_id`, `access_token`, `device_id`, `homeserver` (minted on first boot) |
| `store/crypto.db` | OlmMachine identity — never regenerated under the same `device_id` |
| `store/relay_next_batch` | the relay's own `/sync` cursor |
| `relay-state.json` | `tg_offset` (getUpdates), `mx_seen` ring, hourly activity buckets |

The local CLI uses the same layout under `~/.shape-bridge-bot/` + `~/.teleport-travel/`
(`TELEPORT_DIR`), with its own store — separate from the pod's volume.

## How to add / change a room pair

Operator-only, in one of two ways:

- **First boot:** set `MATRIX_ROOM_ID` + `TELEGRAM_CHAT_ID` (comma-separated for several
  groups) in the deploy env; `entrypoint.sh` seeds them into `/data/test-fixtures.json`.
- **Adding or removing a GROUP later:** change `TELEGRAM_CHAT_ID` in the deploy env and
  redeploy. `entrypoint.sh` reconciles the list into the volume and logs
  `RE-PAIRED telegram chats: [...] -> [...]`. Env only changes via a deploy, so this is
  already an operator action — but it is logged loudly rather than done quietly.
- **Changing the MATRIX ROOM:** not possible this way, on purpose. A room mismatch
  between env and volume **fails the boot**, because re-pointing a live device strands
  the megolm sessions it holds. Wipe the volume to re-pair deliberately, accepting that
  the device is re-minted and cannot read the old room's history.

### Finding a new group's chat id

A bot cannot read history, and the relay consumes then discards updates from chats the
fixtures don't name — so a new group's id would otherwise be unknowable. Sightings are
recorded instead: add the bot to the group, post once, and the chat shows up under
**Unconfigured chats seen** on `/detail.html?token=…` with its id and title.

Then invite the bridge bot (`@shape-bridge:mtrx.shaperotator.xyz`) to the Matrix room
and the Telegram bot to the group, and cross-sign the new device if the store was wiped
(`MATRIX_RECOVERY_KEY`, else Element shows the yellow shield). The bridged Matrix
room's topic must say it is mirrored to Telegram (#49 trust boundary).

## HTTP surface

The pod fronts this tenant with **no auth**, so `/` and `/health` are world-readable
and say nothing that identifies the venue, accounts, or devices:

| path | auth | contents |
| --- | --- | --- |
| `/` | none | landing page: aggregate activity stats + hourly histogram, no venue/account/device |
| `/health` | none | liveness: `ok`, `service`, `uptime_s` |
| `/detail` | `RELAY_STATUS_TOKEN` | channel pairing, identities, per-direction counts, cursors |
| `/detail.html` | same | rendered |

## Relay semantics

Mirrors plain text in relay-bot style (no puppeting): `<sender>: <text>`, sender bolded
per side's native markup (HTML on Telegram via the Bot API, `formatted_body` on Matrix).
Media becomes a placeholder; edits, deletes, threads, reactions are ignored (#49 v1).

Loop prevention is asymmetric: the Bot API never delivers a bot its own group messages
(verified live — send, then poll: zero updates), so the Telegram side needs none; Matrix
`/sync` does echo, so messages from the bridge mxid are dropped.

Durable cursors: `tg_offset` is the next getUpdates offset and advances only AFTER the
far side accepts (#71) — an offset confirms everything below it, so a pass stops at the
first failed Matrix delivery instead of skipping past it. Catch-up after downtime is
bounded by Telegram's ~24h update retention.

## Not done

- `acceptance.sh` still greps for `skip own echo id=…`, which the Bot API side no
  longer emits — its assertions need rewriting for a positive round-trip. It also
  cannot run against a live pod relay (see the one-poller-per-token rule above).
