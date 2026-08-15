"""Focused regression check for Element's in-room SAS message transport."""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "landing"))

import sas_verification as sas  # noqa: E402


class _Client:
    def __init__(self):
        self.handlers = []
        self.sent = []

    def add_event_handler(self, event_type, handler):
        self.handlers.append((event_type, handler))

    async def send_message_event(self, room_id, event_type, content):
        self.sent.append((room_id, event_type, content))
        return "$room-reply"


class _Event:
    sender = "@alice:example.org"
    room_id = "!dm:example.org"

    def __init__(self, content):
        self.content = content


class _Session:
    def __init__(self, txn_id, their_user, their_device, our_user,
                 our_device, client, store, room_id=None):
        self.room_id = room_id
        self.calls = []

    async def handle_request(self, content):
        self.calls.append(("request", content))
        await self._send("m.key.verification.ready", {
            "transaction_id": content["transaction_id"]})

    async def _send(self, event_type, content):
        await self.client.send_message_event(self.room_id, "m.room.message",
                                              {**content, "msgtype": event_type})


async def main():
    # Keep this test independent of libolm's cryptographic state machine: the
    # regression is the event routing and the room reply transport.
    original = sas._SASSession

    def session(*args, **kwargs):
        result = _Session(*args, **kwargs)
        result.client = args[5]
        return result

    sas._SASSession = session
    try:
        client = _Client()
        manager = sas.SASVerificationManager(client, object(),
                                             "@bot:example.org", "BOT")
        handled = await manager.handle_room_message(_Event({
            "msgtype": "m.key.verification.request",
            "transaction_id": "txn-1",
            "from_device": "ALICE",
        }))
        assert handled
        assert client.sent == [(
            "!dm:example.org", "m.room.message", {
                "transaction_id": "txn-1",
                "msgtype": "m.key.verification.ready",
            })
        ]
        print("PASS: in-room verification request routed and replied in-room")
    finally:
        sas._SASSession = original


if __name__ == "__main__":
    asyncio.run(main())
