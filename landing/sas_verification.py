"""
SAS (m.sas.v1) verification handler for the hermes Matrix gateway.

Patch: append this to /opt/hermes-agent/gateway/platforms/matrix.py
and register _SASVerificationManager on the client after E2EE setup.

Auto-accepts all incoming verification requests (no emoji confirmation
required from the bot side — it trusts any user who initiates).
"""

import asyncio
import base64
import hashlib
import json
import logging
import time

logger = logging.getLogger("gateway.platforms.matrix.sas")

_SAS_METHOD = "m.sas.v1"
_KEY_AGREEMENT = "curve25519-hkdf-sha256"
_HASH_METHOD = "sha256"
_MAC_METHODS = ["hkdf-hmac-sha256.v2", "org.matrix.msc3906.hkdf-hmac-sha256.v2"]
_SAS_TYPES = ["emoji", "decimal"]


def _canonical_json(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.b64decode(s)


class _SASSession:
    """State machine for one verification transaction."""

    def __init__(self, txn_id: str, their_user: str, their_device: str,
                 our_user: str, our_device: str, client, crypto_store):
        import olm
        self.txn_id = txn_id
        self.their_user = their_user
        self.their_device = their_device
        self.our_user = our_user
        self.our_device = our_device
        self._client = client
        self._store = crypto_store
        self._sas = olm.Sas()
        self._start_event: dict | None = None
        self._cancelled = False

    async def _send(self, event_type: str, content: dict):
        from mautrix.types import EventType
        et = EventType.find(event_type, EventType.Class.TO_DEVICE)
        await self._client.send_to_device(
            et, {self.their_user: {self.their_device: content}})

    async def handle_request(self, content: dict):
        logger.info("SAS: verification request from %s/%s txn=%s",
                    self.their_user, self.their_device, self.txn_id)
        await self._send("m.key.verification.ready", {
            "from_device": self.our_device,
            "methods": [_SAS_METHOD],
            "transaction_id": self.txn_id,
        })

    async def handle_start(self, content: dict):
        if content.get("method") != _SAS_METHOD:
            await self._cancel("m.unknown_method")
            return

        self._start_event = content
        our_pubkey = self._sas.pubkey

        # Commitment = base64(sha256(base64(pubkey) + canonical_json(start)))
        start_copy = {k: v for k, v in content.items() if k != "transaction_id"}
        commitment_input = our_pubkey.encode() + _canonical_json(start_copy)
        commitment = _b64(hashlib.sha256(commitment_input).digest())

        await self._send("m.key.verification.accept", {
            "transaction_id": self.txn_id,
            "method": _SAS_METHOD,
            "key_agreement_protocol": _KEY_AGREEMENT,
            "hash": _HASH_METHOD,
            "message_authentication_code": _MAC_METHODS[0],
            "short_authentication_string": _SAS_TYPES,
            "commitment": commitment,
        })

    async def handle_key(self, content: dict):
        their_key = content.get("key", "")
        self._sas.set_their_pubkey(their_key)

        await self._send("m.key.verification.key", {
            "transaction_id": self.txn_id,
            "key": self._sas.pubkey,
        })

        # Auto-accept: compute and send MAC immediately (no emoji confirmation).
        await self._send_mac()

    async def _ensure_device(self):
        device = await self._store.get_device(self.their_user, self.their_device)
        if device:
            return device
        from mautrix.types import DeviceIdentity, TrustState
        q = await self._client.api.request(
            "POST", "/_matrix/client/v3/keys/query",
            content={"device_keys": {self.their_user: [self.their_device]}})
        dinfo = q.get("device_keys", {}).get(self.their_user, {}).get(self.their_device)
        if not dinfo:
            return None
        ed = next(v for k, v in dinfo["keys"].items() if k.startswith("ed25519:"))
        curve = next(v for k, v in dinfo["keys"].items() if k.startswith("curve25519:"))
        device = DeviceIdentity(
            user_id=self.their_user, device_id=self.their_device,
            identity_key=curve, signing_key=ed,
            trust=TrustState.UNVERIFIED, deleted=False,
            name=dinfo.get("unsigned", {}).get("device_display_name", "") or "")
        await self._store.put_devices(self.their_user, {self.their_device: device})
        return device

    def _mac_info_sending(self, key_id: str) -> str:
        # When we send a MAC: info = "MATRIX_KEY_VERIFICATION_MAC" + sender + receiver + tx + key_id
        return ("MATRIX_KEY_VERIFICATION_MAC"
                + self.our_user + self.our_device
                + self.their_user + self.their_device
                + self.txn_id + key_id)

    def _mac_info_receiving(self, key_id: str) -> str:
        # When we receive a MAC: info uses their (sender) before our (receiver).
        return ("MATRIX_KEY_VERIFICATION_MAC"
                + self.their_user + self.their_device
                + self.our_user + self.our_device
                + self.txn_id + key_id)

    async def _send_mac(self):
        # We attest our OWN device signing key under our own device ID.
        our_signing = self._client.crypto.account.signing_key
        key_id = f"ed25519:{self.our_device}"
        key_mac = self._sas.calculate_mac(our_signing, self._mac_info_sending(key_id))
        keys_mac = self._sas.calculate_mac(key_id, self._mac_info_sending("KEY_IDS"))

        await self._send("m.key.verification.mac", {
            "transaction_id": self.txn_id,
            "mac": {key_id: key_mac},
            "keys": keys_mac,
        })
        logger.info("SAS: sent MAC for %s/%s", self.our_user, self.our_device)

    async def handle_mac(self, content: dict):
        device = await self._ensure_device()
        if not device:
            await self._cancel("m.key_mismatch")
            return

        key_id = f"ed25519:{self.their_device}"
        expected_key_mac = self._sas.calculate_mac(device.signing_key, self._mac_info_receiving(key_id))
        expected_keys_mac = self._sas.calculate_mac(key_id, self._mac_info_receiving("KEY_IDS"))

        mac = content.get("mac", {})
        keys_mac = content.get("keys", "")

        if mac.get(key_id) != expected_key_mac or keys_mac != expected_keys_mac:
            logger.warning("SAS: MAC mismatch for %s/%s", self.their_user, self.their_device)
            await self._cancel("m.key_mismatch")
            return

        # Mark device as verified in crypto store.
        from mautrix.types import TrustState
        device.trust = TrustState.VERIFIED
        await self._store.put_devices(self.their_user, {self.their_device: device})
        logger.info("SAS: verified device %s/%s ✓", self.their_user, self.their_device)

        await self._send("m.key.verification.done", {"transaction_id": self.txn_id})

    async def _cancel(self, code: str, reason: str = ""):
        self._cancelled = True
        await self._send("m.key.verification.cancel", {
            "transaction_id": self.txn_id,
            "code": code,
            "reason": reason or code,
        })


class SASVerificationManager:
    """Handles all incoming verification to-device events."""

    def __init__(self, client, crypto_store, our_user: str, our_device: str):
        self._client = client
        self._store = crypto_store
        self._our_user = our_user
        self._our_device = our_device
        self._sessions: dict[str, _SASSession] = {}

        from mautrix.types import EventType

        for ev_type_str, handler in [
            ("m.key.verification.request", self._on_request),
            ("m.key.verification.start",   self._on_start),
            ("m.key.verification.key",     self._on_key),
            ("m.key.verification.mac",     self._on_mac),
            ("m.key.verification.cancel",  self._on_cancel),
        ]:
            et = EventType.find(ev_type_str, EventType.Class.TO_DEVICE)
            client.add_event_handler(et, handler)

    def _get_or_create(self, content: dict, sender: str) -> _SASSession | None:
        txn_id = content.get("transaction_id")
        if not txn_id:
            return None
        if txn_id not in self._sessions:
            their_device = content.get("from_device", "")
            self._sessions[txn_id] = _SASSession(
                txn_id, sender, their_device,
                self._our_user, self._our_device,
                self._client, self._store,
            )
        return self._sessions[txn_id]

    @staticmethod
    def _as_dict(ev):
        c = ev.content if hasattr(ev, "content") else ev.get("content", {})
        if hasattr(c, "serialize"):
            c = c.serialize()
        return c

    async def _on_request(self, event):
        content = self._as_dict(event)
        sender = event.sender if hasattr(event, "sender") else event.get("sender", "")
        sess = self._get_or_create(content, sender)
        if sess:
            await sess.handle_request(content)

    async def _on_start(self, event):
        content = self._as_dict(event)
        sender = event.sender if hasattr(event, "sender") else event.get("sender", "")
        sess = self._get_or_create(content, sender)
        if sess:
            await sess.handle_start(content)

    async def _on_key(self, event):
        content = self._as_dict(event)
        txn_id = content.get("transaction_id")
        if txn_id and txn_id in self._sessions:
            await self._sessions[txn_id].handle_key(content)

    async def _on_mac(self, event):
        content = self._as_dict(event)
        txn_id = content.get("transaction_id")
        if txn_id and txn_id in self._sessions:
            await self._sessions[txn_id].handle_mac(content)
            self._sessions.pop(txn_id, None)

    async def _on_cancel(self, event):
        content = self._as_dict(event)
        txn_id = content.get("transaction_id")
        if txn_id:
            self._sessions.pop(txn_id, None)
