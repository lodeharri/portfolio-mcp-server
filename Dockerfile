# Multi-stage Dockerfile — final image target <150 MB.
#
# Stage 1 (builder): install build deps, compile wheels.
# Stage 2 (runtime): python:3.10.12-slim, copy wheels, run as non-root.
#
# Free-tier / size discipline:
#   - python:3.10.12-slim (Debian) is the smallest base that still has
#     the .so runtime libraries sqlite-vss needs.
#   - No Node, no Playwright, no Playwright browsers in the final image.
#     Those run in CI only.
#   - We pre-bake a vector index at build time (data/index.sqlite) using
#     scripts/preindex.py so the runtime server has zero cold-start work.

# ---------- Stage 1: builder ----------
FROM python:3.10.12-slim AS builder

WORKDIR /build

# System deps for wheel builds. Slim image lacks these.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy only the project metadata first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src

# Build a wheel and install into a venv we copy to the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# ---------- Stage 2: runtime ----------
FROM python:3.10.12-slim AS runtime

# Run as non-root. UID 10001 is Fly's preferred unprivileged UID.
RUN groupadd --system --gid 10001 mcp && \
    useradd  --system --uid 10001 --gid mcp --create-home mcp

WORKDIR /app

# Copy the prebuilt venv from the builder.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy runtime code and the manifest.
COPY --chown=mcp:mcp src ./src
COPY --chown=mcp:mcp config ./config
COPY --chown=mcp:mcp pyproject.toml README.md ./

# Pre-bake the vector index. Requires GEMINI_API_KEY at build time; pass
# via `--build-arg GEMINI_API_KEY=...` or a Fly build secret. The resulting
# data/index.sqlite is what the runtime serves from.
ARG GEMINI_API_KEY
ENV GEMINI_API_KEY=${GEMINI_API_KEY}
RUN python -m mcp_server.interfaces.cli.preindex || echo "WARN: preindex skipped (no API key)"

USER mcp

EXPOSE 8080

# Healthcheck uses the FastAPI /healthz route. Pika-style.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://localhost:8080/healthz', timeout=4).status_code==200 else 1)"

# Start uvicorn. The --workers 1 keeps the 256 MB machine happy.
CMD ["uvicorn", "mcp_server.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
