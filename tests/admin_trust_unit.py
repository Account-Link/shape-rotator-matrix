"""Pure checks for the admin command cryptographic gate."""
import os
import sys

os.environ.setdefault("HS", "http://matrix.test")
os.environ.setdefault("SPACE_ID", "!space:test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "knock-approver"))

import approver
from mautrix.types import TrustState


def check(name, actual, expected):
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")
    print(f"PASS: {name}")


check("cleartext is unverified",
      approver._is_cross_signed_admin_event({}), False)
check("encrypted unknown device is unverified",
      approver._is_cross_signed_admin_event({
          "mautrix": {"was_encrypted": True,
                      "trust_state": TrustState.UNKNOWN_DEVICE}}), False)
check("encrypted TOFU device is accepted",
      approver._is_cross_signed_admin_event({
          "mautrix": {"was_encrypted": True,
                      "trust_state": TrustState.CROSS_SIGNED_TOFU}}), True)
check("encrypted rotated master is refused",
      approver._is_cross_signed_admin_event({
          "mautrix": {"was_encrypted": True,
                      "trust_state": TrustState.CROSS_SIGNED_UNTRUSTED}}), False)
