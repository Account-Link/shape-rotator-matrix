# Security notes

## Matrix admin commands

The knock-approver accepts `!mint`, `!codes`, and `!revoke` only from an
encrypted admin-room message whose sender device is cross-signed.  The
mautrix trust chain must be intact:

`device signing key → self-signing key → master key`

The bot requires `CROSS_SIGNED_TOFU`.  This means the sender's master key was
seen before and has not rotated.  A newly added or rotated device is refused
until the operator verifies it and reprovisions the bot's persisted crypto
store.  Room power level (or `ADMIN_ALLOWLIST`) is an authorization gate after
this cryptographic gate, never a replacement for it.

Cleartext admin-room messages are refused.  Homeserver signatures and room
power levels do not prove that a message came from the human who appears in
the event, so the admin room must remain E2EE-enabled.

This protects against a homeserver forging admin events.  It does not protect
against compromise of the operator's Matrix master/recovery key, the bot's
crypto store, or a verified operator device.

Hermes shape-rotator commands are a separate deployment and are not covered
by this repository's approver check; they require the corresponding hermes
agent change before that surface can claim the same guarantee.
