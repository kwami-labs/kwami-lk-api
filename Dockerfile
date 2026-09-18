# Kwami LK API - Token Endpoint Server
#
# One image, three consumers: Fly builds it on its own builders, `cd.yml` publishes it to GHCR,
# and infra/wrangler.jsonc points a Cloudflare Container at this same file. A change here lands
# on all three.

FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Pinned, not `:latest`. The release image is meant to be reproducible: with a floating tag, two
# builds of the same commit can resolve different uv versions, which is exactly the thing a
# version number is supposed to rule out.
COPY --from=ghcr.io/astral-sh/uv:0.12.16 /uv /usr/local/bin/uv

# Nothing in this service needs root at runtime. The process binds 8080, which is unprivileged,
# and writes nothing outside the virtualenv.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

WORKDIR /app

# Copy dependency files first for better caching
COPY --chown=app:app pyproject.toml uv.lock ./

# Install dependencies (this layer will be cached if dependencies don't change)
RUN uv sync --frozen --no-dev && chown -R app:app /app

# Copy application code (this layer will be rebuilt when code changes)
COPY --chown=app:app . .

USER app

# uv would otherwise re-resolve and try to write the lockfile at `uv run` time, which the app
# user cannot do -- and should not: the image ships the environment it was built with.
ENV UV_FROZEN=1 \
    UV_NO_SYNC=1 \
    PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

ENV APP_ENV=production
ENV API_HOST=0.0.0.0
ENV API_PORT=8080

# Run as module (project.scripts not installed when project isn't packaged)
CMD ["uv", "run", "python", "-m", "src.main"]
