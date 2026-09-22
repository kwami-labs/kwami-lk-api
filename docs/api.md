# API

The live contract is OpenAPI at `/docs` when docs are enabled. This page is
the map: which prefix exists, who may call it, and what it is for.

Base URL is the Cloudflare Worker (`https://kwami-lk-api.nexow.workers.dev`
in production, or `APP_PUBLIC_URL`). Local default is `http://127.0.0.1:8080`.

## Auth classes

| Class | How | Used by |
|-------|-----|---------|
| User | `Authorization: Bearer <supabase JWT>` | App routes |
| Agent | `X-API-Key` or `X-Kwami-API-Key` | Usage report, `/internal/*` |
| Admin | `X-Admin-API-Key` or allowlisted JWT | `/admin/reconciliation/*` |
| Webhook | Provider signature | Stripe, Twilio, SendGrid, wallet |
| Public | none | Health, catalogs |

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
| `/internal` | agent | Runtime config, channel lookup by address. |
| `/webhooks` | signature | Twilio voice/WhatsApp, SendGrid inbound. |
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

### Internal

`GET /internal/kwamis/{kwami_id}/runtime` returns the bootstrap payload the
agent would otherwise wait for from the browser. Used on SIP sessions where
there is no browser.

`GET /internal/channels/by-address` resolves a phone or WhatsApp address to
the owning kwami.

## Pagination and aliases

List endpoints take `limit` / `offset` or provider-specific cursors. Request
bodies accept both `snake_case` and `camelCase` via Pydantic
`populate_by_name=True` — the app speaks camelCase.

## Version

The running service reports `src.__version__` in the startup log, the
OpenAPI `info.version`, and `GET /`. That value is written by
`scripts/set-version.sh` at release time. Do not edit it by hand.
