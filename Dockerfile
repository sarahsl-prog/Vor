FROM python:3.13-slim

# uv binary, minor-pinned to match local / CI.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Resolve dependencies first so this layer is cached unless the lock changes.
# --no-dev drops the [dependency-groups] dev tools from the image.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# Run the app straight from the synced venv.
ENV PATH="/app/.venv/bin:$PATH"

# Cloud Run sets $PORT; default to 8080 for local runs.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
