# kwami-lk-api — Changelog

All notable changes to this service are documented here. This file is generated from the commit history by semantic-release — do not edit it by hand.

`v0.1.0` is a baseline tag placed at the commit that introduced this automation. Everything before it lives in the git log rather than here: it shipped before there was a release line to put it on.

The versioning rules, and why breaking changes bump minor while the service is pre-1.0, are in [docs/deployment.md](docs/deployment.md#release) and [CONTRIBUTING.md](CONTRIBUTING.md#releases).

## [0.1.0] — 2026-09-17

Baseline release. The service already included, at the tag:

- LiveKit token issuance with server-side room claims (`livekit_sessions`)
- Model, voice, and language catalogs derived from LiveKit plugins
- Zep-backed memory with anchored tenancy checks
- Credits ledger in Postgres (`add_credits` / `deduct_credits`), Stripe Checkout, usage settlement
- Channels (phone, SMS, WhatsApp), email Smart Hub, calendar, contacts
- Optional Solana custody wallets
- Twilio and SendGrid webhooks, signature-verified, replay-guarded
- Admin provider-invoice reconciliation
- CI / CD: Conventional Commits, coverage floors, semantic-release, GHCR, Fly.io

Subsequent versions are appended below this heading by semantic-release.
