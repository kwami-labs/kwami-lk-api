# Kwami LiveKit API

[![release](https://img.shields.io/badge/release-v0.1.1-blue)](CHANGELOG.md)
[![ci](https://github.com/kwami-labs/kwami-lk-api/actions/workflows/ci.yml/badge.svg)](https://github.com/kwami-labs/kwami-lk-api/actions/workflows/ci.yml)
[![cd](https://github.com/kwami-labs/kwami-lk-api/actions/workflows/cd.yml/badge.svg)](https://github.com/kwami-labs/kwami-lk-api/actions/workflows/cd.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

Backend API for **Kwami** voice agents: LiveKit token issuance, model and voice catalogs, Zep memory operations, credits (Stripe), telephony, email, and wallets. Used by [Kwami App](https://github.com/kwami-labs/kwami-app) and the Kwami LiveKit agent.

Long-form documentation lives in [`docs/`](docs/README.md): [architecture](docs/architecture.md), [API](docs/api.md), [security](docs/security.md), [billing](docs/billing.md), [data model](docs/data-model.md), [deployment](docs/deployment.md), [development](docs/development.md).

## Features

- **LiveKit tokens** — Issue JWT tokens for app/agent participants; rooms are claimed server-side; agent dispatch is LiveKit Cloud
- **Models** — STT, LLM, TTS model lists derived from LiveKit plugins (OpenAI, Anthropic, Deepgram, ElevenLabs, etc.)
- **Voices & languages** — Voice and language catalogs for the app
- **Memory** — Zep-backed memory endpoints (sessions, search, graph operations)
- **Credits** — Balance, usage settlement, Stripe checkout, admin reconciliation
- **Channels** — Phone number search/purchase, SIP calls, SMS, WhatsApp
- **Email & calendar** — SendGrid Smart Hub and per-kwami events
- **Wallets** — Optional Solana custody funding into the credits ledger
- **Auth** — Supabase JWT (JWKS), agent API keys, signed webhooks

## Prerequisites

- **Python** 3.11+
- **uv** (recommended) or pip

## Setup

```bash
# Install dependencies (uv)
uv sync

# Or with pip
pip install -e .

# Configure environment
cp .env.sample .env
# Edit .env with your keys (see Environment variables)
```

## Running

```bash
# Development (reload on change)
make dev
# or: uv run python -m src.main

# Production (e.g. in Docker)
make docker-build
make docker-up
```

API listens on `API_HOST:API_PORT` (default `0.0.0.0:8080`). OpenAPI docs at `/docs` and `/redoc` when `ENABLE_DOCS=true`.

## API overview

| Prefix | Description |
|--------|-------------|
| `/` `/health` | Liveness |
| `/token` | LiveKit token generation and room claim |
| `/memory` | Zep memory (sessions, graph, search) |
| `/models` `/voices` `/languages` | Catalogs |
| `/credits` | Balance, packs, Stripe, usage report |
| `/channels` | Phone, SMS, WhatsApp, outbound calls |
| `/contacts` `/email` `/calendar` | Per-kwami communications |
| `/wallets` | Custody wallets and funding |
| `/webhooks` | Twilio and SendGrid inbound |
| `/internal` | Agent bootstrap (API key) |
| `/admin/reconciliation` | Provider invoice diffs |

The **token** endpoint expects a POST body with optional `roomName`, `participantName` / `participantIdentity`, permissions, and optional `kwamiId`. Omitted room names are generated; a name owned by another user is rejected. Full route map and auth classes: [docs/api.md](docs/api.md).

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LIVEKIT_URL` | Yes | LiveKit WebSocket URL (e.g. `wss://your-project.livekit.cloud`) |
| `LIVEKIT_API_KEY` | Yes | LiveKit API key |
| `LIVEKIT_API_SECRET` | Yes | LiveKit API secret |
| `ZEP_API_KEY` | For memory | Zep Cloud API key |
| `SUPABASE_URL` | For auth | Supabase project URL (JWKS verification) |
| `SUPABASE_SECRET_KEY` | For credits/DB | Supabase service role key |
| `STRIPE_SECRET_KEY` | For credits | Stripe secret key |
| `STRIPE_WEBHOOK_SECRET` | For credits | Stripe webhook signing secret |
| `STRIPE_PUBLISHABLE_KEY` | Optional | Stripe publishable key |
| `KWAMI_API_KEY` | For agent | Shared secret for agent usage reporting (X-API-Key) |
| `CORS_ORIGINS` | No | Comma-separated origins (default `*` in dev) |
| `API_HOST` / `API_PORT` | No | Bind address and port (default `0.0.0.0:8080`) |
| `APP_ENV` | No | `development` \| `staging` \| `production` |
| `ENABLE_DOCS` | No | Set to `true` to expose `/docs` and `/redoc` in production |

See `.env.sample` for a full list and comments.

## Commands

`make` on its own runs the whole gate. `make help` lists every target.

| Command | Description |
|---------|-------------|
| `make check` | The full gate: lint, format, both test lanes, coverage floors, migrations |
| `make install` | Sync the venv, dev extra included |
| `make dev` | Run the API on :8080 |
| `make test` | Unit and API tests — no database, no network |
| `make test-db-up` | Start a throwaway Postgres on 55433 for the integration lane |
| `make test-integration` | Migrations and money invariants against that Postgres |
| `make coverage` | Run both lanes and merge their profiles |
| `make coverage-gate` | Enforce the per-module floors in `coverage.floors` |
| `make lint` / `make format` | Ruff check / format and fix |
| `make vuln` | pip-audit over the locked dependency set (network) |
| `make hooks` | Install the pre-push hook that refuses a direct push to `main` |
| `make docker-build` | Build the release image CD publishes |
| `make docker-up` / `make docker-down` | Run or stop that container |

## Project structure

```
src/
├── main.py              # FastAPI app, CORS, routes
├── api/
│   ├── routes/          # one module per HTTP surface
│   ├── deps.py          # JWT, admin, agent API-key dependencies
│   └── authz.py         # owned-kwami resolvers
├── core/                # settings, JWKS, domain errors
└── services/            # LiveKit, credits, channels, wallets, …
config/                  # LiveKit plugin YAML (inference, voices, languages)
migrations/              # numbered SQL, applied by scripts/migrate.py
docs/                    # architecture, API, security, billing, …
tests/
```

## Deployment

Deploys are automatic. A green `ci` run on `main` triggers [`cd.yml`](.github/workflows/cd.yml),
which cuts the version and the changelog with semantic-release, publishes the image to GHCR, and
deploys to Fly.io — in that order, all from the commit CI tested. Nothing is released or shipped
from a commit whose tests did not pass, and no version is ever bumped in a pull request.

- **Fly.io** — `fly.toml` names the production app. Secrets live in `fly secrets`, not the repo;
  `FLY_API_TOKEN` is the one GitHub needs. `make deploy` is the manual escape hatch.
- **GHCR** — `ghcr.io/kwami-labs/kwami-lk-api`, tagged with the version, the minor, `main` and the
  full commit SHA.
- **Docker** — `Dockerfile` builds the same image locally; set env via `--env-file`.

[docs/deployment.md](docs/deployment.md) is the pipeline in full. [CONTRIBUTING.md](CONTRIBUTING.md)
has the branch model, the release rules and what each CI check means. [SECURITY.md](SECURITY.md) is
how to report a vulnerability; [docs/security.md](docs/security.md) is the threat model.

## License

Apache-2.0. See [LICENSE](LICENSE).
