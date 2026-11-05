FROM ghcr.io/astral-sh/uv:0.11.3 AS uv-bin


FROM python:3.11-slim AS python-base

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv-bin /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY scripts ./scripts


FROM python-base AS runtime

ARG SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.revision=$SOURCE_COMMIT

# The runtime image serves both inbound surfaces from one build, so it carries
# the telegram and api extras. The data-build toolchain stays out of it entirely.
RUN uv sync --locked --no-dev --no-editable --extra telegram --extra api --no-cache \
    && python -c "from pathlib import Path; import shutil; [shutil.rmtree(p) for p in Path('/app/.venv').rglob('__pycache__') if p.is_dir()]" \
    && rm -f /usr/local/bin/uv /usr/local/bin/uvx

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["bot"]


FROM python-base AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN uv sync --locked --no-dev --no-editable --extra build --no-cache \
    && python -c "from pathlib import Path; import shutil; [shutil.rmtree(p) for p in Path('/app/.venv').rglob('__pycache__') if p.is_dir()]"

COPY deploy/builder-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["verify-db"]
