# shape-rotator-matrix

Deployment state for `mtrx.shaperotator.xyz` — the Shape Rotator community's
Matrix homeserver running in a Phala TEE.

This is a **specific instance**, not a generic example — for the example, see
`../dstack-matrix/`. What's Shape Rotator-specific here:

- Custom landing page at `/` and per-invite `/join?code=…` page (served by a
  small nginx sidecar in front of continuwuity)
- `knock-approver`: a tiny Python service that auto-approves Matrix `knock`
  events on the Shape Rotator space when the knock `reason` matches a code
  in `/data/codes.json`
- dstack-ingress with Namecheap DNS-01 Let's Encrypt for the custom domain

## Layout

```
docker-compose.yml    compose bundling dstack-ingress + continuwuity + landing + knock-approver
continuwuity/         (reserved; currently server is configured purely via env vars)
landing/
  index.html          public landing page
  join.html           /join?code=… page (reads code from URL, shows it + Element link)
  nginx.conf          routes / and /join to static files, everything else to continuwuity
knock-approver/
  approver.py         long-polls /sync, approves knocks with matching code via /invite
deploy/
  encode_env.sh       refreshes *_B64 env entries from the plaintext sources
.env.example          documented env vars; real .env is gitignored
```

## Deploy

```bash
# 1. First time: copy the template and fill in secrets (Namecheap keys,
#    registration token, knock-approver token, initial codes).
cp .env.example .env
$EDITOR .env

# 2. Re-encode the landing pages + approver into the *_B64 env entries.
./deploy/encode_env.sh

# 3. Push to the CVM.
phala deploy --cvm-id dstack-matrix -c docker-compose.yml -e .env
```

The CVM retains state across redeploys via the named volumes
(`continuwuity-data`, `cert-data`, `knock-data`). Do **not** delete the CVM
to redeploy — you'll lose the Matrix database and have to start fresh.

## Space + room layout

- Space:         `!4FL8uL5OEYLATG1VH4wC2CD3pfIV6BMFId9VT7rmm-g`  (`#shape-rotator:mtrx.shaperotator.xyz`)
- General:       `!z85RFatK8w0f04i8yVOCidnYRKXlZuRjK4kYkdXVhUc`
- Announcements: `!9p9ZAr8CFo8WjD8g0hKv_1sOewNWt0zTBCWMAkWnLxo`
- Bot Noise:     `!a8L-8zCDgQZhddUWkb4FYkCVjPBu0lY6QwtLVBXIRXc`

Join rules:
- Space: `knock`
- Child rooms: `restricted` (auto-join for anyone already in the space)

## Invite flow (UX)

1. Admin generates a code and sends `https://mtrx.shaperotator.xyz/join?code=XYZ`
   to the new member.
2. They open it → page displays the code in a highlighted box + "Open Shape
   Rotator in Element" button.
3. Element opens on `matrix.to/#/#shape-rotator:mtrx.shaperotator.xyz` →
   they click "Request to join" and paste the code as the reason.
4. `knock-approver` sees the knock in the next `/sync` batch, matches the
   code against `/data/codes.json`, and POSTs `/invite` → the user gets an
   invite in their client.
5. Accepting the invite joins the space; the `restricted` rule on child
   rooms lets them auto-join General / Announcements / Bot Noise.

## Managing invite codes

Codes live in a named volume (`knock-data:/data/codes.json`) inside the CVM.
Initial codes are seeded from `INITIAL_CODES` in `.env` on first start —
subsequent restarts only add codes that aren't already in the file (existing
codes keep their used count).

To add more codes without redeploying, you'll currently need to SSH into the
CVM and edit `/data/codes.json` directly. A tiny admin Matrix command inside
the approver is a nice-to-have next step.

File format:

```json
{
  "abc123xyz": {"uses_remaining": 5, "label": "batch A"},
  "hfuy89kl":  {"uses_remaining": 1, "label": "one-shot for X"}
}
```

All approvals and rejections are appended to `/data/log.jsonl`.

## Secrets that belong in .env

- `NAMECHEAP_USERNAME`, `NAMECHEAP_API_KEY` — DNS-01 for Let's Encrypt
- `REGISTRATION_TOKEN` — continuwuity signup gate (for bots/agents; humans
  should create a matrix.org account and federate in)
- `KNOCK_APPROVER_TOKEN` — access token of a Matrix user with PL ≥ 50 in
  the space (currently `@shape-rotator-2:mtrx.shaperotator.xyz`)
- `DSTACK_AUTHORIZED_KEYS` — ssh pubkey for `phala ssh` access

Rotate any of them by updating `.env` and redeploying.
