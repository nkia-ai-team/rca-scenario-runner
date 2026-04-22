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
    DOCKER_VERSION=24.0.7 \
    SCRIPT_DIR=/app/scripts \
    LOG_DIR=/app/logs \
    STATIC_DIR=/app/static \
    PORT=8000

WORKDIR /app

# System deps + kubectl + docker CLI + iproute2 (ss).
# - kubectl: NKIAAI-480 스크립트가 testbed pods 제어
# - docker CLI (static binary): scenario-02 가 호스트의 pg-mock 컨테이너 start/stop/inspect
# - iproute2: `ss -tlnp` 로 포트 점유 확인 (scenario-02 black-hole 교체 타이밍)
# arch-aware: arm64 on 109, amd64 on x86 hosts
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates bash iproute2 \
 && arch="$(dpkg --print-architecture)" \
 && arch_uname="$(uname -m)" \
 && curl -fsSL -o /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
 && chmod 0755 /usr/local/bin/kubectl \
 && curl -fsSL "https://download.docker.com/linux/static/stable/${arch_uname}/docker-${DOCKER_VERSION}.tgz" \
    | tar -xz -C /tmp \
 && mv /tmp/docker/docker /usr/local/bin/docker \
 && rm -rf /tmp/docker \
 && chmod 0755 /usr/local/bin/docker \
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
