#!/usr/bin/env python3
"""Telegram Bot API send/tail CLI for the Matrix<->Telegram relay (issue #50).

Talks to exactly ONE Telegram chat: the group whose id lives in
`telegram_chat_id` of the test-fixtures file. That is the single source of
truth for the destination — this tool will never send anywhere else.

Credentials (provisioned on the relay host, never committed):
  - Bot token:   $TELEGRAM_BOT_TOKEN, else ~/.shape-bridge-bot/telegram-bot-token
  - Chat id:     $TEST_FIXTURES_PATH (default ~/.teleport-travel/test-fixtures.json)

Bot API, not MTProto. Consequences that shaped this file:

  - No api_id/api_hash, no session file, and no single-host rule — the token is
    the only credential and works from anywhere.
  - A bot CANNOT read history. `tail` shows PENDING updates (what the relay
    would consume next), never a backlog. Telegram retains undelivered updates
    for ~24h, which is the real bound on restart catch-up.
  - Only one getUpdates poll may be in flight per bot. Running `tail` against a
    live relay gets HTTP 409 from Telegram; that is reported, not swallowed.
  - Bots do not receive their own messages in groups, so the relay needs no
    sender-id loop guard on this side (verified live 2026-08-19).

Requires: aiohttp (already in tests/Dockerfile).

Usage:
  tg.py send <text>    send <text> to the group, print the new message id
  tg.py tail [n]       print up to n pending updates (does NOT consume them)
  tg.py whoami         print the bot identity (getMe)
"""
import argparse
import asyncio
import html
import json
import os
import sys
from pathlib import Path

import aiohttp

TELEPORT_DIR = Path(os.environ.get("TELEPORT_DIR", Path.home() / ".teleport-travel"))
FIXTURES_PATH = Path(os.environ.get("TEST_FIXTURES_PATH", TELEPORT_DIR / "test-fixtures.json"))
TOKEN_PATH = Path(
    os.environ.get("TELEGRAM_BOT_TOKEN_PATH", Path.home() / ".shape-bridge-bot/telegram-bot-token")
)
API_ROOT = os.environ.get("TELEGRAM_API_ROOT", "https://api.telegram.org")

# Long-poll seconds for getUpdates. Telegram holds the connection open until an
# update arrives or this elapses, so the relay is event-driven, not a spin loop.
POLL_TIMEOUT = int(os.environ.get("TG_POLL_TIMEOUT", "25"))


def load_token() -> str:
    """Bot token from the environment, else the provisioned file."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if tok:
        return tok.strip()
    try:
        tok = TOKEN_PATH.read_text().strip()
    except FileNotFoundError:
        raise SystemExit(f"bot token not found: set TELEGRAM_BOT_TOKEN or provision {TOKEN_PATH}")
    if not tok:
        raise SystemExit(f"bot token file {TOKEN_PATH} is empty")
    return tok


def load_chat_id() -> int:
    """Destination chat id comes ONLY from the test-fixtures file."""
    try:
        fixtures = json.loads(FIXTURES_PATH.read_text())
    except FileNotFoundError:
        raise SystemExit(f"test-fixtures not found: {FIXTURES_PATH}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"test-fixtures {FIXTURES_PATH} is not valid JSON: {e}")
    try:
        return int(fixtures["telegram_chat_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise SystemExit(f"test-fixtures {FIXTURES_PATH} missing/invalid telegram_chat_id: {e}")


class BotClient:
    """Thin aiohttp wrapper over the Bot API. Every non-ok response raises."""

    def __init__(self, token: str, session: aiohttp.ClientSession):
        self._token = token
        self._session = session

    async def call(self, method: str, timeout: int = 0, **params):
        url = f"{API_ROOT}/bot{self._token}/{method}"
        # Read timeout must outlast the server-side long poll or aiohttp aborts
        # a healthy getUpdates mid-wait.
        rt = aiohttp.ClientTimeout(total=timeout + 30)
        if timeout:
            params["timeout"] = timeout
        async with self._session.post(url, json=params, timeout=rt) as resp:
            body = await resp.json()
            if not body.get("ok"):
                raise RuntimeError(
                    f"telegram {method} failed: HTTP {resp.status} "
                    f"{body.get('error_code')} {body.get('description')!r}"
                )
            return body["result"]


def make_client(session: aiohttp.ClientSession) -> BotClient:
    return BotClient(load_token(), session)


def _sender_name(msg: dict) -> str:
    """Human-readable sender for the relay prefix.

    Falls through first_name -> username -> channel title -> numeric id ->
    "unknown" for the senderless service messages Telegram emits."""
    frm = msg.get("from") or {}
    name = frm.get("first_name") or frm.get("username")
    if name:
        return str(name)
    title = (msg.get("sender_chat") or {}).get("title")
    if title:
        return str(title)
    if frm.get("id") is not None:
        return f"id:{frm['id']}"
    return "unknown"


def check_migration(msg: dict, chat_id: int) -> None:
    """A basic group auto-upgraded to a supergroup gets a NEW chat id.

    Telegram announces it once via migrate_to_chat_id and the old id goes deaf.
    Fail loudly so the operator updates the fixtures — silently following the
    migration would send to a venue the fixtures never named."""
    new_id = msg.get("migrate_to_chat_id")
    if new_id is not None:
        raise SystemExit(
            f"telegram chat {chat_id} migrated to supergroup {new_id}; "
            f"update telegram_chat_id in {FIXTURES_PATH} (operator action)"
        )


async def get_updates(client: BotClient, offset: int, chat_id: int, limit: int = 100):
    """Long-poll for updates, returning (update_id, message) for the fixture chat.

    `offset` confirms every update below it, which is what durably advances the
    cursor. Updates from any other chat are dropped here — the bot may be added
    to other groups, but this relay only ever bridges the fixture pair. The
    update_id is still returned for those, so the caller advances past them."""
    updates = await client.call(
        "getUpdates",
        timeout=POLL_TIMEOUT,
        offset=offset,
        limit=limit,
        allowed_updates=["message"],
    )
    out = []
    for u in updates:
        msg = u.get("message")
        if msg and (msg.get("chat") or {}).get("id") == chat_id:
            check_migration(msg, chat_id)
            out.append((u["update_id"], msg))
        else:
            out.append((u["update_id"], None))
    return out


async def send_message(client: BotClient, chat_id: int, text_html: str) -> int:
    """Send pre-escaped HTML to the group, returning the new message id."""
    msg = await client.call("sendMessage", chat_id=chat_id, text=text_html, parse_mode="HTML")
    return msg["message_id"]


def relay_html(sender: str, text: str) -> str:
    """Relay-bot line: bold sender, escaped body.

    HTML rather than Markdown because a body containing `_` or `*` — entirely
    ordinary in code snippets — makes Telegram reject a Markdown parse outright."""
    return f"<b>{html.escape(sender)}:</b> {html.escape(text)}"


async def _run(action):
    async with aiohttp.ClientSession() as session:
        return await action(make_client(session), load_chat_id())


async def send(text: str) -> None:
    async def _do(client, chat_id):
        mid = await send_message(client, chat_id, html.escape(text))
        print(f"sent id={mid} chat={chat_id}")

    await _run(_do)


async def tail(n: int) -> None:
    async def _do(client, chat_id):
        # offset=0 reads pending updates WITHOUT confirming them, so the relay's
        # cursor is untouched. A live relay holds the only allowed poll slot and
        # Telegram answers with 409 — surfaced, never masked.
        pending = [(uid, m) for uid, m in await get_updates(client, 0, chat_id, n) if m]
        if not pending:
            print("(no pending updates — a bot cannot read history)")
        for update_id, msg in pending[-n:]:
            body = msg.get("text") or "(non-text message)"
            print(f"update_id={update_id} id={msg['message_id']} from={_sender_name(msg)}: {body}")

    await _run(_do)


async def whoami() -> None:
    async def _do(client, chat_id):
        me = await client.call("getMe")
        print(
            f"@{me['username']} id={me['id']} name={me.get('first_name')!r} "
            f"can_read_all_group_messages={me.get('can_read_all_group_messages')} chat={chat_id}"
        )

    await _run(_do)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tg.py",
        description="Send to / tail the one Telegram group for the relay (Bot API).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="send <text> to the group")
    p_send.add_argument("text", help="message text to send")

    p_tail = sub.add_parser("tail", help="print up to n PENDING updates (not history)")
    p_tail.add_argument("n", type=int, nargs="?", default=10, help="number of updates (default 10)")

    sub.add_parser("whoami", help="print the bot identity (getMe)")

    args = parser.parse_args(argv)
    if args.cmd == "send":
        asyncio.run(send(args.text))
    elif args.cmd == "tail":
        asyncio.run(tail(args.n))
    else:
        asyncio.run(whoami())
    return 0


if __name__ == "__main__":
    sys.exit(main())
