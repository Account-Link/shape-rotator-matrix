# bridge/ — Matrix &harr; Telegram relay for Shape OS coordination

Three pieces, all scoped to **the single fixture pair** in
`~/.teleport-travel/test-fixtures.json` (one Matrix `matrix_room_id`, one
Telegram `telegram_chat_id`). Production pairing (different room/group) is an
operator-only config change — there is no flag to override either target, by
design.

| file | role |
| --- | --- |
| `mx.py`   | E2EE Matrix bot CLI — `send` / `tail` the test room |
| `tg.py`   | Telethon CLI — `send` / `tail` the test group |
| `relay.py`| Long-running bidirectional relay between the two |

## relay.py

```
python3 bridge/relay.py            # run until SIGTERM / SIGINT / timeout
python3 bridge/relay.py --once     # one poll pass on each side, then exit
```

Mirrors plain-text messages both ways in relay-bot style (no puppeting):

```
**<sender>:** <text>
```

v1 scope (#49): plain text only. Media becomes a placeholder; edits, deletes,
threads, and reactions are ignored.

**Loop prevention** — each side ignores messages authored by the relay's own
identity (the Telethon account on Telegram; `@shape-bridge` on Matrix). A
forwarded message lands on the far side under the relay's identity, so the
counter-poller drops it. Sender-id is the sole guard.

**Durable cursors** — `~/.teleport-travel/relay-state.json` holds:

- `tg_last_id` — highest Telegram message id processed.
- `mx_seen` — bounded ring of Matrix event_ids (dedup across re-syncs).

and the relay's OWN `/sync` cursor lives at
`~/.shape-bridge-bot/store/relay_next_batch` (separate from `mx.py`'s
`store/next_batch`, so the debug CLI can't advance the relay's bookmark).
Absent on first start &rarr; advance to "now" without relaying (skip backlog,
like the Telegram side).

State is written atomically (tmp + rename) after every relay, so a kill between
messages never double-delivers; on restart the relay catches up everything
missed during downtime. The Matrix `/sync` cursor itself lives at
`~/.shape-bridge-bot/store/next_batch` (`mx.py`'s `_FileSyncStore`); the relay
owns it while it runs.

`relay.py` imports the proven client machinery from `mx.py` (`make_client`,
`load_config`, `_shutdown`) and `tg.py` (`make_client`, `load_chat_id`,
`_sender_name`) — no duplicate E2EE / Telethon code. It expects `mx.py` to have
bootstrapped the device once already (`store/.woke` present) so Element is
sharing megolm keys to it.

## mx.py

```
python3 bridge/mx.py send "hello from the bridge"
python3 bridge/mx.py tail [N]      # default N = 10
```

End-to-end-encrypted via `mautrix-python` (`OlmMachine` + `PgCryptoStore`). The
first run bootstraps the device once:

1. creates `~/.shape-bridge-bot/store/crypto.db` (and never regenerates it under
   the same `device_id`),
2. cross-signs the account via `OlmMachine.generate_recovery_key()` and writes
   the recovery key to `~/.shape-bridge-bot/store/recovery_key.txt`,
3. posts one outgoing **wake** message after first sync so Element shares megolm
   session keys to this device (gated by `store/.woke` — fires exactly once).

Follows every non-negotiable in `MATRIX_ONBOARDING.md` ("mautrix-python known
bugs"): wraps `MemoryStateStore` with `is_encrypted` / `find_shared_rooms` /
`get_encryption_info`, persists `next_batch` every sync, gathers `handle_sync`
tasks, and uses the unsuffixed Continuwuity room id.

## Dependencies

Same image the E2EE tests already use (`tests/Dockerfile`): Python 3.11,
`libolm3`, `pip install 'mautrix[e2be]' aiosqlite asyncpg python-olm`. The relay
also needs `telethon` + `python-dotenv` (the Telethon session side).

## Credentials &amp; state (operator-provisioned — never commit)

| path | purpose |
| --- | --- |
| `~/.shape-bridge-bot/creds.json`     | `user_id`, `access_token`, `device_id`, `homeserver` |
| `~/.shape-bridge-bot/store/`         | `crypto.db` + `/sync` cursor + `.woke` marker |
| `~/.teleport-travel/shapeos_zed.session` | Telethon user session (single-host — never copied across machines) |
| `~/.teleport-travel/.env`            | `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` |
| `~/.teleport-travel/test-fixtures.json` | the ONLY `matrix_room_id` + `telegram_chat_id` bridge code may touch |
| `~/.teleport-travel/relay-state.json` | relay cursors (written by `relay.py`) |
