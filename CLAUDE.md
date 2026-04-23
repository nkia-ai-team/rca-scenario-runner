# CLAUDE.md — rca-scenario-runner

RCA 테스트베드 장애 시나리오를 브라우저에서 실행/관측하는 내부 QA 툴. Linear 이슈: NKIAAI-498.

## Architecture

```
Browser ──HTTP──▶ 109서버:8091 (Docker, network_mode: host)
                   └─ uvicorn (FastAPI + React static)
                        ├─ /api/*    → FastAPI routes
                        ├─ /healthz  → health
                        └─ /*        → React SPA (Vite dist)
                             │
                             ▼ subprocess("bash", "scenario-XX.sh")
                   bash script ──▶ kubectl exec / curl / docker cmd
                                    (호스트 K3s `rca-testbed` ns 조작)
```

- FastAPI 가 bash 스크립트를 `asyncio.create_subprocess_exec` 로 실행, stdout 파이프를 한 줄씩 읽어 **메모리 deque(200줄) + 디스크 로그파일** 에 이중 기록
- 브라우저는 `GET /api/scenarios/{id}/status` **2초 polling** 으로 상태/로그 읽음
- **한 번에 1개 시나리오만 실행** (asyncio.Lock). 동시 실행 요청 → HTTP 409
- 시나리오 스크립트는 `trap cleanup EXIT` 로 자동 원상복구. SIGKILL 은 못 잡음 (Cleanup 버튼 안전망)

## Tech Stack

- Backend: Python 3.12, FastAPI, uvicorn, uv
- Frontend: React 18 + Vite + TypeScript + Tailwind v3 (Claude Design 프로토타입 기반)
- Deploy: Docker 단일 컨테이너, `network_mode: host`, multi-stage Dockerfile (node:20-slim → python:3.12-slim)

## Dev Commands

**Backend**
```bash
cd backend
uv sync --extra dev
uv run pytest -v                                   # 9 tests 전부 PASS 해야 함
SCRIPT_DIR=/tmp/fake LOG_DIR=/tmp/sr-logs \
  uv run uvicorn app.main:app --port 8000          # 로컬 실행 (104서버엔 K3s 없어 실제 시나리오 실행은 실패)
```

**Frontend**
```bash
cd frontend
npm ci
npm run dev                                         # :5173, proxy /api → :8000
npm run build                                       # dist/
```

## Deploy (109서버)

```bash
ssh nkia@192.168.200.109
git clone https://github.com/nkia-ai-team/rca-scenario-runner.git ~/rca-scenario-runner    # 최초 1회
cd ~/rca-scenario-runner
git pull                                            # 이후엔 pull 만
./build-and-deploy.sh                               # 빌드 + compose up + healthz 확인
```

- 접속: `http://192.168.200.109:8091/`
- 정지: `docker compose down`
- uvicorn 로그: `docker logs -f scenario-runner`
- 시나리오 bash stdout: `~/rca-scenario-runner/logs/{run_id}.log` (호스트 볼륨 마운트로 보존)

## Environment Variables

`.env` 파일로 관리 (compose 자동 로드 + `build-and-deploy.sh` 도 상단에서 source).

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `PORT` | `8090` | uvicorn bind port. **109서버는 반드시 8091** (pg-mock 충돌) |
| `SCRIPT_DIR` | `/app/scripts` | 컨테이너 안 시나리오 디렉토리 경로 |
| `LOG_DIR` | `/app/logs` | 실행 로그 저장 |
| `STATIC_DIR` | `/app/static` | React dist 서빙 경로 |
| `KUBECONFIG` | `/home/nkia/.kube/config` | **스크립트가 하드코딩** 한 경로. 컨테이너 안 마운트 경로를 이것에 맞춤 |
| `SCRIPTS_HOST_PATH` | `./scenarios/services/plopvape-shop/scripts` | 레포 내부 시나리오 경로(기본). 다른 서비스로 전환 시 `./scenarios/services/<name>/scripts` |
| `LOGS_HOST_PATH` | `./logs` | 호스트 로그 보존 경로 |
| `KUBECONFIG_HOST_PATH` | `/home/nkia/.kube` | 호스트 kubeconfig 디렉토리 |

## Volume Mounts

`docker-compose.yml` 에 3개, 각각 이유 있음:

1. **`${KUBECONFIG_HOST_PATH}:/home/nkia/.kube:ro`** — 컨테이너 안에서도 호스트와 **동일 경로** 로 노출. 이유: NKIAAI-480 bash 스크립트 1번째 줄 `export KUBECONFIG=/home/nkia/.kube/config` 하드코딩. 스크립트 수정 회피 목적.
2. **`${SCRIPTS_HOST_PATH}:/app/scripts:ro`** — 시나리오 스크립트는 **레포에 포함**(`scenarios/services/<name>/scripts/`). 기본 서비스 `plopvape-shop`. 다른 서비스 전환 시 `.env` 의 `SCRIPTS_HOST_PATH` 오버라이드. (구 디자인: `/home/nkia/scenarios/` scp 방식 — 2026-04 이관으로 레포 내부 경로가 source of truth.)
3. **`/var/run/docker.sock:/var/run/docker.sock:rw`** — **Docker-out-of-Docker**. scenario-02 가 호스트 `pg-mock` 컨테이너를 start/stop. 컨테이너가 호스트 docker root 권한 획득이라 보안상 내부 신뢰 환경 한정.

## Known Gotchas / Conventions

1. **포트 8091 강제** — 109서버 호스트에서 `pg-mock` 이 8090 점유 중. `.env` 에 `PORT=8091` 필수. 다른 환경에선 8090 써도 됨.

2. **Docker CLI ≥ 27.x 필수** — 호스트 daemon 29.x 의 minimum API 1.44 요구. 24.x CLI 면 `client too old` 에러. `Dockerfile` 에 `DOCKER_VERSION=27.3.1` pin.

3. **node:20-slim (NOT alpine)** — host glibc 에서 생성한 `package-lock.json` 엔 musl native binding 누락. alpine 은 `npm ci` 실패. slim(Debian glibc) 사용. lockfile 재생성 시 glibc 환경에서 할 것.

4. **`build-and-deploy.sh` 가 `.env` source 함** — 상단 `set -a; source .env; set +a`. 직접 실행 시 이 로드가 없으면 compose variable 과 shell 변수 불일치해 헬스체크 단계에서 잘못된 포트 확인함.

5. **FastAPI 가 React static 직접 서빙** — nginx 없음. `app.py` 마지막 라인에 `app.mount("/", StaticFiles(...))`. mount 순서 중요: 먼저 등록된 `/api/*` 라우트 → 마지막에 catchall `/` static. 순서 바꾸면 API 라우트가 static 에 가려짐.

6. **Clipboard API HTTP 한계** — 접속 URL 이 HTTP + 사설 IP 라 `navigator.clipboard` undefined. `frontend/src/lib/clipboard.ts` 에 `document.execCommand('copy')` 폴백 구현. 수정 시 폴백 체인 유지할 것.

7. **LogViewer 높이 동기화** — 왼쪽 시나리오 리스트 높이에 맞추기 위해 `ResizeObserver` 로 측정 → 오른쪽 section `height: min(leftHeight, calc(100vh-96px))`. `sticky top-[72px]` + `items-start` 조합 필수. `frontend/src/App.tsx` 의 `leftSectionRef` 블록 참조. 순수 CSS 로 sibling 높이 매칭 불가.

8. **진행률은 단순 추정치** — `elapsed / estimated_duration_sec`. 스크립트 내부 phase 모르므로. 종료 상태(succeeded/failed) 에서 강제 100% 스냅 (`frontend/src/components/ExecutionPanel.tsx`).

9. **SIGKILL 타임아웃 시 cleanup 미발동** — bash 의 `trap cleanup EXIT` 는 SIGTERM/정상 종료만. runner.py 타임아웃은 `proc.kill()` (SIGKILL) 이라 좀비 상태(lock 등) 남을 수 있음. **Cleanup 버튼은 수동 원상복구용 안전망**.

10. **Git push 인증** — 104서버 HOME 의 `~/.git-credentials` 에 PAT 있음. 새로운 환경(109 등)엔 별도 PAT 발급 + `credential.helper store` 설정 필요.

## Scenario Side Effects

각 시나리오의 cleanup 은 멱등이지만 **Polestar10 관측 자산에 부작용** 남김. UI 카드의 "주의사항" 섹션에 표시됨 (`backend/app/scenarios.py` `warnings` 필드).

- **시나리오 03** (PostgreSQL CPU throttle): cleanup 중 postgres pod 재기동 → KCM 의 postgres 개별 알람 설정 disable. KCM 콘솔에서 재활성화 필요.
- **시나리오 04** (Traffic Flood): cleanup 이 5개 서비스 rolling restart → WPM 에이전트 재등록, 기존 에이전트 disabled + 카운트 증가. disabled 에이전트 삭제 시 해당 에이전트 수집 WPM 데이터도 함께 삭제.

## Related Repos

- **lucida-rca-agent** (GitLab cims2, 제품 레포) — RCA 분석 엔진. NKIAAI-480 시나리오 스크립트는 2026-04 본 레포(`scenarios/services/plopvape-shop/scripts/`)로 이관되어 현재 rca-agent 에는 없음.
- **plopvape-shop** (GitHub, ARM64 빌드 패턴 참조) — 109서버 K3s `rca-testbed` ns 의 testbed-* pods 배포. scenario-runner 가 이 pods 를 `kubectl exec` 로 조작.
- **Polestar10** 관측 도메인: APM/WPM/DPM/NMS/KCM/SMS. 자세한 맵은 `lucida-rca-agent/docs/testbed/architecture.md` §3.2.
