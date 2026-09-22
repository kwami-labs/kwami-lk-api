# kwami-lk-api — Changelog

All notable changes to this service are documented here. This file is generated from the commit history by semantic-release — do not edit it by hand.

The service is 1.x: `feat!` or a `BREAKING CHANGE:` footer bumps the major, `feat` the minor, and every other conventional type a patch. `v0.1.0` and `v0.1.1` are the pre-1.0 line; everything before `v0.1.0` lives in the git log rather than here.

## [0.1.1](https://github.com/kwami-labs/kwami-lk-api/compare/v0.1.0...v0.1.1) (2026-09-16)

Dependency and tooling bumps only; no `feat`, `fix`, `perf` or `revert` commit, so
semantic-release cut the patch without a heading under it.

## [0.1.0](https://github.com/kwami-labs/kwami-lk-api/releases/tag/v0.1.0) (2026-09-16)

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
