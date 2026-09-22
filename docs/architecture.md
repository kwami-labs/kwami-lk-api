# Architecture

Kwami LiveKit API is the control plane for Kwami voice agents. It issues LiveKit
tokens, serves model and voice catalogs, stores per-kwami memory and
communications state, and settles money. It does not run the realtime agent
itself — that is a separate process that joins the room LiveKit Cloud dispatches.

The running process is a FastAPI app (`src/main.py`) on Python 3.11. Persistence
is Supabase (Postgres + Auth + PostgREST). Secrets and third-party credentials
are validated at boot by `src/core/config.py`; a missing required variable
refuses to start the process.

## System context

```mermaid
flowchart LR
  subgraph Clients
    App["Kwami App"]
    Agent["Kwami LiveKit agent"]
  end

  API["kwami-lk-api"]

  subgraph Realtime
    LK["LiveKit Cloud"]
  end

  subgraph IdentityAndData
    SB["Supabase Auth + Postgres"]
    Zep["Zep Cloud"]
  end

  subgraph Payments
    Stripe["Stripe"]
    Wallet["Custody / Solana"]
  end

  subgraph Comms
    Twilio["Twilio"]
    SG["SendGrid"]
  end

  App -->|Supabase JWT| API
  Agent -->|X-API-Key / X-Kwami-API-Key| API
  App -->|connect JWT| LK
  Agent -->|join + publish| LK
  LK -->|auto-dispatch agent| Agent

  API --> SB
  API --> Zep
  API --> LK
  API --> Stripe
  API --> Wallet
  API --> Twilio
  API --> SG

  Stripe -->|signed webhook| API
  Twilio -->|signed webhook| API
  SG -->|inbound parse| API
```

Two clients, two trust boundaries:

- **Kwami App** authenticates as a user. Every tenant-scoped route takes a
  Supabase JWT and resolves ownership before business logic runs.
- **Kwami LiveKit agent** authenticates as a service. Usage reports use
  `X-API-Key`; bootstrap/internal routes use `X-Kwami-API-Key`. Both compare
  against `KWAMI_API_KEY` in constant time.

Webhooks have no user. Stripe is verified by signature and a timestamp window;
Twilio is verified against the public URL and **fails closed** when
`TWILIO_AUTH_TOKEN` is unset.

## Process layout

```
src/
├── main.py                 FastAPI app, CORS, lifespan, router mount
├── api/
│   ├── deps.py             JWT, admin, internal API-key dependencies
│   ├── authz.py            Owned-kwami resolvers (path / query / body)
│   └── routes/             One module per HTTP surface
├── core/
│   ├── config.py           Pydantic settings, boot-time validation
│   ├── security.py         JWKS verification, AuthUser, admin checks
│   └── errors.py           Domain errors → stable HTTP envelope
└── services/               Integrations and ledger logic; no FastAPI types
```

Routes stay thin. Services raise typed `DomainError` subclasses; 
`install_error_handlers` maps them to status codes. Handlers do not forward
upstream `str(e)` to clients — that was the previous failure mode.

## Request path

```mermaid
sequenceDiagram
  participant C as Client
  participant CORS as CORSMiddleware
  participant Auth as deps / authz
  participant R as Route
  participant S as Service
  participant DB as Supabase / Postgres
  participant Ext as External API

  C->>CORS: HTTP request
  CORS->>Auth: authenticated request
  alt User route
    Auth->>Auth: verify Supabase JWT via JWKS
    Auth->>Auth: resolve owned kwami
  else Agent route
    Auth->>Auth: hmac.compare_digest API key
  else Webhook
    Auth->>Auth: provider signature
  end
  Auth->>R: principal + owned resource
  R->>S: domain call
  S->>DB: PostgREST / RPC
  opt Provider call
    S->>Ext: Stripe / Twilio / LiveKit / Zep
  end
  S-->>R: result or DomainError
  R-->>C: envelope
```

Authorization is a dependency, not a check inside the handler. `kwami_id`
arrives as a path parameter, a query parameter, or a body field — three
shapes, one resolver in `src/services/kwamis.py`. An unowned kwami never
reaches business logic.

## Voice session

Token issuance is the one endpoint whose output is a credential. Room names
used to come from the client; any authenticated user could join any room by
guessing the name. Rooms are now claimed in `livekit_sessions` at issue time.
`UNIQUE(room_name)` is the arbiter, not a read-then-write in Python.

```mermaid
sequenceDiagram
  participant App as Kwami App
  participant API as kwami-lk-api
  participant DB as livekit_sessions
  participant LK as LiveKit Cloud
  participant Agent as Kwami agent

  App->>API: POST /token (Bearer JWT, optional kwamiId)
  API->>API: require_auth + require_kwami_owned
  API->>API: check credit balance
  alt roomName omitted
    API->>API: build unguessable room name
  end
  API->>DB: claim_room (INSERT or confirm owner)
  alt room owned by someone else
    API-->>App: 403 forbidden
  else first claim or same owner
    API->>API: mint LiveKit JWT (TTL 15m)
    API-->>App: token, room_name, livekit_url
  end
  App->>LK: connect with JWT
  LK->>Agent: auto-dispatch (RoomAgentDispatch)
  Agent->>API: GET /internal/kwamis/{id}/runtime
  Note over App,Agent: realtime audio in the room
  Agent->>API: POST /credits/usage/report (X-API-Key)
  API->>DB: claim usage_reports key, settle ledger
```

Agent dispatch is LiveKit Cloud's job. The API must not dispatch a second
agent into the same room. The token carries `RoomAgentDispatch` so the first
connect creates the agent; the API does not call the dispatch RPC.

Inbound phone calls take a different path to the same room:

```mermaid
sequenceDiagram
  participant Caller as Phone / WhatsApp
  participant TW as Twilio
  participant API as kwami-lk-api
  participant LK as LiveKit SIP
  participant Agent as Kwami agent

  Caller->>TW: inbound call or message
  TW->>API: POST /webhooks/twilio/voice (signed)
  API->>API: validate signature (fail closed)
  API->>API: resolve channel by called number
  alt unknown number
    API-->>TW: TwiML Reject
  else mapped to a kwami
    API->>API: ensure contact + conversation
    API-->>TW: TwiML Dial LIVEKIT_SIP_INBOUND_URI
    TW->>LK: SIP INVITE
    LK->>Agent: dispatch into call room
  end
```

Shared SIP trunks are platform infrastructure. Users buy numbers in the app;
the API attaches those numbers to the shared Twilio trunk and maps them to a
kwami. There is no per-kwami trunk to provision by hand.

## Catalogs

`/models`, `/voices`, and `/languages` are read-mostly. The lists are derived
from LiveKit plugin packages plus the YAML under `config/`:

| File | Feeds |
|------|--------|
| `config/livekit_inference_llm.yaml` | `/models/llm` |
| `config/livekit_inference_stt.yaml` | `/models/stt` |
| `config/livekit_inference_tts.yaml` | `/models/tts` |
| `config/livekit_voices.yaml` | `/voices/*` |
| `config/livekit_languages.yaml` | `/languages/*` |

Plugin packages are dependencies so the API can extract model IDs the agent
can actually load. They are not used to call the providers from this process.

## Memory

`/memory/{user_id}/...` is a Zep Cloud facade. Tenancy is
`src/core/security.py:check_user_access`: the authenticated user may read
their own id, the legacy `kwami_<user.id>` namespace, or a per-kwami
`kwami_<user.id>_*` namespace. Those three anchored checks are the whole
contract — a previous `str.replace` rule accepted namespaces that merely
contained the caller's id after mangling.

## Where money is decided

Python prices a usage item (`src/services/pricing.py`) and applies markup
(`BILLING_MARKUP_MULTIPLIER`). The **balance change** is a Postgres function.
`deduct_credits` is `UPDATE ... WHERE balance >= p_amount` in one statement;
`add_credits` is idempotent via a unique ledger key and
`ON CONFLICT DO NOTHING`. See [Billing](./billing.md).

## Environments

| `APP_ENV` | Docs | Credits fail-open | Wallets |
|-----------|------|-------------------|---------|
| `development` | on | on (unless set) | on (unless set) |
| `staging` | on | on (unless set) | on (unless set) |
| `production` | off unless `ENABLE_DOCS=true` (UIs *and* `/openapi.json`) | off | off unless `WALLET_ENABLED=true` |

The Cloudflare Worker terminates TLS and proxies into the Container. Uvicorn
runs with `proxy_headers=True` so `request.url.scheme` stays `https` — Twilio
signs the public HTTPS URL, and a scheme mismatch fails every inbound call.


## Concurrency model

Every outbound call is asynchronous. Supabase goes through
`create_async_client`; Stripe uses its `*_async` resource methods; Twilio runs
on `AsyncTwilioHttpClient`; the provider-reconciliation pulls use `httpx`; Zep
was already `AsyncZep`. One client of each is built per process — in the
lifespan for Supabase, lazily for the rest — so a request borrows a pooled
connection instead of opening one.

This was not always so. Every SDK was the synchronous one, called straight from
`async def` handlers, so each request held the event loop for the duration of
its network round trip. A static walk of the call graph put 94 of 167 `async
def` functions on a path to a blocking call, and the two shared ownership
dependencies in `src/api/authz.py` were among them, so nearly every
authenticated request blocked before its handler began.

`tests/perf/test_event_loop_stall.py` measures it: ten concurrent reads against
a 50ms query took **0.61s** before and take **0.02s** now.

Two things stay synchronous on purpose, and are pushed to a worker thread with
`anyio.to_thread.run_sync` rather than left on the loop:

* the JWKS fetch in `src/core/security.py`, because `PyJWKClient` is urllib-based
  and a cache miss is an HTTPS round trip on the auth path of every request;
* Louvain community detection in the memory routes, which is CPU-bound and
  superlinear in the graph — it stalls the loop exactly as a socket read would.

## Middleware

In order, outermost first:

| Middleware | What it does |
|---|---|
| `RequestContextMiddleware` | Assigns or honours `X-Request-ID`, binds it to every log record, writes the access line |
| `SecurityHeadersMiddleware` | `nosniff`, `DENY`, `no-referrer`, a CSP, and HSTS over TLS |
| `BodySizeLimitMiddleware` | 413 above `MAX_REQUEST_BODY_BYTES`, counted as the body streams |
| `CORSMiddleware` | Explicit origins; a wildcard is refused at boot in production |

All three are raw ASGI rather than `BaseHTTPMiddleware`. That base class runs
the rest of the application in a separate anyio task with two memory streams
between them: a task and two queue hops on every request, and — found the hard
way — it also hides everything downstream from coverage's tracer, which made
whole route modules measure 26% while their tests passed.

Rate limiting (`src/api/ratelimit.py`) keys on the authenticated user, falling
back to the agent or admin key and then the peer address, so one office behind a
NAT does not share a budget and one account cannot buy headroom by changing
address. Storage is per worker by default; `RATE_LIMIT_STORAGE_URI` takes a
Redis URL to make the limits exact across machines.
