FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client git \
    && rm -rf /var/lib/apt/lists/*

# Pinned uv binary for locked, reproducible installs (issue #319) — image build
# must NOT re-resolve from PyPI, otherwise the lock does not govern containers.
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv

WORKDIR /app

# Install runtime deps from uv.lock first (cached layer, no project source yet).
# `--no-dev` keeps dev tools (pytest/ruff/playwright) out of the production image.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY src/ src/
RUN uv sync --locked --no-dev

# Run from the uv-managed venv without an `uv run` prefix.
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8002 8003

CMD ["python", "-m", "src.mcp.server"]
