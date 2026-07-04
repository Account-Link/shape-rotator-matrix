# bridge/mx.py — Matrix side of the shape-rotator bridge

E2EE Matrix bot CLI for `@shape-bridge:mtrx.shaperotator.xyz`. Two operations,
both against **the single test room** named in `~/.teleport-travel/test-fixtures.json`
(`matrix_room_id`) — never any other room:

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
`libolm3`, and `pip install 'mautrix[e2be]' aiosqlite asyncpg python-olm`.

## Credentials (operator-provisioned — never commit)

| path | purpose |
| --- | --- |
| `~/.shape-bridge-bot/creds.json` | `user_id`, `access_token`, `device_id`, `homeserver` |
| `~/.shape-bridge-bot/store/` | `crypto.db` + `/sync` cursor + `.woke` marker |
| `~/.teleport-travel/test-fixtures.json` | the ONLY `matrix_room_id` this tool may touch |

Production pairing (pointing at a different room) is an operator-only config
change — there is no CLI flag to override the target room, by design.
