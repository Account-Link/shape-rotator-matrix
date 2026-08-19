#!/usr/bin/env python3
"""bridge/relay.py — bidirectional Matrix<->Telegram relay (issue #52, #49).

Long-running loop that mirrors plain-text messages between the ONE Matrix room
and the ONE Telegram group named in ~/.teleport-travel/test-fixtures.json.
Built on bridge/mx.py (E2EE Matrix) and bridge/tg.py (Telegram Bot API) — the
Matrix device is expected to already be bootstrapped (`mx.py send` once, so
`store/.woke` exists and Element has shared megolm keys to the device).

Mirroring format (relay-bot style, no puppeting):

    <sender>: <text>       (sender bolded on each side's native markup)

v1 scope (per #49): plain text only. Media is replaced with a placeholder;
edits, deletes, threads, and reactions are ignored (not relayed).

Loop prevention is asymmetric, because the two platforms differ:

  - Telegram: NONE NEEDED. The Bot API never delivers a bot its own messages
    in a group, so what the relay posts cannot come back. Verified live
    2026-08-19 (send, then poll: zero updates).
  - Matrix:   messages whose sender is the bridge bot mxid (from creds.json)
    are dropped, since Matrix does echo them back through /sync.

Content-prefix matching is intentionally NOT used on either side (it is
fragile against users literally named like the relay, and against edited
bodies).

Durable cursors (~/.teleport-travel/relay-state.json) — restart-safe, no
dupes, catch-up after downtime:

  - tg_offset:       next getUpdates offset (one past the last CONFIRMED
                     update_id). Sending it to Telegram is what acknowledges
                     everything below it. Telegram retains unconfirmed updates
                     for ~24h, which is the real bound on catch-up after
                     downtime — longer outages lose the gap.
  - mx_seen:         bounded ring of recently-processed Matrix event_ids
                     (dedup safety net across re-syncs / cursor rewinds).

plus the relay's OWN /sync cursor at
``~/.shape-bridge-bot/store/relay_next_batch``: absent on first start ->
advance to "now" without relaying (skip backlog, same as tg_offset).

Both cursors advance only AFTER the far side accepts the message (#71), so a
delivery failure is retried rather than swallowed.

The Matrix /sync cursor lives in its OWN file
(``~/.shape-bridge-bot/store/relay_next_batch``), NOT mx.py's
``store/next_batch`` — so running the ``mx.py`` debug CLI between relay
restarts cannot advance the relay's bookmark and drop events. Both share
``crypto.db`` (the OlmMachine identity); only the cursor is split.

HARD RULE (identical to mx.py / tg.py): only the fixture pair from
test-fixtures.json is ever touched. Production pairing is an operator-only
config change — there is no flag to override either target.

Usage:
    python3 bridge/relay.py            # run until SIGTERM / SIGINT / timeout
    python3 bridge/relay.py --once     # one poll pass on each side, then exit
"""
import argparse
import asyncio
import hmac
import html
import json
import os
import signal
import sys
import time
from pathlib import Path

# Sibling modules — bridge/ is sys.path[0] when this file is run as a script.
# Also insert it explicitly so the module imports cleanly under `import
# bridge.relay` (tests / packaging) rather than only as a bare __main__.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mx import (
    STORE_DIR as MX_STORE_DIR,
    _shutdown as mx_shutdown,
    ensure_ready as mx_ensure_ready,
    load_config as mx_load_config,
    make_client as mx_make_client,
)
from tg import (
    _sender_name as tg_sender_name,
    get_updates as tg_get_updates,
    load_chat_ids as tg_load_chat_ids,
    make_client as tg_make_client,
    relay_html as tg_relay_html,
    send_message as tg_send_message,
)
import aiohttp
from mautrix.types import (
    EncryptedEvent,
    Event,
    EventType,
    Format,
    MessageType,
    TextMessageEventContent,
    UserID,
)

TELEPORT_DIR = Path(os.environ.get("TELEPORT_DIR", Path.home() / ".teleport-travel"))
STATE_PATH = Path(os.environ.get("RELAY_STATE", TELEPORT_DIR / "relay-state.json"))
# The relay's OWN /sync cursor — independent of mx.py's `store/next_batch` so
# that running the debug CLI between daemon restarts can't advance the relay's
# bookmark and silently drop events. See mx.make_client(sync_cursor_path=...).
RELAY_SYNC_CURSOR = Path(
    os.environ.get("MX_RELAY_CURSOR", str(MX_STORE_DIR / "relay_next_batch"))
)

# /sync long-poll timeout (ms). The server holds the connection this long.
MX_SYNC_TIMEOUT = 30_000
# Backoff (s) after a FAILED Telegram poll only. getUpdates long-polls, so the
# success path re-polls immediately — a fixed cadence here would just add
# latency to every bridged message.
TG_ERROR_BACKOFF = 3.0
# Bounds for one catch-up pass (getUpdates caps `limit` at 100).
TG_POLL_LIMIT = 100
# How many Matrix event_ids to remember for dedup.
MX_SEEN_LIMIT = 500
# Hours of activity history retained (8 days, so a 7-day window is always full).
BUCKET_RETENTION_H = 24 * 8

# v1 media placeholders (no re-upload yet).
_TG_MEDIA_PLACEHOLDER = {
    "photo": "[image posted in Telegram]",
    "document": "[file posted in Telegram]",
    "video": "[video posted in Telegram]",
    "sticker": "[sticker posted in Telegram]",
    "voice": "[voice message posted in Telegram]",
    "audio": "[audio posted in Telegram]",
    # Bot API field name for a GIF is `animation`, not `gif`.
    "animation": "[gif posted in Telegram]",
    "video_note": "[video note posted in Telegram]",
}


def log(side: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {side}: {msg}", flush=True)


class State:
    """Durable relay cursors. Persisted atomically after every relay so a kill
    between messages cannot double-relay."""

    def __init__(self, tg_offset: int, mx_seen, stats=None):
        # Next getUpdates offset: one past the highest update_id confirmed.
        # Sending it back to Telegram is what durably acknowledges everything
        # below it, so this is the Telegram-side restart cursor.
        self.tg_offset = tg_offset
        self.mx_seen = list(mx_seen)
        # Lifetime counters, persisted alongside the cursors so a restart or a
        # redeploy doesn't reset the picture of what this relay has carried.
        s = dict(stats or {})
        # Hourly activity buckets {hour_epoch: count}. Aggregate counts only —
        # no sender, no content, no per-message timestamps. Enough to show the
        # service is used without describing who said what to whom.
        self.buckets = {int(k): int(v) for k, v in (s.get("buckets") or {}).items()}
        self.unconfigured = dict(s.get("unconfigured") or {})
        self.stats = {
            "tg_to_mx": int(s.get("tg_to_mx", 0)),
            "mx_to_tg": int(s.get("mx_to_tg", 0)),
            "first_start": s.get("first_start") or int(time.time()),
            "last_relay": s.get("last_relay"),
            "restarts": int(s.get("restarts", 0)),
        }

    def note_unconfigured(self, chat: dict) -> None:
        """Remember a chat the bot is in but the fixtures do not name.

        Discovery aid: a new group's id is otherwise unknowable, since the relay
        consumes and drops its updates and a bot cannot read history."""
        cid = str(chat["id"])
        prev = self.unconfigured.get(cid) or {}
        self.unconfigured[cid] = {
            "id": chat["id"], "title": chat.get("title"), "type": chat.get("type"),
            "first_seen": prev.get("first_seen") or int(time.time()),
            "last_seen": int(time.time()),
            "messages": int(prev.get("messages", 0)) + 1,
        }
        if len(self.unconfigured) > 20:  # bounded; oldest sighting drops out
            oldest = min(self.unconfigured, key=lambda k: self.unconfigured[k]["last_seen"])
            del self.unconfigured[oldest]

    def count(self, direction: str) -> None:
        now = int(time.time())
        self.stats[direction] = self.stats.get(direction, 0) + 1
        self.stats["last_relay"] = now
        hour = now // 3600
        self.buckets[hour] = self.buckets.get(hour, 0) + 1
        # Keep a little more than the 7-day window so it is always complete.
        cutoff = hour - BUCKET_RETENTION_H
        for h in [h for h in self.buckets if h < cutoff]:
            del self.buckets[h]

    @classmethod
    def load(cls) -> "State":
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text())
            except (json.JSONDecodeError, TypeError) as e:
                raise SystemExit(f"relay state {STATE_PATH} corrupt: {e}")
            try:
                return cls(
                    tg_offset=int(data.get("tg_offset", 0)),
                    mx_seen=list(data.get("mx_seen", [])),
                    stats=data.get("stats") or {},
                )
            except (TypeError, ValueError) as e:
                raise SystemExit(f"relay state {STATE_PATH} corrupt: {e}")
        return cls(tg_offset=0, mx_seen=[], stats={})

    def save(self) -> None:
        """Atomic write (tmp + rename) — never a half-written file on crash."""
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "tg_offset": self.tg_offset,
                "mx_seen": self.mx_seen[-MX_SEEN_LIMIT:],
                "stats": {**self.stats, "buckets": {str(k): v for k, v in self.buckets.items()},
                          "unconfigured": self.unconfigured},
            }
        )
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(payload)
        os.replace(tmp, STATE_PATH)

    def mark_mx(self, event_id: str) -> bool:
        """Record that we've processed `event_id`. Returns True if it was
        already recorded (i.e. a re-sync we should skip). Does NOT persist —
        the caller saves once after a relay / end of pass."""
        if event_id in self.mx_seen:
            return True
        self.mx_seen.append(event_id)
        if len(self.mx_seen) > MX_SEEN_LIMIT:
            del self.mx_seen[: len(self.mx_seen) - MX_SEEN_LIMIT]
        return False


def _tg_text(msg: dict) -> str | None:
    """Body to relay for a Bot API message dict, or None to skip.

    Plain text passes through; a caption carries the same intent so it is
    relayed alongside the media placeholder; media without a caption becomes a
    bare placeholder; empty service messages are skipped."""
    text = msg.get("text")
    if text:
        return text
    for key, label in _TG_MEDIA_PLACEHOLDER.items():
        if msg.get(key) is not None:
            caption = msg.get("caption")
            return f"{label} {caption}" if caption else label
    return None


def _mx_body(evt) -> str | None:
    """Body to relay for a decrypted Matrix event, or None to skip.

    text/notice pass through; emote is prefixed with '* '; media becomes a
    placeholder; edits (m.replace) and anything that isn't m.room.message are
    skipped per #49 v1 scope."""
    content = getattr(evt, "content", None)
    if content is None:
        return None
    rel = getattr(content, "relates_to", None)
    if rel is not None and getattr(rel, "rel_type", None) == "m.replace":
        return None  # edit — skipped in v1
    msgtype = getattr(content, "msgtype", None)
    body = getattr(content, "body", None)
    if msgtype in (MessageType.TEXT, MessageType.NOTICE):
        return body or ""
    if msgtype == MessageType.EMOTE:
        return f"* {body}*" if body else None
    if msgtype in (MessageType.IMAGE, MessageType.FILE, MessageType.AUDIO, MessageType.VIDEO):
        return f"[{getattr(msgtype, 'value', msgtype)} posted in Matrix]"
    return None


_mx_name_cache: dict[str, str] = {}


async def _mx_displayname(mx_client, user_id: str) -> str:
    """Human-readable sender for the relay prefix. Displayname if fetchable,
    else the mxid localpart — never raises (attribution is cosmetic)."""
    if user_id in _mx_name_cache:
        return _mx_name_cache[user_id]
    name = user_id.split(":")[0].lstrip("@") or user_id
    try:
        prof = await mx_client.get_displayname(UserID(user_id))
        dn = getattr(prof, "displayname", None)
        if dn:
            name = str(dn)
    except Exception:
        pass
    _mx_name_cache[user_id] = name
    return name


def _try_deserialize(raw):
    try:
        return Event.deserialize(raw)
    except Exception:
        return None


async def mx_send_relay(mx_client, room: str, sender: str, text: str) -> None:
    """Encrypt + send one relayed line into the Matrix room.

    `body` carries the markdown source and `formatted_body` the HTML, because
    Matrix clients render only the latter — a body-only `**sender:**` shows up
    in Element as literal asterisks, the same way the Bot API shipped literal
    asterisks before parse_mode was set."""
    await mx_client.send_message_event(
        room,
        EventType.ROOM_MESSAGE,
        TextMessageEventContent(
            msgtype=MessageType.TEXT,
            body=f"**{sender}:** {text}",
            format=Format.HTML,
            formatted_body=f"<b>{html.escape(sender)}:</b> {html.escape(text)}",
        ),
    )
    log("mx", f"-> {sender}: {text!r}")


async def tg_send_relay(tg_client, tg_chat_ids, sender: str, text: str) -> None:
    """Send one relayed line into EVERY configured Telegram group.

    Hub-and-spoke: a Matrix message fans out to all groups. Groups do not see
    each other, because a message the relay posts INTO Matrix is dropped by the
    Matrix poller as its own — so it never continues on to a sibling group.

    HTML, not Markdown: the Bot API does not auto-parse (Telethon did), and a
    body containing `_` or `*` would make Telegram reject a Markdown parse.

    A send that fails to ONE group raises after the others are attempted, so a
    single bad chat can't silently stop delivery to the rest — the caller then
    leaves the cursor put and the whole line is retried."""
    body = tg_relay_html(sender, text)
    failures = []
    for chat_id in tg_chat_ids:
        try:
            await tg_send_message(tg_client, chat_id, body)
        except Exception as exc:
            failures.append(f"{chat_id}: {type(exc).__name__}: {exc}")
    if failures:
        raise RuntimeError("; ".join(failures))
    log("tg", f"-> {len(tg_chat_ids)} chat(s) {sender}: {text!r}")


async def _mx_sync(mx_client, room: str, *, full_state: bool = False, timeout: int = MX_SYNC_TIMEOUT):
    """One /sync cycle.

    Persists next_batch every call, refreshes the joined-room set the crypto
    state store needs, and lets OlmMachine process to-device events (megolm
    key shares) by gathering handle_sync's tasks. Returns the bridge room's
    raw timeline events (oldest-first) for the caller to relay."""
    since = None if full_state else await mx_client.sync_store.get_next_batch()
    data = await mx_client.sync(since=since, timeout=timeout, full_state=full_state)
    if not isinstance(data, dict):
        return []
    nb = data.get("next_batch")
    if nb:
        await mx_client.sync_store.put_next_batch(nb)
    mx_client._mx_state._joined.clear()
    mx_client._mx_state._joined.update(data.get("rooms", {}).get("join", {}).keys())
    tasks = mx_client.handle_sync(data)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return (
        data.get("rooms", {})
        .get("join", {})
        .get(room, {})
        .get("timeline", {})
        .get("events", [])
    )


async def mx_relay_once(mx_client, room, bot_mxid, tg_client, tg_chat_ids, state, *, timeout=MX_SYNC_TIMEOUT):
    """Sync once and relay every new non-self Matrix message in the bridge
    room to Telegram. State is saved after each relay so a kill never
    double-delivers."""
    timeline = await _mx_sync(mx_client, room, timeout=timeout)
    for raw in timeline:
        evt = _try_deserialize(raw)
        if evt is None:
            continue
        event_id = getattr(evt, "event_id", None)
        if not event_id:
            continue
        sender = str(getattr(evt, "sender", ""))
        # Loop prevention: drop our own bridge-bot posts (mark seen so a
        # re-sync doesn't re-evaluate them).
        if sender == bot_mxid:
            state.mark_mx(event_id)
            log("mx", f"skip own echo {event_id}")
            continue
        if evt.type == EventType.ROOM_ENCRYPTED and isinstance(evt, EncryptedEvent):
            try:
                evt = await mx_client.crypto.decrypt_megolm_event(evt)
            except Exception as exc:
                # Mark seen so we don't retry forever; log honestly, never mask.
                state.mark_mx(event_id)
                state.save()
                log("mx", f"undecryptable {event_id}: {type(exc).__name__}: {exc}")
                continue
        if evt.type != EventType.ROOM_MESSAGE:
            state.mark_mx(event_id)
            continue
        if event_id in state.mx_seen:
            continue  # already relayed (dedup)
        body = _mx_body(evt)
        if body is None:
            continue  # edit / reaction / redaction / unsupported — v1 skips
        name = await _mx_displayname(mx_client, sender)
        try:
            await tg_send_relay(tg_client, tg_chat_ids, name, body)
        except Exception as exc:
            log("tg", f"send failed for {event_id}: {type(exc).__name__}: {exc}")
            # Do not mark the event until delivery succeeds. The next pass
            # must retry a failed send, including after a process restart.
            continue
        state.mark_mx(event_id)
        state.count("mx_to_tg")
        state.save()
    state.save()


async def tg_poll_once(tg_client, tg_chat_ids, chat_titles, mx_client, room, state):
    """Long-poll getUpdates once and relay every new Telegram message to Matrix.

    No loop guard is needed here: the Bot API never delivers a bot its own
    messages in a group, so what the relay posts cannot come back (verified
    live 2026-08-19). If Telegram ever changes that, the relayed line would
    echo, so the sender id is logged rather than silently trusted.

    Cursor discipline mirrors mx_relay_once (#71): tg_offset only advances PAST
    a message once Matrix has accepted it. A failed Matrix send leaves the
    offset behind, so Telegram redelivers the update on the next poll and the
    message is retried — including across a restart."""
    updates = await tg_get_updates(
        tg_client, offset=state.tg_offset, chat_ids=tg_chat_ids, limit=TG_POLL_LIMIT
    )
    if not updates:
        return
    # First-ever start: confirm the backlog without relaying it, so a fresh
    # deploy never dumps Telegram history into the Matrix room.
    if state.tg_offset == 0:
        state.tg_offset = updates[-1][0] + 1
        state.save()
        log("tg", f"first start: offset set to {state.tg_offset}, backlog skipped")
        return
    for update_id, msg, seen in updates:  # getUpdates returns oldest -> newest
        if msg is None:  # another chat, or a non-message update — step over it
            if seen and seen.get("id") is not None:
                state.note_unconfigured(seen)
            state.tg_offset = max(state.tg_offset, update_id + 1)
            continue
        text = _tg_text(msg)
        if text is None:
            state.tg_offset = max(state.tg_offset, update_id + 1)
            continue
        sender = tg_sender_name(msg)
        # With several groups feeding one room, "Andrew: hi" is ambiguous —
        # Matrix readers cannot tell which group it came from. Qualify the
        # sender only when that ambiguity actually exists.
        if len(tg_chat_ids) > 1:
            origin = (msg.get("chat") or {}).get("id")
            title = chat_titles.get(origin) or str(origin)
            sender = f"{sender} ({title})"
        try:
            await mx_send_relay(mx_client, room, sender, text)
        except Exception as exc:
            log("mx", f"send failed for tg update={update_id}: {type(exc).__name__}: {exc}")
            break  # stop the pass; offset stays put so Telegram redelivers
        state.tg_offset = max(state.tg_offset, update_id + 1)
        state.count("tg_to_mx")
        state.save()
    state.save()


# ---------------------------------------------------------------------------
# HTTP surface. Two audiences, one listener.
#
# The pod's ingress proxies this path-based with NO auth in front of it, so "/"
# and "/health" are effectively world-readable. They therefore say nothing about
# WHICH venues are bridged, WHO the bot is, or how much traffic there is —
# message counts are metadata about a private room. Everything identifying sits
# behind RELAY_STATUS_TOKEN on /detail.
# ---------------------------------------------------------------------------

def _ago(ts) -> str:
    if not ts:
        return "never"
    d = max(0, int(time.time()) - int(ts))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if d >= size:
            return f"{d // size}{unit} ago"
    return f"{d}s ago"


def public_summary(state, started_at) -> dict:
    """Liveness only. No venue, account, device, or traffic volume."""
    return {"ok": True, "service": "matrix-telegram-relay",
            "uptime_s": int(time.time() - started_at)}


def detail_summary(state, started_at, room, tg_chat_ids, me, bot_mxid,
                   device_id, chat_titles=None) -> dict:
    st = state.stats
    return {
        "ok": True,
        "uptime_s": int(time.time() - started_at),
        "topology": "hub-and-spoke: every group mirrors with the room; groups do not see each other",
        "channels": [{
            "matrix_room": room,
            "telegram_chat": cid,
            "telegram_title": (chat_titles or {}).get(cid),
            "direction": "bidirectional",
            "scope": "plain text; media relayed as a placeholder",
        } for cid in tg_chat_ids],
        "identities": {
            "matrix_user": bot_mxid,
            "matrix_device": device_id,
            "telegram_bot": "@" + me["username"],
            "telegram_bot_id": me["id"],
        },
        "relayed": {
            "telegram_to_matrix": st["tg_to_mx"],
            "matrix_to_telegram": st["mx_to_tg"],
            "total": st["tg_to_mx"] + st["mx_to_tg"],
        },
        "cursors": {"tg_offset": state.tg_offset, "mx_seen": len(state.mx_seen)},
        "unconfigured_chats_seen": sorted(
            state.unconfigured.values(), key=lambda c: -c["last_seen"]),
        "first_start": st["first_start"],
        "last_relay": st["last_relay"],
        "last_relay_ago": _ago(st["last_relay"]),
    }


_CSS = """
/* Series hue is categorical slot 1, validated against both surfaces with the
   dataviz palette validator (lightness band, chroma floor, >=3:1 contrast).
   Dark is a SELECTED step from the same ramp, not an automatic flip. */
:root{--bg:#fcfcfb;--fg:#1a1a19;--mut:#6b6b66;--line:#e4e4e0;--card:#fff;--ok:#0f7a52;
--series-1:#2a78d6;--axis:#d8d8d4;--zero:#c9c9c4}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#16161a;--fg:#ececf0;--mut:#9a9aa4;--line:#2c2c33;--card:#1e1e24;--ok:#4ade9f;
--series-1:#3987e5;--axis:#3a3a42;--zero:#4a4a52}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:44rem;margin:0 auto;padding:3rem 1.25rem 4rem}
h1{font-size:1.5rem;margin:0 0 .25rem;letter-spacing:-.01em}
.sub{color:var(--mut);margin:0 0 2rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1.1rem 1.25rem;margin:0 0 1rem}
.pill{display:inline-flex;align-items:center;gap:.45rem;font-weight:600;color:var(--ok)}
.dot{width:.5rem;height:.5rem;border-radius:50%;background:var(--ok)}
dl{display:grid;grid-template-columns:auto 1fr;gap:.4rem 1.25rem;margin:0}
dt{color:var(--mut)}dd{margin:0;overflow-wrap:anywhere}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em}
.flow{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;color:var(--mut)}
.node{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:.3rem .6rem;color:var(--fg);font-weight:600}
footer{color:var(--mut);font-size:.85rem;margin-top:2rem;border-top:1px solid var(--line);padding-top:1rem}
a{color:inherit}
h2{font-size:.95rem;margin:0 0 .15rem;font-weight:600}
.cap{color:var(--mut);font-size:.85rem;margin:0 0 .9rem}
.empty{color:var(--mut);font-size:.85rem;margin:0 0 .5rem}
/* Stat tiles: the headline numbers are the point, so they get hero weight and
   the label recedes. Text wears text tokens, never the series color. */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr));gap:.75rem;margin:0 0 1rem}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.9rem 1rem}
.tile .n{display:block;font-size:1.6rem;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.tile .k{display:block;color:var(--mut);font-size:.8rem;margin-top:.15rem}
svg{display:block;overflow:visible}
.tick{fill:var(--mut);font-size:10px}
table{border-collapse:collapse;width:100%;margin-top:.5rem;font-size:.9rem}
th{text-align:left;color:var(--mut);font-weight:500;padding:.35rem .6rem .35rem 0;border-bottom:1px solid var(--line)}
td{padding:.35rem .6rem .35rem 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{_CSS}</style></head>"
            f"<body><div class=wrap>{body}</div></body></html>")


def window_total(state, hours: int) -> int:
    """Messages routed in the last `hours`, from the hourly buckets."""
    now_h = int(time.time()) // 3600
    return sum(c for h, c in state.buckets.items() if h > now_h - hours)


def hourly_series(state, hours: int = 24):
    """[(hour_epoch, count)] for the last `hours`, oldest first, gaps as 0."""
    now_h = int(time.time()) // 3600
    return [(h, state.buckets.get(h, 0)) for h in range(now_h - hours + 1, now_h + 1)]


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """Column with rounded top corners, square where it meets the baseline.

    marks-and-anatomy: round the DATA END only — a bar rounded at the baseline
    reads as floating and misstates where zero is."""
    r = min(r, w / 2, h)
    if h <= 0:
        return ""
    if h <= r:
        return f"M{x},{y + h}L{x},{y}L{x + w},{y}L{x + w},{y + h}Z"
    return (f"M{x},{y + h}L{x},{y + r}Q{x},{y} {x + r},{y}"
            f"L{x + w - r},{y}Q{x + w},{y} {x + w},{y + r}L{x + w},{y + h}Z")


def activity_svg(series) -> str:
    """Hourly column chart of the last 24h. One series, so no legend — the
    heading names it. Native <title> tooltips per bar (interaction.md: a bar
    chart ships per-mark hover)."""
    W, H, PAD_B = 640.0, 108.0, 16.0
    n = len(series) or 1
    slot = W / n
    gap = 2.0                      # 2px surface gap between adjacent bars
    bw = max(3.0, slot - gap)
    peak = max((c for _, c in series), default=0)
    plot_h = H - PAD_B
    bars = []
    for i, (hour, c) in enumerate(series):
        x = i * slot + gap / 2
        if peak > 0 and c > 0:
            bh = max(3.0, (c / peak) * (plot_h - 6))
        else:
            bh = 0.0
        label = time.strftime("%H:00 UTC", time.gmtime(hour * 3600))
        tip = f"{label} — {c} message{'' if c == 1 else 's'}"
        if bh > 0:
            bars.append(f'<path d="{_bar_path(x, plot_h - bh, bw, bh)}" fill="var(--series-1)">'
                        f'<title>{tip}</title></path>')
        else:
            # Zero still gets a hit target and a tick, so an empty hour is
            # legible as "nothing happened" rather than as missing data.
            bars.append(f'<rect x="{x:.1f}" y="{plot_h - 1.5:.1f}" width="{bw:.1f}" height="1.5" '
                        f'fill="var(--zero)"><title>{tip}</title></rect>')
    axis = (f'<line x1="0" y1="{plot_h:.1f}" x2="{W}" y2="{plot_h:.1f}" '
            f'stroke="var(--axis)" stroke-width="1"/>')
    # One direct label, on the peak only — it gives the chart a scale without a
    # y-axis, and marks-and-anatomy forbids a number on every bar.
    peak_label = ""
    if peak > 0:
        i = max(range(len(series)), key=lambda j: series[j][1])
        bh = max(3.0, (series[i][1] / peak) * (plot_h - 6))
        cx = i * slot + gap / 2 + bw / 2
        anchor = "start" if i < 2 else ("end" if i > n - 3 else "middle")
        peak_label = (f'<text x="{cx:.1f}" y="{plot_h - bh - 4:.1f}" class="tick" '
                      f'text-anchor="{anchor}">{peak}</text>')
    ends = (f'<text x="0" y="{H - 2:.0f}" class="tick">24h ago</text>'
            f'<text x="{W}" y="{H - 2:.0f}" class="tick" text-anchor="end">now</text>')
    return (f'<svg viewBox="0 -10 {W:.0f} {H + 10:.0f}" width="100%" height="{H + 10:.0f}" '
            f'role="img" aria-label="Messages routed per hour over the last 24 hours" '
            f'preserveAspectRatio="none">{axis}{"".join(bars)}{peak_label}{ends}</svg>')


def landing_html(state, started_at, n_channels: int = 1) -> str:
    up = int(time.time() - started_at)
    series = hourly_series(state, 24)
    d1, d7 = window_total(state, 24), window_total(state, 24 * 7)
    total = state.stats["tg_to_mx"] + state.stats["mx_to_tg"]
    peak = max((c for _, c in series), default=0)
    chart = activity_svg(series) if peak else (
        '<p class=empty>No messages in the last 24 hours.</p>' + activity_svg(series))
    body = f"""
<h1>Matrix &harr; Telegram relay</h1>
<p class=sub>A small bridge that mirrors messages between a Matrix room and {'a Telegram group' if n_channels == 1 else f'{n_channels} Telegram groups'}.</p>

<div class=tiles>
  <div class=tile><span class=n>{total:,}</span><span class=k>messages routed</span></div>
  <div class=tile><span class=n>{d1:,}</span><span class=k>last 24 hours</span></div>
  <div class=tile><span class=n>{d7:,}</span><span class=k>last 7 days</span></div>
  <div class=tile><span class=n>{n_channels}</span><span class=k>channel{'' if n_channels == 1 else 's'} bridged</span></div>
</div>

<div class=card>
  <h2>Messages routed per hour</h2>
  <p class=cap>Last 24 hours &middot; hover a bar for its count</p>
  {chart}
</div>

<div class=card>
  <p class=pill><span class=dot></span>Running &middot; up {up // 3600}h {(up % 3600) // 60}m</p>
  <p class=flow><span class=node>Matrix</span> &harr; <span class=node>relay</span> &harr; <span class=node>Telegram</span></p>
  <p>Each message posted on one side is re-posted on the other, attributed to its
  original sender. It relays plain text; images and files become a short
  placeholder rather than being re-uploaded. Edits, deletions, replies and
  reactions are not mirrored.</p>
  <p>The Matrix side is end-to-end encrypted, so the relay holds its own device
  keys and decrypts only in memory to forward. Messages are not stored &mdash; it
  keeps a position marker per side so a restart resumes without duplicating.</p>
</div>

<div class=card>
  <p><strong>Which room and group?</strong> Not shown here. The counts above are
  totals only: no senders, no content, no per-message timestamps, and nothing
  naming the venues or the bot accounts.</p>
  <p class=sub style="margin:0">Operators: <code>/detail.html?token=&hellip;</code></p>
</div>
<footer>One pairing per instance, fixed in configuration. No public endpoint can change it.</footer>
"""
    return _page("Matrix ↔ Telegram relay", body)


UNAUTH_HTML = _page("Not authorized", """
<h1>Not authorized</h1>
<p class=sub>This view needs an operator token.</p>
<div class=card><p>Append <code>?token=…</code> or send
<code>Authorization: Bearer …</code>.</p></div>
<p><a href="./">&larr; Back</a></p>
""")


def detail_html(d: dict) -> str:
    ids = d["identities"]
    r = d["relayed"]
    up = d["uptime_s"]
    body = f"""
<h1>Relay detail</h1>
<p class=sub>Operator view &mdash; contains venue and account identifiers.</p>
<div class=card>
  <p class=pill><span class=dot></span>Running</p>
  <dl>
    <dt>Uptime</dt><dd>{up // 3600}h {(up % 3600) // 60}m</dd>
    <dt>First started</dt><dd>{_ago(d['first_start'])}</dd>
    <dt>Last relay</dt><dd>{d['last_relay_ago']}</dd>
  </dl>
</div>
<div class=card>
  <h2 style="font-size:1rem;margin:0 0 .35rem">Channels maintained</h2>
  <p class=cap>{html.escape(d.get('topology',''))}</p>
  <dl>
    <dt>Matrix room</dt><dd><code>{html.escape(str(d['channels'][0]['matrix_room']))}</code></dd>
  </dl>
  <table>
    <tr><th>Telegram group</th><th>chat id</th><th>scope</th></tr>
    {"".join(
      f"<tr><td>{html.escape(str(c.get('telegram_title') or '—'))}</td>"
      f"<td><code>{html.escape(str(c['telegram_chat']))}</code></td>"
      f"<td>{html.escape(c['scope'])}</td></tr>" for c in d['channels'])}
  </table>
</div>
<div class=card>
  <h2 style="font-size:1rem;margin:0 0 .75rem">Messages relayed</h2>
  <dl>
    <dt>Telegram &rarr; Matrix</dt><dd>{r['telegram_to_matrix']}</dd>
    <dt>Matrix &rarr; Telegram</dt><dd>{r['matrix_to_telegram']}</dd>
    <dt>Total</dt><dd><strong>{r['total']}</strong></dd>
  </dl>
</div>
<div class=card>
  <h2 style="font-size:1rem;margin:0 0 .75rem">Identities &amp; cursors</h2>
  <dl>
    <dt>Matrix user</dt><dd><code>{html.escape(str(ids['matrix_user']))}</code></dd>
    <dt>Matrix device</dt><dd><code>{html.escape(str(ids['matrix_device']))}</code></dd>
    <dt>Telegram bot</dt><dd><code>{html.escape(str(ids['telegram_bot']))}</code></dd>
    <dt>getUpdates offset</dt><dd><code>{d['cursors']['tg_offset']}</code></dd>
    <dt>Matrix dedup ring</dt><dd>{d['cursors']['mx_seen']} event ids</dd>
  </dl>
</div>
{"".join([
  '<div class=card><h2 style="font-size:1rem;margin:0 0 .35rem">Unconfigured chats seen</h2>'
  '<p class=cap>The bot is in these but the fixtures do not name them, so nothing is bridged. '
  'Add an id to <code>telegram_chat_ids</code> to bridge it.</p>'
  '<table><tr><th>group</th><th>chat id</th><th>msgs</th><th>last seen</th></tr>'
  + "".join(
      f"<tr><td>{html.escape(str(c.get('title') or '—'))}</td>"
      f"<td><code>{c['id']}</code></td><td>{c['messages']}</td>"
      f"<td>{_ago(c['last_seen'])}</td></tr>" for c in d['unconfigured_chats_seen'])
  + '</table></div>'
]) if d.get('unconfigured_chats_seen') else ""}
<p><a href="./">&larr; Public page</a></p>
"""
    return _page("Relay detail", body)


async def run(args) -> int:
    started_at = time.time()
    creds, room = mx_load_config()
    tg_chat_ids = tg_load_chat_ids()
    bot_mxid = creds["user_id"]
    state = State.load()
    state.stats["restarts"] += 1
    state.save()

    mx_client = await mx_make_client(creds, sync_cursor_path=RELAY_SYNC_CURSOR)
    tg_session = aiohttp.ClientSession()
    tg_client = tg_make_client(tg_session)

    try:
        me = await tg_client.call("getMe")
    except BaseException:
        await tg_session.close()
        raise
    if not me.get("can_read_all_group_messages"):
        await tg_session.close()
        raise SystemExit(
            f"bot @{me.get('username')} has privacy mode ENABLED — it would only see "
            "commands and replies, silently missing most group traffic. Disable it via "
            "BotFather (/setprivacy -> Disable) or make the bot a group admin."
        )

    # Human-readable group names for the relay prefix and the operator view.
    # Cosmetic only, so a getChat failure degrades the label to the numeric id
    # rather than taking down the run.
    chat_titles = {}
    for cid in tg_chat_ids:
        try:
            info = await tg_client.call("getChat", chat_id=cid)
            chat_titles[cid] = info.get("title") or info.get("username") or str(cid)
        except Exception as exc:
            log("tg", f"getChat {cid} failed ({type(exc).__name__}); using the numeric id")
            chat_titles[cid] = str(cid)
    log("tg", "bridging " + ", ".join(f"{t} ({c})" for c, t in chat_titles.items()))

    # Run the SAME bootstrap the mx.py CLI does, rather than assuming someone
    # ran it first. In a container there is no operator to run `mx.py send`
    # beforehand: the device is minted at boot, so if the relay only called
    # share_keys() the device would never be cross-signed (yellow shield in
    # Element) and would never post the wake message that makes other clients
    # share megolm keys to it. Both are idempotent and marker-guarded.
    await mx_ensure_ready(mx_client, room)

    log(
        "relay",
        f"start room={room} tg_chats={tg_chat_ids} bot={bot_mxid} tg_bot=@{me['username']}",
    )

    # First-ever Matrix start: the relay keeps its OWN /sync cursor
    # (RELAY_SYNC_CURSOR), separate from mx.py's. If that cursor file doesn't
    # exist yet, advance it to "now" without relaying — mirroring the
    # Telegram first-start skip. Tied to the cursor file (not a flag) so it
    # self-heals if the file is deleted.
    if await mx_client.sync_store.get_next_batch() is None:
        log("mx", "first start: advancing relay sync cursor to now (backlog skipped)")
        await _mx_sync(mx_client, room, timeout=0)

    if args.once:
        # One pass each side with a short MX window, then a clean exit.
        try:
            await tg_poll_once(tg_client, tg_chat_ids, chat_titles, mx_client, room, state)
            await mx_relay_once(
                mx_client, room, bot_mxid, tg_client, tg_chat_ids, state, timeout=2000
            )
        finally:
            state.save()
            await tg_session.close()
            await mx_shutdown(mx_client)
            log("relay", "--once complete")
        return 0

    stop = asyncio.Event()

    def _stop(*_):
        if not stop.is_set():
            log("relay", "stop signal received")
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            # e.g. Windows — fall back to KeyboardInterrupt handling.
            pass

    async def tg_loop():
        while not stop.is_set():
            try:
                # getUpdates long-polls, so this blocks until traffic arrives or
                # the poll window closes. Re-poll immediately on success; only
                # back off after a failure.
                await tg_poll_once(tg_client, tg_chat_ids, chat_titles, mx_client, room, state)
                continue
            except Exception as exc:
                log("tg", f"poll error: {type(exc).__name__}: {exc}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=TG_ERROR_BACKOFF)
            except asyncio.TimeoutError:
                pass

    async def mx_loop():
        while not stop.is_set():
            try:
                await mx_relay_once(
                    mx_client, room, bot_mxid, tg_client, tg_chat_ids, state,
                    timeout=MX_SYNC_TIMEOUT,
                )
            except Exception as exc:
                log("mx", f"sync error: {type(exc).__name__}: {exc}")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

    # The pod's ingress proxies path-based at /<name>/ and expects a listener,
    # so expose the relay's cursors rather than a bare 200 — a stuck cursor is
    # exactly what "the bridge looks up but isn't moving" looks like.
    health = None
    if os.environ.get("HEALTH_PORT"):
        from aiohttp import web

        # The pod's ingress serves this with NO authentication, so the split
        # below is a security boundary, not decoration. Anything that names a
        # venue, account, or device belongs behind the token.
        status_token = os.environ.get("RELAY_STATUS_TOKEN", "")

        def _authed(req) -> bool:
            if not status_token:
                return False  # no token configured => detail is unreachable
            supplied = req.query.get("token", "")
            auth = req.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[7:]
            return hmac.compare_digest(supplied, status_token)

        async def _index(_req):
            return web.Response(text=landing_html(state, started_at, len(tg_chat_ids)),
                                content_type="text/html")

        async def _health_json(_req):
            # Deliberately non-identifying: liveness only.
            return web.json_response(public_summary(state, started_at))

        async def _detail(req):
            if not _authed(req):
                return web.json_response(
                    {"error": "unauthorized",
                     "hint": "GET /detail?token=… or Authorization: Bearer …"},
                    status=401)
            return web.json_response(detail_summary(
                state, started_at, room, tg_chat_ids, me, bot_mxid,
                creds["device_id"], chat_titles))

        async def _detail_html(req):
            if not _authed(req):
                return web.Response(text=UNAUTH_HTML, status=401,
                                    content_type="text/html")
            return web.Response(
                text=detail_html(detail_summary(
                    state, started_at, room, tg_chat_ids, me, bot_mxid,
                    creds["device_id"], chat_titles)),
                content_type="text/html")

        app = web.Application()
        app.router.add_get("/", _index)
        app.router.add_get("/health", _health_json)
        app.router.add_get("/detail", _detail)
        app.router.add_get("/detail.html", _detail_html)
        health = web.AppRunner(app)
        await health.setup()
        port = int(os.environ["HEALTH_PORT"])
        await web.TCPSite(health, "0.0.0.0", port).start()
        log("relay", f"http listening on :{port} (detail {'gated' if status_token else 'DISABLED — no RELAY_STATUS_TOKEN'})")

    tasks = [asyncio.create_task(tg_loop()), asyncio.create_task(mx_loop())]
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if health is not None:
            await health.cleanup()
        state.save()
        await tg_session.close()
        await mx_shutdown(mx_client)
        log("relay", "stopped cleanly")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="relay.py",
        description="Bidirectional Matrix<->Telegram relay (test-fixture pair only).",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="run one poll pass on each side and exit (no long-poll loop)",
    )
    args = ap.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        print(f"relay.py: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
