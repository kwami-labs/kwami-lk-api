"""What the generated OpenAPI document says about the API as a whole.

Per-route behaviour is described where the route is declared. This module holds the parts that belong
to no single route: the overview, who may call what, the error envelope, and one description per tag
so the sidebar of `/docs` is a map rather than a list of bare words.

`docs/api.md` is the long-form version; keep the two in step.
"""

from __future__ import annotations

from typing import Any

DESCRIPTION = """\
Token endpoint and configuration API for Kwami AI agents: LiveKit tokens, model and voice catalogs,
memory, credits and billing, telephony channels, email, calendar, Google, the PC work thread, and the
brain. It runs as a FastAPI container behind a Cloudflare Worker.

## Authentication

Every route belongs to one of these classes. Use **Authorize** to set the credential the route needs.

| Class | How to authenticate | Used by |
|-------|---------------------|---------|
| **User** | `Authorization: Bearer <Supabase JWT>` | The app's routes |
| **Agent** | `X-Kwami-API-Key` (or `X-API-Key` on the usage report) | `/internal/*`, `POST /credits/usage/report` |
| **Admin** | `X-Admin-API-Key`, or a JWT whose email is allow-listed | `/admin/reconciliation/*` |
| **Webhook** | The provider's own signature | Stripe, Twilio, SendGrid, LiveKit, wallet |
| **Work thread** | `X-Kwami-Work-Token` | `POST /google/work-thread` |
| **Public** | None | Health, catalogs, OAuth callbacks |

JWTs are verified against the Supabase JWKS with audience `authenticated`. A missing `Authorization`
header is anonymous; a present but invalid token is `401`.

## Errors

Failures share one envelope. `detail` is kept for older clients; new clients should read
`error.code`, which is stable.

```json
{
  "detail": "Kwami not found",
  "error": { "code": "kwami_not_found", "message": "Kwami not found", "details": null }
}
```

| Status | Meaning |
|--------|---------|
| `401` | Missing, expired or invalid credential |
| `402` | `insufficient_credits`: the balance cannot cover the charge |
| `403` | Authenticated, but the resource belongs to someone else |
| `404` | Not found for this caller |
| `413` | Request body larger than the cap (10 MiB by default) |
| `422` | `validation_failed`: the body or parameters did not validate |
| `429` | `rate_limited`: too many requests; per account when signed in, per address otherwise |
| `502` | `upstream_error`: Stripe, Twilio, Zep or LiveKit failed. Upstream text is never forwarded |
| `503` | `service_unavailable`: the feature is not configured |

Every response carries `X-Request-ID`. Send one to keep a trace you started; quote it when reporting
a problem.

## Conventions

- Bodies and responses are JSON unless a route says otherwise (webhooks answer TwiML or `204`).
- Token requests use camelCase field aliases (`roomName`, `kwamiId`, `canPublish`).
- Money-moving writes accept an `Idempotency-Key` so a retry settles once.
- Docs: this page is Scalar; the same schema is in [Swagger UI](/swagger), [ReDoc](/redoc) and
  [`/openapi.json`](/openapi.json).
"""

TAGS: list[dict[str, Any]] = [
    {
        "name": "Health",
        "description": "Liveness. The Container ping and the Worker health check hit `/health`. "
        "Public.",
    },
    {
        "name": "Token",
        "description": "Mint a LiveKit access token and claim a room. `POST` is preferred; `GET` is "
        "the query-string form. A `kwamiId` must be owned by the caller, and a room name already "
        "issued to another user is a `403`. Tokens live 15 minutes by default (max 360). **User.**",
    },
    {
        "name": "Models",
        "description": "Catalogs of speech-to-text, LLM, text-to-speech and realtime models, and "
        "a cost estimate for a combination. **Public.**",
    },
    {
        "name": "Voices",
        "description": "Text-to-speech and realtime voice catalogs. **Public.**",
    },
    {
        "name": "Languages",
        "description": "Speech-to-text, text-to-speech and realtime language catalogs. **Public.**",
    },
    {
        "name": "Memory",
        "description": "A kwami's memory in Zep: sessions, the knowledge graph, search and "
        "ingest, all under `/memory/{user_id}`. The caller must be that user. **User + tenancy.**",
    },
    {
        "name": "Credits",
        "description": "Balance, credit packs, purchase through Stripe, the transaction ledger, usage "
        "and reconciliation. `POST /credits/webhook` is Stripe's signed callback and "
        "`POST /credits/usage/report` is the agent's metered usage report, which accepts an "
        "`Idempotency-Key`. **Mixed:** user, Stripe signature, agent key.",
    },
    {
        "name": "Channels",
        "description": "Phone numbers (search, purchase, release), WhatsApp, and outbound calls and "
        "SMS. Inbound traffic arrives through `/webhooks`. **User.**",
    },
    {
        "name": "Contacts",
        "description": "A per-kwami address book. **User.**",
    },
    {
        "name": "Wallets",
        "description": "Solana custody wallets and funding intents. Provider callbacks are signed "
        "with `X-Wallet-Signature`. **User + webhook.**",
    },
    {
        "name": "Email",
        "description": "The Smart Hub inbox: list and read mail, send, and manage the kwami's "
        "address. Inbound mail arrives through `/webhooks/email/inbound`. **User.**",
    },
    {
        "name": "Calendar",
        "description": "Per-kwami calendar events. **User.**",
    },
    {
        "name": "Assistant",
        "description": "Reminders and follow-ups, report settings, notifications, and the record of "
        "messages sent on the user's express order. **User.**",
    },
    {
        "name": "Google",
        "description": "The user's linked Google accounts: connect and complete the consent flow, "
        "then Gmail, Calendar, Drive, Docs, Sheets, Slides, Contacts and Tasks. Pass `account` to "
        "choose a link; without it the oldest is used. A link Google stops honouring is marked "
        "`needs_reconnect_at`. The OAuth callback is public and connects nothing by itself. "
        "**User + work thread.**",
    },
    {
        "name": "PC",
        "description": "The user's PC: whether its work thread is on, Claude's sign-in, and sites to "
        "sign in to in Jarvis's Chrome. Orders are queued for the work thread and followed at "
        "`GET /pc/orders/{order_id}`. **User.**",
    },
    {
        "name": "Brain",
        "description": "Work the brain does, asked from the app, on the user's PC or in the cloud: "
        "where it would run, jobs, the user's agents, and what the cloud made on its way home. "
        "Answers `503` until migration 029 is in the database. At most 20 jobs may be queued from "
        "the app at once. **User.**",
    },
    {
        "name": "Providers",
        "description": "Model and voice keys the user connects for their own use. OpenRouter is "
        "connected by consent; its callback is public and connects nothing by itself. **User.**",
    },
    {
        "name": "Access",
        "description": "The sites Jarvis may visit: one rule per site plus `*` for the rest, the "
        "level on each, a check for an address, and the pending requests Jarvis made for a site "
        "it had no rule for. **User.**",
    },
    {
        "name": "Internal",
        "description": "Backend-to-backend routes for the agent: the runtime bootstrap payload for a "
        "kwami and channel lookup by address. Used on SIP sessions, where there is no browser. "
        "**Agent key.**",
    },
    {
        "name": "Webhooks",
        "description": "Provider callbacks: Twilio voice and WhatsApp, SendGrid inbound parse, and "
        "LiveKit room events. Each is authenticated by the provider's signature, never by a user "
        "token. A signature that does not verify is a `401`.",
    },
    {
        "name": "Admin Reconciliation",
        "description": "Import provider invoices and review reconciliation findings. **Admin.**",
    },
]

CONTACT = {"name": "Kwami", "url": "https://kwami.io"}
LICENSE_INFO = {
    "name": "See repository LICENSE",
    "url": "https://github.com/kwami-labs/kwami-lk-api",
}
