#!/usr/bin/env bash
# =============================================================================
# rca-scenario-runner: build + deploy (109서버 내부에서 실행)
# =============================================================================
# 하는 일
#   1) 사전 조건 확인 (docker, docker compose, kubectl 접근, 마운트 경로)
#   2) ARM64 네이티브로 Docker 이미지 빌드
#   3) 기존 컨테이너 정지/제거 후 재기동
#   4) 헬스체크 + 시나리오 API 응답 확인
#
# 사용법
#   cd ~/rca-scenario-runner
#   git fetch origin && git checkout feature/nkiaai-498 && git pull
#   ./build-and-deploy.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- 색상 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

PORT="${PORT:-8090}"
SCRIPTS_HOST_PATH="${SCRIPTS_HOST_PATH:-/home/nkia/scenarios}"
KUBECONFIG_HOST_PATH="${KUBECONFIG_HOST_PATH:-/home/nkia/.kube}"

# compose v1 vs v2 감지
if docker compose version &>/dev/null; then
    COMPOSE=(docker compose)
elif command -v docker-compose &>/dev/null; then
    COMPOSE=(docker-compose)
else
    log_error "docker compose 를 찾을 수 없습니다"
    exit 1
fi

# ---------------------------------------------------------------------------
# Phase 1: 사전 조건
# ---------------------------------------------------------------------------
phase_prereqs() {
    log_info "=== Phase 1: 사전 조건 확인 ==="

    if ! command -v docker &>/dev/null; then
        log_error "docker 미설치"; exit 1
    fi
    log_ok "docker 확인: $(docker --version)"
    log_ok "compose 확인: $(${COMPOSE[@]} version --short 2>/dev/null || echo unknown)"

    local arch
    arch="$(dpkg --print-architecture 2>/dev/null || uname -m)"
    log_ok "아키텍처: $arch"
    if [[ "$arch" != "arm64" && "$arch" != "aarch64" ]]; then
        log_warn "109서버는 ARM64 여야 합니다 (현재: $arch). 다른 호스트라면 빌드/실행은 되지만 운영 타겟과 아키텍처가 다릅니다."
    fi

    if [[ ! -d "$SCRIPTS_HOST_PATH" ]]; then
        log_error "시나리오 스크립트 경로 누락: $SCRIPTS_HOST_PATH"
        log_error "NKIAAI-480 의 스크립트를 109 호스트의 이 경로에 배치해야 합니다."
        exit 1
    fi
    local script_count
    script_count="$(find "$SCRIPTS_HOST_PATH" -name 'scenario-*.sh' -maxdepth 1 | wc -l)"
    log_ok "시나리오 스크립트: $script_count 개 (${SCRIPTS_HOST_PATH})"

    if [[ ! -f "${KUBECONFIG_HOST_PATH}/config" ]]; then
        log_error "kubeconfig 누락: ${KUBECONFIG_HOST_PATH}/config"
        exit 1
    fi
    log_ok "kubeconfig 확인: ${KUBECONFIG_HOST_PATH}/config"

    if command -v kubectl &>/dev/null; then
        if KUBECONFIG="${KUBECONFIG_HOST_PATH}/config" kubectl --request-timeout=3s -n rca-testbed get pods &>/dev/null; then
            log_ok "K3s(rca-testbed) 접근 정상"
        else
            log_warn "호스트 kubectl 에서 rca-testbed 조회 실패 — 컨테이너 내부에서 재확인됩니다."
        fi
    fi

    if ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
        log_warn "포트 ${PORT} 이미 사용 중일 수 있음. 기존 scenario-runner 컨테이너면 재배포 과정에서 정리됨."
    fi
}

# ---------------------------------------------------------------------------
# Phase 2: 빌드
# ---------------------------------------------------------------------------
phase_build() {
    log_info "=== Phase 2: Docker 이미지 빌드 ==="
    # BuildKit 활성화 (기본적으로 compose v2 는 buildkit 사용)
    DOCKER_BUILDKIT=1 "${COMPOSE[@]}" build scenario-runner
    log_ok "이미지 빌드 완료: scenario-runner:latest"
}

# ---------------------------------------------------------------------------
# Phase 3: 재기동
# ---------------------------------------------------------------------------
phase_up() {
    log_info "=== Phase 3: 컨테이너 재기동 ==="
    "${COMPOSE[@]}" down --remove-orphans 2>/dev/null || true
    "${COMPOSE[@]}" up -d
    log_ok "컨테이너 기동 요청 완료"
}

# ---------------------------------------------------------------------------
# Phase 4: 헬스체크
# ---------------------------------------------------------------------------
phase_health() {
    log_info "=== Phase 4: 헬스체크 ==="

    local max_wait=30
    local start
    start=$(date +%s)
    while true; do
        if curl -sf --max-time 2 "http://localhost:${PORT}/healthz" >/dev/null; then
            log_ok "/healthz 200 OK"
            break
        fi
        if (( $(date +%s) - start > max_wait )); then
            log_error "헬스체크 시간 초과 (${max_wait}초). docker logs scenario-runner 확인 필요."
            docker ps --filter name=scenario-runner
            docker logs --tail 40 scenario-runner || true
            exit 1
        fi
        sleep 1
    done

    local scenarios_json
    scenarios_json="$(curl -s --max-time 3 "http://localhost:${PORT}/api/scenarios" || true)"
    local count
    count="$(echo "$scenarios_json" | grep -oE '"id":"[0-9]+"' | wc -l)"
    if [[ "$count" -ge 4 ]]; then
        log_ok "/api/scenarios 응답 OK (시나리오 $count 개)"
    else
        log_warn "/api/scenarios 응답이 기대와 다름. 원문: $scenarios_json"
    fi
}

# ---------------------------------------------------------------------------
# Phase 5: 상태 요약
# ---------------------------------------------------------------------------
phase_summary() {
    log_info "=== Phase 5: 배포 결과 ==="
    echo
    docker ps --filter name=scenario-runner --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    echo
    log_ok "접속 URL: http://$(hostname -I | awk '{print $1}'):${PORT}/"
    echo
    log_info "로그 확인: docker logs -f scenario-runner"
    log_info "정지:     ${COMPOSE[*]} down"
}

# ---------------------------------------------------------------------------
main() {
    local t0
    t0=$(date +%s)
    phase_prereqs
    echo
    phase_build
    echo
    phase_up
    echo
    phase_health
    echo
    phase_summary
    echo
    log_ok "총 소요: $(( $(date +%s) - t0 ))초"
}

main "$@"
