# syntax=docker/dockerfile:1.6

# ========== Stage 1: Frontend build ==========
# Use glibc image (node:20-slim) instead of alpine — the package-lock.json generated on
# a glibc host doesn't list the musl-specific native bindings (@emnapi/*, rollup musl),
# so `npm ci` on alpine fails with EUSAGE.
FROM node:20-slim AS frontend-builder

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ========== Stage 2: Backend runtime ==========
FROM python:3.12-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    KUBECTL_VERSION=v1.30.0 \
    SCRIPT_DIR=/app/scripts \
    LOG_DIR=/app/logs \
    STATIC_DIR=/app/static \
    PORT=8000

WORKDIR /app

# System deps + kubectl (arch-aware: arm64 on 109, amd64 on x86 hosts)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates bash \
 && arch="$(dpkg --print-architecture)" \
 && curl -fsSL -o /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
 && chmod 0755 /usr/local/bin/kubectl \
 && rm -rf /var/lib/apt/lists/*

# uv (single binary, portable). Pin by digest-friendly tag; matches uv >= 0.8 which
# understands our uv.lock schema.
COPY --from=ghcr.io/astral-sh/uv:0.8.0 /uv /usr/local/bin/uv

# Backend dependencies (cache friendly: lock+pyproject first)
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd backend && uv sync --frozen --no-dev

# Backend source
COPY backend/app ./backend/app

# Frontend build artifacts
COPY --from=frontend-builder /build/dist ${STATIC_DIR}

# Runtime dirs
RUN mkdir -p ${LOG_DIR} ${SCRIPT_DIR}

EXPOSE 8000
WORKDIR /app/backend

CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
