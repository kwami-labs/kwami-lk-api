# Kwami LK API - Token Endpoint Server

FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for better caching
COPY pyproject.toml uv.lock ./

# Install dependencies (this layer will be cached if dependencies don't change)
RUN uv sync --frozen --no-dev

# Copy application code (this layer will be rebuilt when code changes)
COPY . .

EXPOSE 8080

ENV APP_ENV=production
ENV API_HOST=0.0.0.0
ENV API_PORT=8080

# Run as module (project.scripts not installed when project isn't packaged)
CMD ["uv", "run", "python", "-m", "src.main"]
