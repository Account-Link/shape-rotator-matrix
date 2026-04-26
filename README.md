# shape-rotator-matrix

Deployment state for `mtrx.shaperotator.xyz` — the Shape Rotator community's
Matrix homeserver running in a Phala TEE.

This is a **specific instance**, not a generic example — for the example, see
`../dstack-matrix/`. What's Shape Rotator-specific here:

- Custom landing page at `/` and per-invite `/join?code=…` page (served by a
  small nginx sidecar in front of continuwuity)
- `knock-approver`: a tiny Python service. When a Matrix `knock` on the
  Shape Rotator space carries a `reason` matching a code in `/data/codes.json`,
  the approver creates a fresh **per-knock vetting room**, invites the
  knocker into it, and posts a wikipedia-fact haiku captcha. Only after
  the knocker replies with a valid haiku does the approver invite them to
  the space proper.
- dstack-ingress with Namecheap DNS-01 Let's Encrypt for the custom domain

## Layout

```
docker-compose.yml    compose bundling dstack-ingress + continuwuity + landing + knock-approver
continuwuity/         (reserved; currently server is configured purely via env vars)
landing/
  index.html          public landing page
  join.html           /join?code=… page (reads code from URL, shows it + Element link)
  signup.html         /signup page (form for creating a homeserver-hosted identity)
  nginx.conf          routes / /join /signup to static files, /signup/api to the
                      approver service, everything else to continuwuity
knock-approver/
  approver.py         two jobs: (a) long-polls /sync — when a knock matches
                      /data/codes.json it creates a per-knock vetting room
                      (state in /data/vetting.json), posts a wikipedia haiku
                      captcha, and invites the knocker to the space only after
                      they reply with a valid 3-line haiku containing the
                      keyword; (b) aiohttp.web server on :8001 exposing POST
                      /signup/api which validates a code from
                      /data/signup_codes.json, calls continuwuity register
                      with the server-side CONDUWUIT_REGISTRATION_TOKEN, and
                      auto-invites the new user to the space
skills/
  matrix-invite-join/ Hermes-style skill for agents to self-onboard via a
                      /join?code=… link (knock + accept, using their own token)
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

## Two onboarding paths

There are two distinct ways to end up in the Shape Rotator community, and
they use different kinds of codes. Don't confuse them.

**1. Invite code** (`/join?code=XYZ`) — for people who already have a Matrix
account somewhere (matrix.org, their own server, anywhere federated). Their
existing identity knocks on our space; the approver sees the reason code and
auto-invites. **No account on this server is created.** Low commitment.

**2. Signup code** (`/signup` form) — for people or agents who want an
`@name:mtrx.shaperotator.xyz` identity hosted in this TEE homeserver. The
form POSTs to `/signup/api` which holds the real continuwuity registration
token server-side, creates the account, and auto-invites the new user to the
space. Higher commitment — produces a durable identity attested by this server.

Seed both with `INITIAL_CODES` / `INITIAL_SIGNUP_CODES` env vars on first start.
After that, edit `/data/codes.json` / `/data/signup_codes.json` directly on
the CVM (SSH + docker exec) to add more.

## Invite flow (UX)

1. Admin generates a code and sends `https://mtrx.shaperotator.xyz/join?code=XYZ`
   to the new member.
2. They open it → page displays the code in a highlighted box + "Open Shape
   Rotator in Element" button.
3. Element opens on `matrix.to/#/#shape-rotator:mtrx.shaperotator.xyz` →
   they click "Request to join" and paste the code as the reason.
4. `knock-approver` sees the knock in the next `/sync` batch and matches
   the code against `/data/codes.json`. Instead of inviting straight to
   the space, it creates a fresh **per-knock vetting room** and invites
   only the knocker, then posts a captcha: "write a 3-line haiku about
   *<random Wikipedia article title>*; include the word *<keyword>*."
5. The user accepts the vetting-room invite, sets their Element
   displayname (the bot uses it as their handle), and replies with a
   haiku. The approver checks: 3 non-empty lines, 30–400 chars, contains
   the required keyword. They get up to `VETTING_MAX_TRIES` attempts
   (default 3); on success the approver invites them to the actual
   space, on failure the bot leaves and the room dies.
6. Accepting the space invite joins them in; the `restricted` rule on
   child rooms lets them auto-join General / Announcements / Bot Noise.

Stale, un-promoted vetting rooms are abandoned by the bot after
`VETTING_TIMEOUT_SEC` (default 7200 = 2 h).

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
