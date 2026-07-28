# Issue #20 plan

- [x] Require admin commands to arrive as decrypted Matrix events.
- [x] Require mautrix `CROSS_SIGNED_TOFU` (or stronger) before PL/allowlist authorization.
- [x] Refuse unknown, unverified, and rotated-device commands and audit the refusal.
- [x] Document the threat model and the remaining hermes-agent scope.
- [ ] Verify the deployed staging knock-approver flow with a signed-in operator device.
- [ ] Apply the equivalent sender verification to the hermes shape-rotator agent (separate repo).
