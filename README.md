# rca-scenario-runner

RCA 테스트베드 장애 시나리오를 브라우저에서 실행할 수 있는 내부용 웹 UI.
Linear 이슈: [NKIAAI-498](https://linear.app/nkia/issue/NKIAAI-498).

## What it does

- 109서버 K3s 의 `rca-testbed` 네임스페이스에 배포된 쇼핑몰 마이크로서비스를 대상으로 사전 정의된 4종 장애 시나리오를 웹 UI 로 실행
- 테스터가 SSH 없이 브라우저에서 버튼 클릭만으로 장애 주입 + 실시간 상태/로그 확인
- 시나리오 스크립트는 레포에 포함 (`scenarios/services/<service-name>/scripts/`). 배포 시 레포 내부 경로가 컨테이너로 마운트됨. 자세한 구조는 [scenarios/README.md](scenarios/README.md) 참조.

## Architecture

```
Browser ──HTTP──▶ 109서버:8090 ──▶ [Docker (host network)]
                                      └─ uvicorn (FastAPI)
                                         ├─ /api/*     → scenario runner API
                                         ├─ /healthz   → health
                                         └─ /*         → React static (Vite build)
                                       │
                                       │ asyncio subprocess
                                       ▼
                                     bash scenario-XX.sh
                                       │ kubectl exec
                                       ▼
                                     K3s rca-testbed ns
                                       (postgres, 5 shop services, nginx)
```

- Frontend: React + Vite + TypeScript + Tailwind
- Backend: FastAPI + asyncio subprocess
- Deploy: 단일 Docker 컨테이너 (host network) + docker-compose, 109서버 ARM64 네이티브
- 동시 실행: 한 번에 하나의 시나리오만 (asyncio.Lock 기반)

## Prerequisites (109서버)

- Docker 24+ 및 docker compose v2 플러그인
- `/home/nkia/.kube/config` 로 로컬 K3s API 접근 가능
- 호스트 포트 8090 free (변경 가능, `.env` 참조)
- 시나리오 스크립트는 **레포 내부**(`scenarios/services/<service-name>/scripts/`)에 포함되어 있어 별도 배치 불필요. 기본 서비스: `plopvape-shop`. 다른 서비스로 전환하려면 `.env` 의 `SCRIPTS_HOST_PATH` 오버라이드.

## Deploy (109서버)

```bash
# 최초
git clone https://github.com/nkia-ai-team/rca-scenario-runner.git ~/rca-scenario-runner
cd ~/rca-scenario-runner
git checkout feature/nkiaai-498   # 통합 후에는 develop/main

# 배포
./build-and-deploy.sh

# 확인
curl http://localhost:8090/healthz
curl http://localhost:8090/api/scenarios
# 브라우저: http://<target-host>:${PORT}/

# 업데이트 (새 변경 반영)
git pull
./build-and-deploy.sh
```

스크립트가 다음을 순차 수행:
1. 사전 조건 (docker, compose, 마운트 경로, kubeconfig) 검사
2. ARM64 네이티브 빌드
3. 기존 컨테이너 정지 후 재기동
4. `/healthz`, `/api/scenarios` 헬스체크
5. 접속 URL 출력

## Local Development (104서버 또는 본인 PC)

백엔드와 프론트엔드를 별도 프로세스로 돌립니다.

```bash
# 터미널 1: backend
cd backend
SCRIPT_DIR=../scenarios/services/plopvape-shop/scripts \
  LOG_DIR=/tmp/scenario-runner-logs \
  uv sync
SCRIPT_DIR=../scenarios/services/plopvape-shop/scripts \
  LOG_DIR=/tmp/scenario-runner-logs \
  uv run uvicorn app.main:app --reload --port 8000

# 터미널 2: frontend (Vite proxy 가 /api → :8000 으로 전달)
cd frontend
npm install
npm run dev
# 브라우저: http://localhost:5173/
```

로컬에선 kubectl / K3s 가 없어 `POST /run` 시 bash 가 실패합니다. **UI 흐름 검증 전용** 이며 실제 장애 주입은 109 배포 후에만 가능합니다.

### Tests

```bash
# Backend (9 integration tests via httpx ASGI transport)
cd backend && uv run pytest -v

# Frontend build (TypeScript strict + Vite)
cd frontend && npm run build
```

## Configuration

환경변수는 `.env.example` 참조. `.env` 파일을 만들면 docker-compose 가 자동 로드.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `PORT` | `8090` | 호스트 바인드 포트 |
| `SCRIPTS_HOST_PATH` | `./scenarios/services/plopvape-shop/scripts` | 컨테이너 `/app/scripts` 로 마운트되는 경로. 레포 내부가 기본값 — 다른 서비스로 바꾸려면 `./scenarios/services/<name>/scripts` 로 변경 |
| `KUBECONFIG_HOST_PATH` | `/home/nkia/.kube` | 컨테이너 `/root/.kube` 로 마운트되는 호스트 경로 |
| `LOGS_HOST_PATH` | `./logs` | 실행 로그 영구 보존 경로 |

## API

| Method | Path | 설명 |
|--------|------|------|
| GET | `/healthz` | 헬스체크 |
| GET | `/api/scenarios` | 시나리오 목록 |
| GET | `/api/scenarios/{id}` | 단일 시나리오 |
| POST | `/api/scenarios/{id}/run` | 시나리오 실행 (비동기, 즉시 반환) |
| POST | `/api/scenarios/{id}/cleanup` | Cleanup 실행 (멱등) |
| GET | `/api/scenarios/{id}/status` | 현재 실행 상태 + log_tail (200줄) |
| GET | `/api/scenarios/{id}/logs?run_id=X` | 전체 로그 파일 (text/plain) |
| GET | `/api/history` | 최근 실행 이력 (20건) |

## Operations

```bash
# 로그 보기
docker logs -f scenario-runner

# 컨테이너 상태
docker ps --filter name=scenario-runner

# 정지
docker compose down

# 컨테이너 내부로 진입 (디버깅)
docker exec -it scenario-runner bash
```

## License

Internal use only — NKIA.
