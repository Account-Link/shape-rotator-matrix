#!/usr/bin/env python3
"""Telegram send/tail CLI for the Matrix<->Telegram relay (issue #50).

Talks to exactly ONE Telegram chat: the test group whose id lives in
`telegram_chat_id` of the test-fixtures file. That is the single source of
truth for the destination — this tool will never send anywhere else.

Credentials & session (provisioned on the relay host, never committed/copied):
  - Telethon user session: $TELEGRAM_SESSION (default ~/.teleport-travel/shapeos_zed)
  - API id/hash:           $TELEPORT_DIR/.env (TELEGRAM_API_ID / TELEGRAM_API_HASH)
  - Destination chat id:   $TEST_FIXTURES_PATH (default ~/.teleport-travel/test-fixtures.json)

Single-host rule: a Telethon session used from a second IP is permanently
invalidated. This script reuses the session in place; it never copies one and
never performs an interactive re-login. If the session is not authorized, that
is an operator action — the script fails loudly instead of prompting.

Requires: telethon, python-dotenv (provided in ~/.teleport-travel/venv).

Usage:
  tg.py send <text>       send <text> to the test group, print the new msg id
  tg.py tail [n]          print the last n messages (newest last): id, sender, text
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import AuthKeyError

# Provisioned locations; overridable via env so production pairing is an
# operator config change, never a code change.
TELEPORT_DIR = Path(os.environ.get("TELEPORT_DIR", Path.home() / ".teleport-travel"))
SESSION_PATH = os.environ.get("TELEGRAM_SESSION", str(TELEPORT_DIR / "shapeos_zed"))
ENV_PATH = Path(os.environ.get("TELEGRAM_ENV_PATH", TELEPORT_DIR / ".env"))
FIXTURES_PATH = Path(os.environ.get("TEST_FIXTURES_PATH", TELEPORT_DIR / "test-fixtures.json"))


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


def make_client() -> TelegramClient:
    """Build a TelegramClient from the provisioned .env + session path."""
    load_dotenv(ENV_PATH)  # does not override vars already in the environment
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit(f"TELEGRAM_API_ID/TELEGRAM_API_HASH missing in {ENV_PATH}")
    try:
        api_id_int = int(api_id)
    except ValueError:
        raise SystemExit(f"TELEGRAM_API_ID in {ENV_PATH} is not an integer: {api_id!r}")
    return TelegramClient(SESSION_PATH, api_id_int, api_hash)


async def _run(action):
    """Connect with the provisioned session and run `action(client, chat_id)`.

    Uses connect()+is_user_authorized() rather than start(): an unauthorized
    session means the operator must re-login in place (single-host rule), which
    we never do from a CLI. Fail loudly instead of hanging on a prompt.
    """
    chat_id = load_chat_id()
    client = make_client()
    try:
        await client.connect()
        try:
            authorized = await client.is_user_authorized()
        except AuthKeyError as e:
            raise SystemExit(f"session auth key unusable ({e}); re-login is an operator action")
        if not authorized:
            raise SystemExit(
                f"session {SESSION_PATH}.session is not authorized; "
                "re-login in place on the relay host (single-host rule)"
            )
        return await action(client, chat_id)
    finally:
        await client.disconnect()


def _sender_name(sender, sender_id) -> str:
    # Falls back through readable name -> numeric id -> "unknown" for the
    # senderless service messages Telegram emits (group creation, pinning, ...).
    if sender is not None:
        name = getattr(sender, "first_name", None) or getattr(sender, "title", None)
        if name:
            return name
    if sender_id is not None:
        return f"id:{sender_id}"
    return "unknown"


async def send(text: str) -> None:
    async def _do(client, chat_id):
        msg = await client.send_message(chat_id, text)
        print(f"sent id={msg.id} chat={chat_id}")
    await _run(_do)


async def tail(n: int) -> None:
    async def _do(client, chat_id):
        # iter_messages yields newest-first; reverse so the window reads as a
        # transcript (oldest -> newest), matching `tail` semantics.
        messages = []
        async for msg in client.iter_messages(chat_id, limit=n):
            messages.append(msg)
        for msg in reversed(messages):
            sender = await msg.get_sender()
            name = _sender_name(sender, msg.sender_id)
            when = msg.date.isoformat(timespec="seconds") if msg.date else "-"
            body = msg.text or "(non-text message)"
            print(f"[{when}] id={msg.id} from={name}: {body}")
    await _run(_do)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tg.py",
        description="Send to / tail the one Telegram test group for the relay.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="send <text> to the test group")
    p_send.add_argument("text", help="message text to send")

    p_tail = sub.add_parser("tail", help="print the last n messages (id, sender, text)")
    p_tail.add_argument("n", type=int, nargs="?", default=10, help="number of messages (default 10)")

    args = parser.parse_args(argv)
    if args.cmd == "send":
        asyncio.run(send(args.text))
    else:
        asyncio.run(tail(args.n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
