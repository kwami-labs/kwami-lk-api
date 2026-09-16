# Development

Contribution rules, branch model, and release policy live in
[CONTRIBUTING.md](../CONTRIBUTING.md). This page is the local loop:
install, run, test, and what the gate actually checks.

## Prerequisites

- Python **3.11** — [`.python-version`](../.python-version) pins it. `uv`,
  CI, and `python:3.11-slim` all use 3.11. Coverage percentages shift
  between interpreters, so an unpinned runtime makes the floors flaky.
- [uv](https://docs.astral.sh/uv/)
- Docker, for the throwaway Postgres and for image builds
- A filled-in `.env` from [`.env.sample`](../.env.sample) to run the
  server (tests use `tests/.env.test` via `KWAMI_ENV_FILE`)

## Setup

```bash
uv sync --extra dev
cp .env.sample .env          # then fill keys
make hooks                   # refuse a direct push to main
make test-db-up              # Postgres on 55433
make check                   # the whole gate
```

`make` with no target is `make check`. `make help` lists the rest.

## Run

```bash
make dev                     # uv run python -m src.main
# listens on API_HOST:API_PORT, default 0.0.0.0:8080
```

OpenAPI is at `/docs` and `/redoc` outside production. Set
`ENABLE_DOCS=true` if you need them with `APP_ENV=production`.

`APP_ENV` is `development` | `staging` | `production`. Outside
production, credits fail-open on a check error and wallets default on.
Neither is true in production unless you set the flags explicitly.

## Tests

Two tiers, one command each. There is no third "hit the real Stripe"
lane.

| Tier | Where | Command | Needs |
|------|-------|---------|-------|
| Unit | `tests/unit/`, `tests/api/`, `tests/core/` | `make test` | nothing |
| Integration | `tests/integration/` | `make test-integration` | Postgres on `TEST_DATABASE_URL` |

Unit tests fake outbound HTTP at the transport: **respx** for httpx
(Supabase, Zep, SendGrid), **responses** for requests (Stripe, Twilio),
**aioresponses** for aiohttp (LiveKit twirp). Do not add a fourth
library; pick the one that matches the client the code uses.

Integration tests apply `migrations/` and exercise RPCs, unique indexes,
triggers, and RLS. They **skip** when `TEST_DATABASE_URL` is unset —
that is what keeps `make test` offline. `make check` always sets it, so
an unreachable Postgres is a failure, not a quiet pass.

```bash
make test-db-up              # docker: postgres:16-alpine on 55433
make test                    # hermetic
make test-integration        # money invariants
make coverage                # both lanes, merged profile
make coverage-gate           # enforce coverage.floors
```

### Things that catch people out

**There is no `pytest-asyncio`.** Async tests run through anyio
(`@pytest.mark.anyio` plus the session `anyio_backend` fixture).
Starlette and httpx are anyio-native. Two async plugins competing to
collect the same coroutine silently skip tests. The dependency is
absent on purpose; `pyproject.toml` says so too.

**`filterwarnings = ["error"]` and `xfail_strict = true`.** A new
`DeprecationWarning` fails the suite unless it is from a dependency
already listed. An `xfail` that starts passing fails too.

**Ruff lints `ASYNC`.** Blocking HTTP, file, or sleep inside `async def`
is a defect — it is what made every Supabase call stall the event loop.
`B008` is ignored for FastAPI's `Depends(...)` defaults.

## Coverage

[`coverage.floors`](../coverage.floors) sets a minimum per module.
`make coverage-gate` enforces it against whatever `.coverage` currently
holds.

A module listed in neither the floors nor the exclusions **fails the
gate**. A new file needs a coverage decision on purpose. Add a floor,
or an `exclude` line with a comment above it saying why.

The floors are a **ratchet, not a target**. They were recorded from the
suite when the gate was added and rounded down. The rule is "this
module may not get worse"; the gate prints every module that now
measures above its floor so the next pull request can raise it.

Raising a floor needs no discussion. Lowering one is a reviewable
change — say why in the pull request.

Both lanes write separate profiles (`coverage.unit`,
`coverage.integration`) and `coverage combine` merges them.
`parallel` and `relative_files` in `pyproject.toml` are what make the
merge work here and across two CI runners.

## Project layout

```
src/                    application
  api/routes/           HTTP surfaces
  api/deps.py           JWT, admin, agent keys
  api/authz.py          owned-kwami dependencies
  core/                 settings, security, errors
  services/             integrations and ledger
config/                 LiveKit plugin YAML
migrations/             numbered SQL, applied by scripts/migrate.py
supabase/migrations/    hosted-project mirror
tests/
  unit/ api/ core/      hermetic
  integration/          Postgres
  fakes/                in-process Supabase stand-in
  factories/            tenant fixtures
scripts/                migrate, coverage-gate, set-version, rulesets
.github/workflows/      ci.yml, cd.yml
.github/rulesets/       main and tag protection
```

## Environment

See [`.env.sample`](../.env.sample) for the full list. The minimum to
boot:

```
LIVEKIT_URL
LIVEKIT_API_KEY
LIVEKIT_API_SECRET
```

Auth, memory, credits, telephony, email, and wallets each light up when
their keys are present. Missing optional groups 503 the feature rather
than crashing the process — except LiveKit, which is required.

Tests never read the live `.env`. `KWAMI_ENV_FILE` points them at
`tests/.env.test`.

## Style of change

Match the file you are in. Services raise `DomainError` subclasses;
routes do not catch `Exception` and return `detail=str(e)`. Shared
secrets compare with `hmac.compare_digest`. Ownership is a dependency
(`OwnedKwamiPath` / `OwnedKwamiQuery` / `require_kwami_owned`), not an
`if` in the handler.

If the change is an invariant — a unique index, an RPC, an RLS policy —
the test belongs in `tests/integration/`. An in-process fake can model
it; it cannot prove it.
