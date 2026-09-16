# Security policy

Contribution rules live in [CONTRIBUTING.md](./CONTRIBUTING.md). Licence terms are in
[LICENSE](./LICENSE). The threat model, auth classes, and money invariants are in
[docs/security.md](./docs/security.md).

## Reporting a vulnerability

**Do not open a public issue or pull request for a security problem.**

Report it privately through
[GitHub Security Advisories](https://github.com/kwami-labs/kwami-lk-api/security/advisories/new),
or contact the repository owner directly. Please include:

- what the issue is and which surface it affects (the LiveKit token endpoint, the Supabase JWT
  boundary, the Stripe or Twilio or SendGrid webhooks, the credits and wallet ledger, Postgres,
  or CI);
- reproduction steps or a proof of concept;
- the impact you believe it has, and any suggested fix.

Expect an acknowledgement within a few days. Please give us a reasonable window to ship a fix
before discussing the issue anywhere else.

## Supported versions

Only `main` is deployed. `dev` and `stg` are tested; `dev` has a deploy job that stays skipped
until a second Fly app exists. See [Releases](./CONTRIBUTING.md#releases).

Fixes land on `main` and ship on the next green run. Do not assume a vulnerability is patched on
the deployed service until the matching commit is on `main` and `cd` has gone green.

## The surfaces worth knowing about

- **Token issuance** (`/token`) mints LiveKit JWTs. It is the one endpoint whose output is a
  credential, and it decides which room a participant may join.
- **Webhooks** (`/webhooks/...`) take unauthenticated input. Stripe is verified by signature with
  a timestamp window; Twilio is verified against the public URL and **fails closed** when the auth
  token is unset. Both are replay-guarded through `payment_events` / `usage_reports` uniqueness
  rather than in Python.
- **Money** is settled in SQL. `deduct_credits` cannot overdraw because of
  `UPDATE ... WHERE balance >= p_amount` in a single statement; crediting is idempotent because of
  a unique index and `ON CONFLICT DO NOTHING`; tenants are separated by RLS. The tests that prove
  this are in `tests/integration/db/` and run on every pull request — a change here that only
  passes the unit lane has not been tested.

## Handling secrets

- Never commit `.env` files, tokens, or keys. `.gitignore` blocks `.env` and `.env.*` except
  [`.env.sample`](./.env.sample), which holds names and placeholder values only.
- Runtime configuration is validated at boot by [`src/core/config.py`](./src/core/config.py). The
  process refuses to start when a required variable is missing, rather than starting and 500ing.
- Deploy credentials live in GitHub (`FLY_API_TOKEN`) and on Fly (`fly secrets`), not in the repo.
  `GITHUB_TOKEN` covers the GHCR publish.
- `RELEASE_TOKEN` is a fine-grained PAT owned by a repository admin, with **Contents: Read and
  write** on this repository and nothing else. It exists only because the release commit is pushed
  to protected `main` and `github-actions[bot]` cannot bypass the ruleset — see
  [CONTRIBUTING.md](./CONTRIBUTING.md#the-release-needs-release_token). Scope it to this repository,
  give it an expiry, and rotate it on the same schedule as any other deploy credential. A leaked
  one can rewrite `main`.
- Rotate anything that reaches a third party in both places at once: `LIVEKIT_API_SECRET`,
  `SUPABASE_SECRET_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `TWILIO_AUTH_TOKEN`,
  `SENDGRID_API_KEY`, `ZEP_API_KEY`, `KWAMI_API_KEY`, and the wallet signing and webhook secrets.
- `STRIPE_WEBHOOK_SECRET` and `SENDGRID_INBOUND_WEBHOOK_SECRET` are what make an unauthenticated
  endpoint safe. A deployment with either unset accepts forged events; treat an unset one as an
  incident, not a configuration gap.
- Dependency advisories are reported by the `vuln` job on every pull request. It is advisory
  rather than blocking today — see the table in [CONTRIBUTING.md](./CONTRIBUTING.md#ci) — which
  means somebody has to read it.

If you believe a secret has been exposed, rotate it first, then report it.
