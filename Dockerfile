# Kwami LK API - Token Endpoint Server
#
# One image, three consumers: Fly builds it on its own builders, `cd.yml` publishes it to GHCR,
# and infra/wrangler.jsonc points a Cloudflare Container at this same file. A change here lands
# on all three.
#
# Two stages. The builder resolves and installs the virtualenv; the runtime copies it and nothing
# else, so uv, the build cache and the lockfile never reach the shipped image.

# Pinned by digest, not by tag. `python:3.11-slim` moves, so two builds of the same commit could
# previously resolve different base layers -- exactly what a version number is supposed to rule
# out, and the reason `uv` below was already pinned. Refresh deliberately, with the tag alongside
# so a human can read which release it is.
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9 AS builder

# Pinned, not `:latest`, for the same reason as the base image.
COPY --from=ghcr.io/astral-sh/uv:0.12.16 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency files first, so this layer is cached until they actually change.
COPY pyproject.toml uv.lock ./

# `--no-dev` keeps pytest, ruff and the rest of the dev extra out of the image.
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9 AS runtime

# curl is here for HEALTHCHECK below. It was previously installed and never used.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Nothing in this service needs root at runtime. The process binds 8080, which is unprivileged,
# and writes nothing outside the virtualenv.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

WORKDIR /app

# The resolved environment, and then the source. Source changes rebuild only the last layer.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app . .

USER app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

ENV APP_ENV=production
ENV API_HOST=0.0.0.0
ENV API_PORT=8080

# Liveness for any runtime that reads it -- Cloudflare Containers and a plain `docker run` do;
# Fly uses the checks in fly.toml instead. Deliberately the shallow endpoint: a dependency
# outage must not make the container restart in a loop. `/health/ready` is the deep one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8080/health || exit 1

# The venv is on PATH, so this runs the installed interpreter directly -- no `uv run`, which
# would want to re-resolve and write a lockfile the app user cannot write.
CMD ["python", "-m", "src.main"]
