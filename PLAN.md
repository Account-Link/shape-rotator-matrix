# Issue #62 plan

- [x] Send an Olm-encrypted `m.room_key_bundle` to every known invitee device after successful E2EE-room invitations.
- [x] Cover signup and vetted-knock invitation paths, preserving the inviter identity.
- [x] Append `(endorser, invitee, code_or_manual, room_id, ts)` edges to `/data/endorsements.jsonl`.
- [x] Prove delivery, bundle import, and decryption of pre-invite messages in the dev-stack E2E test.
