# Deployment

Nothing is released or shipped from a commit whose tests did not pass.
`cd.yml` is triggered by `workflow_run` on `ci`, not by a second `push`.

## Pipeline

```mermaid
flowchart TD
  PR["pull request"] --> CI["ci.yml"]
  DEV["push to dev"] --> FAST["ci fast lane: lint, unit, migrations"]
  STG["push to stg"] --> CI
  MAIN["push to main"] --> CI

  CI -->|red| STOP[nothing]
  CI -->|green on main| CD["cd.yml"]
  FAST -->|green on dev| DEVDEP["deploy development when FLY_APP is set"]

  CD --> REL["semantic-release: tag + CHANGELOG + GitHub Release"]
  REL --> PUB["docker build, push GHCR"]
  PUB --> FLY["flyctl deploy --remote-only"]
  PUB --> CF["wrangler deploy (Worker + Container)"]
```

Two production targets run side by side. Fly is the live origin; the
Cloudflare Worker is deployed on every green `main` run and stays dark
until a custom domain is attached to it. Both build the same
`Dockerfile` from the same commit.

| Event | lint / unit / migrations | integration / coverage / build | vuln | cd |
|-------|--------------------------|--------------------------------|------|----|
| pull request | yes | yes | advisory | no |
| push `dev` | yes | no | no | development deploy, if configured |
| push `stg` | yes | yes | advisory | no |
| push `main` | yes | yes | advisory | **release, publish, deploy production** |

The fast lane on `dev` is a shorter feedback loop, not a lower bar. Every
pull request still runs the full suite, so nothing reaches `main` without
it.

`main` CI runs are never cancelled. A force-push on `dev` or `stg`
cancels the run it superseded.

## Release

Versions, tags, the GitHub Release, and [CHANGELOG.md](../CHANGELOG.md)
are generated from Conventional Commit history by semantic-release.
Nothing is hand-maintained. The **pull request title** is the commit that
lands — squash merge is required — so a non-conventional title is
silently unreleasable work.

The service is **1.x**. [`.releaserc.json`](../.releaserc.json) maps plain
semver: `feat!:` or a `BREAKING CHANGE:` footer bumps the **major**, `feat:`
the **minor**, and every other conventional type a **patch**. Every
conventional type is releasable; only `feat`, `fix`, `perf`, `revert`, and
breaking changes get a heading in the changelog.

The preset is `conventionalcommits`, not `angular`. `angular` does not
understand the `!` marker: under it a `feat!:` title cut no release at all
while `publish` and `deploy` still ran, shipping the change under the
previous version. The preset is not bundled with semantic-release either —
`cd.yml` installs `conventional-changelog-conventionalcommits` in its
`npx -p` list.

`v0.1.0` is a baseline tag at the commit that introduced this automation,
and `v0.1.1` is the last release of the pre-1.0 line. Without a baseline,
semantic-release would treat the repository as a fresh 1.0.0 and pull the
entire pre-automation history into the first changelog.

`scripts/set-version.sh` writes the version in four places:

- `pyproject.toml`
- `uv.lock` (the one `uv lock --check` gates)
- `src/__init__.py` (what the running service reports)
- the README badge

The release commit is pushed with a token that triggers no workflow. That
stops `cd → tag → cd`. It also means the `chore(release):` commit is never
itself built — the image and the deploy come from the commit `ci` tested,
one behind. The tag and changelog are correct; the deployed tree is a
changelog commit short.

That push lands on protected `main`, so something has to bypass the
pull-request rule. It is **not** the `github-actions` app: that app is not
installed on the `kwami-labs` organisation, and naming it makes GitHub
reject the whole ruleset with `422`. The bypass actor in
[`.github/rulesets/main.json`](../.github/rulesets/main.json) is the
**repository admin role**, and the release pushes as an admin using
`RELEASE_TOKEN` — a fine-grained PAT with `Contents: Read and write`.
`cd.yml` refuses to start a release without it whenever a branch ruleset is
active. See [CONTRIBUTING.md](../CONTRIBUTING.md#the-release-needs-release_token).

## Images

| Tag | When |
|-----|------|
| `1.4.2`, `1.4` | a version was cut on this run |
| `main` | every green run on `main` |
| `sha-<full sha>` | every green run on `main` |

Registry: `ghcr.io/kwami-labs/kwami-lk-api`.

GHCR is the archive, not what Fly runs. `flyctl deploy --remote-only`
builds the same `Dockerfile` from the same commit on Fly's builders.
Pointing Fly at GHCR would need registry credentials on the Fly side for
a package that is private by default — the trade is a second build
rather than a second credential.

The image is `python:3.11-slim`, installs `uv`, runs
`uv sync --frozen --no-dev`, and starts with
`uv run python -m src.main`. `.python-version` is `3.11` locally and in
CI for the same reason: coverage percentages shift between interpreters.

Two things in it are deliberate. The `uv` binary comes from a **pinned**
`ghcr.io/astral-sh/uv` tag rather than `:latest` — with a floating tag,
two builds of the same commit can resolve different uv versions, which is
the thing a version number exists to rule out. And the process runs as
the unprivileged `app` user (uid 10001), not root: it binds 8080, which
needs no privilege, and writes nothing outside the virtualenv.
`UV_FROZEN` and `UV_NO_SYNC` stop `uv run` from trying to re-resolve and
rewrite the lockfile at start-up, which that user cannot do and should
not — the image ships the environment it was built with.

## Fly.io

[`fly.toml`](../fly.toml) names the production app (`kwami-ai-api`) in
`lhr`.

| Setting | Value | Why |
|---------|-------|-----|
| `internal_port` | 8080 | matches `API_PORT` |
| `force_https` | true | Twilio signs the HTTPS URL |
| `auto_stop_machines` | stop | scale to zero |
| `min_machines_running` | 0 | same |
| HTTP check | `GET /health` every 30s | liveness |
| VM | 1 shared CPU, 512 MB | current size |

Secrets live in `fly secrets`, not the repo. `FLY_API_TOKEN` is the
GitHub secret. `make deploy` is the manual escape hatch.

There is one Fly app today. `deploy · development` skips — green — until
`FLY_APP` is set as a variable on the `development` GitHub Environment.
The app name, not the token, enables the tier: `FLY_API_TOKEN` is already
a repository secret, and without a second app name `dev` would ship to
production. [`.github/actions/fly-deploy`](../.github/actions/fly-deploy/action.yml)
refuses that fallback.

Add a `staging` job and `stg` to the `cd.yml` trigger when a staging app
exists.

Uvicorn runs with `proxy_headers=True` and `forwarded_allow_ips="*"`.
Only the Fly proxy can reach the port. Without forwarded headers,
`request.url.scheme` stays `http` and Twilio signature checks fail.

## Cloudflare

The same FastAPI image runs as a [Cloudflare Container] behind a Worker.
[`infra/`](../infra) holds the whole target: `wrangler.jsonc`, the Worker
in `src/index.ts`, and Terraform for the DNS and custom-domain side.

| Piece | Where |
|-------|-------|
| Worker + Container config | [`infra/wrangler.jsonc`](../infra/wrangler.jsonc) |
| Worker entry (proxies to the container) | [`infra/src/index.ts`](../infra/src/index.ts) |
| Container env from Worker vars/secrets | [`infra/src/container-env.ts`](../infra/src/container-env.ts) |
| Secrets upload | [`infra/scripts/put-secrets.sh`](../infra/scripts/put-secrets.sh) |
| DNS / custom domain | [`infra/terraform/`](../infra/terraform) |

`KwamiApiContainer` is a Durable Object owning one container instance on
port 8080, with `/health` as its ping endpoint and `sleepAfter = "10m"`.
The Worker sets `X-Forwarded-Proto`, `X-Forwarded-Host` and
`X-Forwarded-For` from `CF-Connecting-IP` before proxying — the same
reason Fly needs `proxy_headers=True`: Twilio signs the HTTPS URL, so a
request that arrives looking like `http` fails signature validation.

The container image is `"image": "../Dockerfile"` — the same file Fly and
GHCR build. One Dockerfile, three consumers.

Deploys go through
[`.github/actions/cloudflare-deploy`](../.github/actions/cloudflare-deploy/action.yml),
which follows the same two rules as the Fly action: a tier with neither
`CLOUDFLARE_API_TOKEN` nor `CLOUDFLARE_ACCOUNT_ID` skips green, and a
tier with only one of them is a failure rather than a silent skip.

| GitHub Environment | Wrangler env | Worker |
|---|---|---|
| `production` | `production` | `kwami-lk-api` |
| `development` | `development` | `kwami-lk-api-dev` |

Run it by hand from `infra/` with `pnpm install && pnpm exec wrangler deploy --env <env>`.

[Cloudflare Container]: https://developers.cloudflare.com/containers/

## Required status checks

Every job in `ci.yml` except `vuln` is required on `main`:

| Check | Fails when |
|-------|------------|
| `pr-title` | title is not a Conventional Commit |
| `lint` | ruff, or `uv.lock` no longer matches `pyproject.toml` |
| `unit` | hermetic lane fails |
| `integration` | migrations or money invariants fail against Postgres |
| `coverage` | a module is below its floor, or has no floor and no exclusion |
| `migrations` | duplicate or unorderable prefixes |
| `build` | the release image does not build |
| `vuln` | advisory — pip-audit, `continue-on-error` |

Apply the ruleset with `make rules` (`./scripts/branch-protection.sh`).
Until that has been run, `main` is unprotected and the JSON in
`.github/rulesets/` is just JSON.

## Local stand-in

```bash
make hooks    # refuse a direct push to main
make rules    # apply the GitHub ruleset (needs admin)
```

The hook lives in the working copy. `git push --no-verify` walks past it.
It is a guardrail against the accident; the ruleset is the control.
