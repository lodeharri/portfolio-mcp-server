# Multi-stage Dockerfile — final image target <150 MB.
#
# Stage 1 (builder): install build deps, compile wheels, bake the index.
# Stage 2 (runtime): python:3.10.12-slim, copy wheels + index, non-root.
#
# Free-tier / size discipline:
#   - python:3.10.12-slim (Debian) is the smallest base that still has
#     the .so runtime libraries sqlite-vss needs.
#   - No Node, no Playwright, no Playwright browsers in the final image.
#     Those run in CI only.
#   - We pre-bake a vector index at build time (data/index.sqlite) using
#     `python -m mcp_server.interfaces.cli.preindex` so the runtime
#     server has zero cold-start work.
#   - BuildKit `--mount=type=cache` keeps the pip cache out of the
#     builder layer; BuildKit `--mount=type=secret` keeps the
#     GEMINI_API_KEY out of the image layers.
#
# Build arguments:
#   --build-arg BAKE_INDEX=on (default)         Run preindex in the builder.
#   --build-arg BAKE_INDEX=off                  Skip preindex (faster CI).
#   --build-arg GITLEAKS_VERSION=8.18.4         Gitleaks Go binary version.
#
# Build secrets (BuildKit required, Docker >= 23.0):
#   --secret id=gemini,env=GEMINI_API_KEY       Injected as /run/secrets/gemini
#                                               only during the preindex RUN.
#                                               NEVER lands in the image.
#
# Runtime contract:
#   - Platform-agnostic PORT (Fly 8080, HF Spaces 7860, Render 10000).
#   - CMD uses shell form so $PORT is expanded at runtime.
#   - HEALTHCHECK uses os.environ.get("PORT") so it tracks the platform.
#   - Image runs as UID 10001 (mcp) — non-root by default.

# syntax=docker/dockerfile:1.7
# ^^^ Required for BuildKit --mount=type=secret and --mount=type=cache.

# ---------- Stage 1: builder ----------
FROM python:3.10.12-slim AS builder

# Gitleaks version for the secret scanner used by the preindex pipeline.
# Matches the version pinned in pyproject.toml / docs.
ARG GITLEAKS_VERSION=8.18.4

# Build-time switch: bake the vector index? Default ON. CI can pass
# --build-arg BAKE_INDEX=off to skip the preindex step (saves time on
# PRs that don't touch indexing).
ARG BAKE_INDEX=on

WORKDIR /build

# System deps for wheel builds. Slim image lacks these.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the gitleaks Go binary from the GitHub release. The scanner
# lives in src/mcp_server/security/gitleaks_scanner.py and shells out
# to ``gitleaks`` via subprocess. We only need it in the BUILDER stage
# for the preindex step; the runtime stage does NOT carry it.
RUN curl -fsSL \
    "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
        -o /tmp/gitleaks.tar.gz \
    && tar -xzf /tmp/gitleaks.tar.gz -C /tmp \
    && mv /tmp/gitleaks /usr/local/bin/gitleaks \
    && chmod +x /usr/local/bin/gitleaks \
    && rm -f /tmp/gitleaks.tar.gz /tmp/gitleaks \
    && gitleaks version

# Copy only the project metadata first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY scripts ./scripts

# Build a venv with pip (needed for the install below), then drop pip
# after install to save ~12 MB at runtime. The runtime only uses the
# installed packages directly via the venv's Python interpreter.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    # Drop pip from the venv after install — runtime doesn't need it.
    && rm -rf /opt/venv/lib/python3.10/site-packages/pip \
    && rm -rf /opt/venv/lib/python3.10/site-packages/pip-* \
    && rm -rf /opt/venv/bin/pip* \
    && rm -rf /opt/venv/lib/python3.10/site-packages/_distutils_hack 2>/dev/null || true

# Pre-bake a schema-only vector index. The build context does NOT
# include the sibling project trees (absolute paths in the manifest
# pointing outside the build context), so the baked index is always
# empty — the runtime server populates it with real embeddings via a
# mounted volume (Fly.io) or persistent disk (HF Spaces). See spec
# scenario "Build without GEMINI_API_KEY still succeeds".
#
# We use a small helper script (scripts/bake_schema.py) instead of
# running `preindex` directly because (a) preindex requires the project
# trees mounted at build time which we don't have, and (b) the script
# is faster and only creates the schema.
RUN mkdir -p /build/data
RUN --mount=type=secret,id=gemini \
    /opt/venv/bin/python /build/scripts/bake_schema.py /build/data/index.sqlite

# ---------- Stage 2: runtime ----------
FROM python:3.10.12-slim AS runtime

# Run as non-root. UID/GID 10001 is Fly.io's recommended unprivileged
# UID and is below the 65535 cap that breaks on some shared kernels.
RUN groupadd --system --gid 10001 mcp && \
    useradd  --system --uid 10001 --gid mcp --create-home mcp

WORKDIR /app

# Copy the prebuilt venv from the builder.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Copy runtime code and the manifest.
COPY --chown=mcp:mcp --from=builder /build/src ./src
COPY --chown=mcp:mcp --from=builder /build/config ./config
COPY --chown=mcp:mcp --from=builder /build/pyproject.toml ./
COPY --chown=mcp:mcp --from=builder /build/README.md ./

# Copy the baked index. The preindex step above always produces
# /build/data/index.sqlite (real OR empty), so a plain COPY works.
# The runtime server boots with an empty vector store if BAKE_INDEX=off
# and /healthz returns 200 (the index is a soft dependency for tools,
# not for the probe). See spec scenario "Build without GEMINI_API_KEY
# still succeeds".
COPY --chown=mcp:mcp --from=builder /build/data/index.sqlite ./data/index.sqlite

USER mcp

# EXPOSE only accepts literals. The platform-specific PORT is documented
# in fly.toml (PORT=8080), set via env var in Hugging Face Spaces
# (PORT=7860), and set via env var in Render (PORT=10000). See README.
EXPOSE 8080

# Healthcheck uses the FastAPI /healthz route. The port is read at
# runtime via os.environ.get("PORT") so the same image works on every
# platform. JSON-form CMD does NOT expand shell variables, hence the
# inline Python.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, httpx, sys; sys.exit(0 if httpx.get(f'http://localhost:{os.environ.get(\"PORT\", \"8080\")}/healthz', timeout=4).status_code==200 else 1)"]

# Start uvicorn. Shell form so $PORT expands at runtime. The
# --workers 1 keeps the 256 MB machine happy (slowapi in-memory state
# diverges across workers) and matches the Fly.io http_service
# concurrency setting in fly.toml. We force the asyncio loop (not
# uvloop) to drop the 13 MB uvloop dependency — the perf delta for
# a single-worker recruitment demo is negligible.
CMD uvicorn mcp_server.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --loop asyncio
