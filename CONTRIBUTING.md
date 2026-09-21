# Contributing

This repository is Apache-2.0 licensed — see [LICENSE](./LICENSE).
Report security issues privately — [SECURITY.md](./SECURITY.md). Do not open a public issue or
pull request for a vulnerability.

Long-form documentation is in [`docs/`](./docs/README.md). Local setup, the two test lanes, and
coverage floors are also spelled out in [docs/development.md](./docs/development.md).

## The rules, in one paragraph

`main` is protected: you cannot push to it. Every change arrives as a pull request from a branch,
the pull request title is a Conventional Commit, all CI checks pass, one reviewer approves, and it
lands as a single squashed commit. History on `main` is linear and never force-pushed.

Those rules are configuration, not etiquette — they live in [`.github/rulesets/`](.github/rulesets/)
and are applied with `make rules` (`./scripts/branch-protection.sh`).

## Branches

Branch off `dev`, named `<type>/<short-description>`:

```
feat/usage-report-idempotency
fix/stripe-refund-clawback
chore/bump-livekit
```

The type prefix matches the Conventional Commit types below. Branches are deleted on merge.

### `dev`, `stg` and `main`

**`main` is the branch that ships.** A green `ci` run on it cuts the version, publishes the image
and deploys to Fly.io. `dev` has a deploy job waiting for it — see below — and `stg` is tested and
reaches `cd` not at all.

They are not tested the same way. A push to `stg` or a pull request runs everything; a push to
`dev` runs the **fast lane** — `lint`, `unit` and `migrations` — and skips `integration`,
`coverage`, `vuln` and `build`. `dev` is the branch you push to repeatedly, and those cost minutes
each: a Postgres service container, a network fetch against the advisory database, a full image
build. The full suite still runs on every pull request, so nothing reaches `main` without it. The
fast lane is a shorter feedback loop, not a lower bar.

| | `lint` `unit` `migrations` | `integration` `coverage` `build` | `vuln` | `cd` |
|---|---|---|---|---|
| pull request | yes | yes | advisory | no |
| push `dev` | yes | **no** | **no** | deploy development (when configured) |
| push `stg` | yes | yes | advisory | no |
| push `main` | yes | yes | advisory | **release, publish, deploy production** |

`cd.yml` listens to `main` and `dev`. Release, publish and the production deploy are gated to
`main`; the development deploy is gated to `dev` and is deliberately not `needs:` those, since a
job that needs a skipped job is itself skipped.

**There is one Fly app today.** `deploy · development` skips — green — until `FLY_APP` is set as a
variable on the `development` GitHub Environment. It is the app name rather than the token that
enables the tier, because `FLY_API_TOKEN` is already a repository secret: without a second app
name there is nowhere for `dev` to go except production, and
[`.github/actions/fly-deploy`](.github/actions/fly-deploy/action.yml) refuses that outright rather
than falling back to `fly.toml`. Add a `staging` job beside it, and `stg` to the `cd.yml` trigger,
when a staging app exists.

## Commits and pull request titles

[Conventional Commits](https://www.conventionalcommits.org/). CI checks the **pull request title**,
because a squash merge is what lands on `main` — the title becomes the commit message, and the
individual commits on your branch do not survive.

```
<type>(<optional scope>): <description>

feat(credits): accept Idempotency-Key on usage reports
fix(stripe): store the payment intent so refunds can match
test(webhooks): cover the replayed signature path
```

Types: `feat` `fix` `docs` `style` `refactor` `perf` `test` `build` `ci` `chore` `revert`.
A `!` after the type or scope, or a `BREAKING CHANGE:` footer, marks a breaking change.

## Before you open a pull request

```bash
make test-db-up   # throwaway Postgres on 55433
make check
```

That is the whole gate: `ruff check`, `ruff format --check`, both test lanes, the merged coverage
profile against the per-module floors, and the migration dry run. CI runs the same things as
separate jobs, so a green `make check` is a green pipeline — with two exceptions that need the
network and therefore run only in CI: `make vuln` and the Docker build.

**The database is not optional.** The integration tests skip themselves when `TEST_DATABASE_URL`
is unset, which is what keeps `make test` fast and offline; `make check` always sets it, so an
unreachable Postgres is a failure rather than a quiet pass. Those tests are the only thing that
proves the money invariants, because the invariants are not in Python: `UPDATE ... WHERE balance
>= p_amount` in a single statement, partial unique indexes, `ON CONFLICT DO NOTHING`, RLS
policies. An in-process fake can model them; it cannot prove them.

## Tests

Two tiers today, one command each. Where a new test goes:

| Tier | Where | Use it for |
|---|---|---|
| Unit | `tests/unit/`, `tests/api/`, `tests/core/` | Anything that does not need a database. Outbound calls are faked at the transport — respx for httpx, responses for requests, aioresponses for aiohttp. |
| Integration | `tests/integration/` | Anything whose correctness depends on a real query running: a conditional `UPDATE`, a unique index, a trigger, an RLS policy, a migration. |

Two things about this suite catch people out:

- **There is no `pytest-asyncio`.** Async tests run through anyio (`@pytest.mark.anyio` plus the
  session `anyio_backend` fixture), which is what Starlette and httpx are native to. Two async
  plugins competing to collect the same coroutine silently skips tests; that is why the
  dependency is deliberately absent, and it is noted in `pyproject.toml` too.
- **The interpreter is pinned.** [`.python-version`](.python-version) says `3.11`, which is what
  `uv run` uses here, what CI uses, and what `python:3.11-slim` gives the release image. It is not
  only tidiness: coverage percentages shift between Python versions, so an unpinned interpreter
  would make the floors below flaky.
- **`filterwarnings = ["error"]` and `xfail_strict = true`.** A new `DeprecationWarning` fails the
  suite unless it is from a dependency already listed, and an `xfail` that starts passing fails
  too — so a marker cannot go stale after the fix ships.

## Coverage

[`coverage.floors`](coverage.floors) sets a minimum per module, and `make coverage-gate` enforces
it against the merged profile.

A module listed in neither the floors nor the exclusions **fails the gate**. That is deliberate: a
new module gets a coverage decision on purpose, never by being forgotten. So when you add one, add
its floor — or an `exclude` line with a comment above it saying why.

The floors are a **ratchet, not a target**. They were recorded from the suite as it stood when the
gate was added (38.5% overall), rounded down. The rule they encode is "this module may not get
worse"; the gate prints every module that now measures above its floor so the next pull request
can raise it. That is the opposite of the sibling Go services, where every floor is 100 — this
tree is not there yet, and a file full of aspirational hundreds would fail every build and teach
everyone to bypass it.

Raising a floor needs no discussion. Lowering one is a reviewable change: say why in the pull
request.

## CI

Every job in [`ci.yml`](.github/workflows/ci.yml) except `vuln` is a required status check:

| Check | What fails it |
|---|---|
| `pr-title` | The PR title is not a Conventional Commit |
| `lint` | `ruff check`, `ruff format --check`, or a `uv.lock` that no longer matches `pyproject.toml` |
| `unit` | The hermetic lane: no database, no network |
| `integration` | The migrations and the money invariants against a real Postgres |
| `coverage` | A module below its floor, or a module with no floor and no exclusion |
| `migrations` | Duplicate migration version prefixes, or an unorderable set |
| `build` | The release image does not build |
| `vuln` | **Advisory today.** pip-audit reports every advisory against the resolved dependency set with no reachability analysis — 78 of them across 15 packages as of writing, none introduced by a pull request. Requiring it now would mean every PR is red for something nobody in the PR can fix. Dependabot opens the bumps weekly; once the job is green, drop `continue-on-error` from `ci.yml` and add `vuln` to `main.json`. |

[`cd.yml`](.github/workflows/cd.yml) runs after `ci` goes green on `main`: it cuts the release,
publishes to GHCR, and deploys to Fly.

## Releases

Versions, tags, the GitHub Release and [`CHANGELOG.md`](CHANGELOG.md) are generated from the commit
history by [semantic-release](https://semantic-release.gitbook.io). Nothing is hand-maintained,
nothing needs a version bump in a pull request, and no tag is ever pushed by hand — which is exactly
why the PR title has to be a Conventional Commit: a non-conventional subject is silently
unreleasable work.

```text
merge a PR into main
        │
        ▼
      ci.yml ── red ──▶ nothing
        │ green
        ▼
      cd.yml
        ├─ release   semantic-release → CHANGELOG.md + tag vX.Y.Z + GitHub Release
        ├─ publish   one image build, tagged with the version that was just cut
        ├─ deploy    flyctl deploy --remote-only          (the live origin)
        └─ deploy    wrangler deploy (Worker + Container) (dark until a domain is attached)
```

The service is **1.x**, so [`.releaserc.json`](.releaserc.json) maps plain semver:

| Commit | Bump | Example |
|---|---|---|
| `feat!:` or a `BREAKING CHANGE:` footer | **major** | `1.4.2 → 2.0.0` |
| `feat:` | **minor** | `1.4.2 → 1.5.0` |
| every other conventional type | **patch** | `1.4.2 → 1.4.3` |

Every conventional type is releasable — a `test:` or `refactor:` still ships a patch — but only
`feat`, `fix`, `perf` and `revert` get a heading in the changelog; the rest bump the version
silently. Breaking changes get their own section.

The preset is **`conventionalcommits`**, not `angular`, and the difference is not cosmetic: the
`angular` preset does not understand the `!` marker at all. Under it a `feat!:` title parsed as no
recognised type, matched no release rule, and cut **no release whatsoever** — while `publish` and
`deploy` still ran, shipping the change under the previous version number. `conventionalcommits`
reads `!` as breaking, so the title alone is enough and the footer is belt-and-braces.

The preset is also not bundled with semantic-release, which ships only `angular`. `cd.yml`
installs `conventional-changelog-conventionalcommits` explicitly in its `npx -p` list; drop that
line and the release fails at plugin load.

`v0.1.0` is a baseline tag at the commit that introduced this automation, and `v0.1.1` is the last
release of the pre-1.0 line. Without a baseline semantic-release would treat the repository as a
fresh 1.0.0 and pull the entire pre-automation history into the first changelog; the first `cd` run
creates it if it is missing.

The version number itself is stated in four places, and
[`scripts/set-version.sh`](scripts/set-version.sh) writes all of them in one go —
`pyproject.toml`, `uv.lock`, `src/__init__.py` and the README badge. `uv.lock` is the one that
matters: it mirrors the project version out of `pyproject.toml`, so bumping one without the other
makes `uv lock --check` fail, and that is a required status check. `src/__init__.py` is what the
running service reports through `/docs` and the startup log.

| Image tag | When |
|---|---|
| `1.4.2`, `1.4` | a version was cut on this run |
| `main` | every green run on `main` |
| `sha-<full sha>` | every green run on `main` |

GHCR is the archive, not what Fly runs: `flyctl deploy --remote-only` builds the same Dockerfile
from the same commit on Fly's own builders. Pointing Fly at the GHCR image instead would need
registry credentials on the Fly side for a package that is private by default — the trade taken
here is a second build rather than a second credential.

Two things follow from the release commit being pushed with `GITHUB_TOKEN`, which by design triggers
no workflow. It is what stops `cd → tag → cd`. It also means the `chore(release):` commit is never
itself built or deployed — the image and the deploy both come from the commit `ci` tested, one
behind. The tag, the release and the changelog are all correct; only the deployed tree is a
changelog commit short.

### The release needs `RELEASE_TOKEN`

That push lands on protected `main` — a release cannot open a pull request for itself — so
something has to bypass the rule. On most repositories that something is the `github-actions`
app, named as a bypass actor. **This organisation cannot do that.** The Actions app is not
installed on `kwami-labs`, and naming it makes GitHub reject the entire ruleset:

```
422  Actor GitHub Actions integration must be part of the ruleset source or owner organization
```

So the bypass actor in [`main.json`](.github/rulesets/main.json) is the **repository admin
role**, and the release has to push *as* an admin. `GITHUB_TOKEN` does not: it acts as
`github-actions[bot]`, which holds no repository role and is not covered.

Hence one secret:

| Secret | What it is |
|---|---|
| `RELEASE_TOKEN` | A **fine-grained personal access token** owned by a repository admin, scoped to this repository, with **Contents: Read and write**. `cd.yml` checks out and pushes with it, and semantic-release authenticates with it. |

Create it at **Settings → Developer settings → Personal access tokens → Fine-grained tokens**, then
add it under **Settings → Secrets and variables → Actions**.

Without it, `cd.yml` refuses to start a release whenever a branch ruleset is active, rather than
tagging and then failing to push — which would leave a tag and a GitHub Release whose changelog
commit never landed, and a next run that computes its version from that tag. On an unprotected
`main` the workflow falls back to `GITHUB_TOKEN` and warns.

`scripts/branch-protection.sh` drops an Integration bypass actor that the organisation has not
installed, with a warning, rather than letting the API reject the whole payload — so the same
rulesets stay portable to a repository whose org *does* install it.

## Applying the rules to the repository

```bash
make rules                                  # strict: 1 approval, all required checks
DRY_RUN=1 ./scripts/branch-protection.sh    # print the payloads, change nothing
APPROVALS=0 ./scripts/branch-protection.sh  # solo repo — CI still gates every merge
```

Re-run it after editing anything in `.github/rulesets/`; it reconciles rather than duplicating.

This repository is **public**, so rulesets apply to it on a free plan — `GET /repos/…/rulesets`
answers `200 []` rather than the `403 Upgrade to GitHub Pro` a private repository on a free plan
gets. That is worth stating because the sibling Go services are private on a free organisation and
cannot apply theirs at all: their `.github/rulesets/` is aspirational, and this one is not.

Until `make rules` has actually been run, though, `main` is unprotected and the JSON in this
repository is just JSON. `gh api repos/kwami-labs/kwami-lk-api/rulesets` answering `[]` is what
"not applied yet" looks like; a named ruleset in that list is what applied looks like.

**On a solo repository, use `APPROVALS=0`.** The default asks for one approving review and sets
`require_last_push_approval`, which nobody can satisfy alone — CI still gates every merge, and the
pull request is still the only way in.

### The local stand-in

Whether or not the ruleset is applied, every clone should install the hook once:

```bash
make hooks
```

That points `core.hooksPath` at [`.githooks/`](.githooks/), whose `pre-push` refuses any push that
would land on `main` and tells you how to open a branch instead.

Be clear about what it is worth. It lives in the working copy, so it protects whoever installed it
and nobody else, and `git push --no-verify` walks straight past it. It is a guardrail against the
accident, not a control against intent — the ruleset is the control, and the checks in `ci.yml`
are what establish that a change is good.
