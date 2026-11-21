# API

The live contract is OpenAPI, served when docs are enabled (`ENABLE_DOCS`; always on outside
production): [Scalar](https://api.kwami.io/docs) at `/docs`, Swagger UI at `/swagger`, ReDoc at
`/redoc` and the raw schema at `/openapi.json`. This page is
the map: which prefix exists, who may call it, and what it is for.

Base URL is the Cloudflare Worker (`https://api.kwami.io` in production,
`https://dev.api.kwami.io` in development; `APP_PUBLIC_URL`). Local default is `http://127.0.0.1:8080`.

## Auth classes

| Class | How | Used by |
|-------|-----|---------|
| User | `Authorization: Bearer <supabase JWT>` | App routes |
| Agent | `X-API-Key` or `X-Kwami-API-Key` | Usage report, `/internal/*` |
| Admin | `X-Admin-API-Key` or allowlisted JWT | `/admin/reconciliation/*` |
| Webhook | Provider signature | Stripe, Twilio, SendGrid, wallet |
| Work thread | `X-Kwami-Work-Token` | `POST /google/work-thread` |
| Public | none | Health, catalogs, OAuth callbacks |

JWTs are verified against the Supabase JWKS
(`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`), algorithm from the token
header, audience `authenticated`. A missing `Authorization` header is
anonymous; a present but invalid token is 401. Auth is off entirely when
`SUPABASE_URL` is unset — that is a local convenience, not a production mode.

Shared-secret comparisons use `hmac.compare_digest`. Admin access is either
the env-configured `ADMIN_API_KEY` or a JWT whose email is in `ADMIN_EMAILS`
(or whose role is `service_role`).

## Error envelope

Services raise `DomainError` subclasses. The handlers in `src/core/errors.py`
emit:

```json
{
  "detail": "Kwami not found",
  "error": {
    "code": "kwami_not_found",
    "message": "Kwami not found",
    "details": null
  }
}
```

`detail` is kept because the Kwami app already reads it. New clients should
read `error.code`, which is stable. Common codes:

| Code | Status | Meaning |
|------|--------|---------|
| `kwami_not_found` | 404 | No kwami with that id for this user |
| `forbidden` | 403 | Room or resource belongs to someone else |
| `insufficient_credits` | 402 | Balance cannot cover the charge |
| `validation_failed` | 422 | Request body or params failed validation |
| `upstream_error` | 502 | Stripe / Twilio / Zep / LiveKit failed |
| `service_unavailable` | 503 | Feature not configured (missing key) |

Upstream exception text is not forwarded.

## Surfaces

| Prefix | Auth | Purpose |
|--------|------|---------|
| `/` `GET`, `/health` `GET` | public | Liveness. The Container ping and Worker health check hit `/health`. |
| `/token` | user | Mint a LiveKit JWT; claim the room. |
| `/models` | public | STT / LLM / TTS / realtime catalogs, cost estimate. |
| `/voices` | public | TTS and realtime voice catalogs. |
| `/languages` | public | STT / TTS / realtime language catalogs. |
| `/memory/{user_id}` | user + tenancy | Zep sessions, graph, search, ingest. |
| `/credits` | mixed | Balance, packs, purchase, usage, Stripe webhook, agent report. |
| `/channels` | user | Phone search/purchase/release, WhatsApp, outbound call/SMS. |
| `/contacts` | user | Per-kwami address book. |
| `/wallets` | user + webhook | Solana custody wallets, funding intents. |
| `/email` | user | Smart Hub inbox, send, username. |
| `/calendar` | user | Per-kwami events. |
| `/assistant` | user | Reminders and follow-ups, report settings, notifications, the record of messages sent on the user's express order. |
| `/google` | user + work thread | The user's linked Google accounts: Gmail, Calendar, Drive, Docs, Sheets, Slides, Contacts, Tasks. |
| `/pc` | user | The user's PC: whether its work thread is on, Claude's sign-in, sites to sign in to in Jarvis's Chrome. |
| `/providers` | user | Model and voice keys the user connects for their own use (OpenRouter by consent). |
| `/access` | user | The sites Jarvis may visit, the level on each, and its pending requests. |
| `/brain` | user | Work for the brain asked from the app, the cloud setting, the user's agents, what the cloud made. |
| `/internal` | agent | Runtime config, channel lookup by address. |
| `/webhooks` | signature | Twilio voice/WhatsApp (and their status callbacks), SendGrid inbound, LiveKit room events. |
| `/admin/reconciliation` | admin | Provider invoice import and findings. |

### Token

`POST /token` (preferred) and `GET /token` (query-string form). Body fields
use camelCase aliases (`roomName`, `kwamiId`, `canPublish`, …).

- `kwamiId`, when present, must be owned by the caller.
- `roomName` is optional. Omitted names are generated
  (`kwami-web-<kwami8>-<12 hex>`). A name already issued to another user is
  403.
- Tokens live `LIVEKIT_TOKEN_TTL_MINUTES` (default 15, max 360). They are
  redeemed at connect time; a long TTL is a leaked-credential window.
- The response is `{ token, room_name, participant_identity, livekit_url }`.

### Credits

| Method | Path | Auth |
|--------|------|------|
| `GET` | `/credits/balance` | user |
| `GET` | `/credits/packs` | user |
| `POST` | `/credits/purchase` | user |
| `GET` | `/credits/transactions` | user |
| `GET` | `/credits/usage` | user |
| `GET` | `/credits/reconciliation` | user |
| `POST` | `/credits/webhook` | Stripe signature |
| `POST` | `/credits/usage/report` | `X-API-Key` |

`POST /credits/usage/report` accepts an `Idempotency-Key`. Without one, a key
is hashed from the report body so a redelivered identical report still
settles once. See [Billing](./billing.md).

`POST /credits/webhook` is the legacy Stripe path; prefer it staying mounted
so existing Stripe endpoints do not 404.

### Channels and webhooks

Outbound telephony is authenticated (`/channels/calls/outbound`,
`/channels/messages/outbound`). Inbound is Twilio posting to
`/webhooks/twilio/voice` and `/webhooks/twilio/whatsapp`. The voice webhook
returns TwiML that either `<Reject/>` or dials `LIVEKIT_SIP_INBOUND_URI`.

SendGrid inbound parse posts to `/webhooks/email/inbound`.

LiveKit Cloud posts room events to `/webhooks/livekit`, signed with the
project's `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` over the SHA-256 of the body
(anything that does not verify is a 401). They keep `livekit_sessions` honest:
`room_started`, `participant_joined` and `track_published` make a room `active`,
`participant_left` and `track_unpublished` move `last_seen_at`, and
`room_finished` sets `ended` and `ended_at`. `ended` and `abandoned` are final, so
a late or redelivered event never reopens a room; rooms this API did not issue
are acknowledged and ignored. A database failure answers 503 so LiveKit retries
(every update is idempotent). Enable it in the LiveKit project's settings
(Settings → Webhooks) with the URL `https://<api host>/webhooks/livekit`.

### Linking Google and OpenRouter

`POST /google/connect` and `POST /providers/openrouter/connect` return a consent
link. The provider sends the browser to `GET /google/callback` or
`GET /providers/openrouter/callback`, which are public and connect nothing: a
consent link can be opened by someone else. They pass the code on to the app that
started it (`src/services/oauth_return.py`: an exact CORS origin, or
`kwami://connect/callback` for the desktop app), in the URL fragment so it stays
out of logs. The app, signed in, posts it to `/google/complete` or
`/providers/openrouter/complete`, and the API refuses it (403) unless the user is
the one who started.

Every Google call takes `account`, the address of the link to use; without it, the
oldest. A link Google stops honouring is marked `needs_reconnect_at` and the app
says so.

### The work thread's Google

The work thread on the user's PC has no user JWT. `POST /google/work-thread/token`
(user) mints a token for that user alone, valid 90 days; the thread sends it as
`X-Kwami-Work-Token` to `POST /google/work-thread`, and the user is taken from the
token, never from the request.

### PC and access

`/pc/claude` and `/pc/sites` queue an order for the PC's work thread (opening
Claude's sign-in, a site in Jarvis's Chrome); `GET /pc/orders/{order_id}` follows
it. `/access` keeps one rule per site plus `*` for the rest; `GET /access/check`
answers for an address, and `POST /access/requests/{rule_id}` answers a request
Jarvis made for a site it had no rule for.

### Brain

The work the brain does, asked from the app, on the user's PC or in the cloud
(migration 029). Every route answers 503 until that migration is in the database.

| Method | Path | What |
|--------|------|------|
| `GET` | `/brain/where` | Whether the PC and the cloud are there, the user's cloud setting, what waits to come home, and where `auto` work would go now |
| `PUT` | `/brain/cloud` | `{enabled}`: allow or stop the cloud taking this user's work |
| `GET` | `/brain/jobs` | The latest jobs, up to 50 |
| `POST` | `/brain/jobs` | Queue work: `kwami_id`, `task`, optional `title`, `target` (`auto` / `pc` / `cloud`), `difficulty`, `agent_id` |
| `POST` | `/brain/jobs/{job_id}/cancel` | A queued job here; a running one on its worker's next look |
| `GET` `POST` | `/brain/agents` | The user's agents; define one (up to 30) |
| `PUT` `DELETE` | `/brain/agents/{agent_id}` | Change or remove one |
| `POST` | `/brain/agents/{agent_id}/run` | Queue a job for that agent |
| `GET` | `/brain/sync` | What the cloud made and its way home to the PC |
| `POST` | `/brain/sync/{sync_id}/discard` | Leave one in Storage instead of bringing it home, while it waits |

A user has at most 20 jobs queued from the app at once; past that, queueing is a
429 until some start or are cancelled. A kwami that is not the caller's is a 400
("Kwami not found"), an agent that is not theirs a 404.

### Internal

`GET /internal/kwamis/{kwami_id}/runtime` returns the bootstrap payload the
agent would otherwise wait for from the browser. Used on SIP sessions where
there is no browser.

`GET /internal/channels/by-address` resolves a phone or WhatsApp address to
the owning kwami.

`GET`/`POST`/`DELETE /internal/browser-contexts/{owner_key}` read, record and
forget which persisted cloud-browser profile is whose.

## Pagination and aliases

List endpoints take `limit` / `offset` or provider-specific cursors. Request
bodies accept both `snake_case` and `camelCase` via Pydantic
`populate_by_name=True` — the app speaks camelCase.

## Version

The running service reports `src.__version__` in the startup log, the
OpenAPI `info.version`, and `GET /`. That value is written by
`scripts/set-version.sh` at release time. Do not edit it by hand.
