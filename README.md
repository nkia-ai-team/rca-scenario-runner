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

### Declarative execution location

시나리오 YAML의 `execution.orchestrator`는 중앙 스크립트의 시작 위치를,
`execution.injection_points`는 장애가 실제로 만들어지는 위치를 결정한다.
필드가 없거나 기존 평면 `execution`이면 runner 컨테이너의 `local`
orchestrator로 정규화한다.

```yaml
execution:
  orchestrator:
    transport: local
    location: scenario-runner@109
    timeout_sec: 900
  injection_points:
    - id: north-south-surge
      kind: north_south
      transport: ssh
      location: tb-runner@192.168.122.206
      host: 192.168.122.206
      user: root
      identity_file: /root/.ssh/tb_key
      target: commerce NodePort
      entry_path: tb-runner → NodePort → gateway
      cleanup_location: tb-runner
      rationale: baseline과 동일한 사용자 진입 경로
      feasibility: ready
```

- `local`: runner가 저장소의 스크립트를 직접 실행
- `ssh`: 중앙 스크립트를 SSH 대상의 `bash -s`로 전달
- `docker`: 중앙 스크립트를 지정 컨테이너의 `bash -s`로 전달
- `kubectl`: 중앙 스크립트를 지정 namespace/resource의 `bash -s`로 전달
- `api`: `url` 또는 cleanup 시 `cleanup_url`에 시나리오 ID와 mode를 POST.
  인증 헤더는 `header_env: {Authorization: FAULT_API_TOKEN}`처럼 환경변수
  이름만 선언하며 실제 비밀값은 YAML이나 API 응답에 저장하지 않음

SSH는 비대화식 인증과 사전 등록된 host key를 요구한다. 원격 복사본을 실행하지
않으므로 실행한 코드와 저장소의 코드가 달라지는 문제를 피한다.

`POST /api/scenarios/{id}/dry-run`은 script hash, redacted orchestrator 명령,
실제 injection point와 cleanup 위치를 반환한다. subprocess·SSH·Docker·kubectl·API
호출과 로그/state 파일 생성을 하지 않는다.

## Prerequisites (109서버)

- Docker 24+ 및 docker compose v2 플러그인
- `/root/tb-kubeconfig`로 현재 kubeadm 4-node testbed API 접근 가능
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
| `KUBECONFIG_HOST_PATH` | `/root/tb-kubeconfig` | 컨테이너의 동일 경로로 read-only 마운트되는 정본 kubeconfig 파일 |
| `SCENARIO_MANIFEST_HOST_PATH` | `../testbed-services/scripts/scenarios/manifests` | 64개 plan-only manifest 경로 |
| `SCENARIO_CONTRACT_HOST_PATH` | `../testbed-services/scripts/scenarios` | digest 검증 dispatcher와 profile-control API의 read-only 신뢰 root |
| `CAPTURE_SCRIPT_HOST_PATH` | `../testbed-services/scripts/capture-eval-case.sh` | 데이터 저장소 캡처 스크립트의 고정 mount |
| `STATE_HOST_PATH` | `./state` | global lease·fencing·DIRTY coordinator 영속 경로 |
| `RUNS_HOST_PATH` | `./runs` | controller plan/state/timeline/cleanup/recovery/result와 model checkpoint 영속 경로 |
| `PROFILE_STATE_HOST_PATH` | `./profile-state` | 원본값 복구용 profile snapshot 영속 경로 |
| `EVAL_CASES_HOST_PATH` | `./eval-cases` | 완료된 calibration/evaluation capture 경로 |
| `LOGS_HOST_PATH` | `./logs` | 실행 로그 영구 보존 경로 |
| `SSH_KNOWN_HOSTS_HOST_PATH` | `/root/.ssh/known_hosts` | strict host-key 검증용 known_hosts |
| `COMMERCE_DB_PASSWORD` | 없음(필수) | controller의 read-only PostgreSQL 관측 자격증명 |
| `PG_PASSWORD` | 없음(필수) | 평가 케이스 PostgreSQL dump 자격증명 |
| `CH_PASSWORD` | 없음(필수) | 평가 케이스 ClickHouse export 자격증명 |

Adaptive controller는 `injection.profile_id`, `injection.catalog_slug`와 완전한
controller 계약이 있는 시나리오에만 활성화된다. 프로필 변경은 mounted
`profile-control.py`가 현재 plan digest·fencing token·idempotency key를 모두
검증할 때만 허용되며, API가 없으면 부하를 넣기 전에 fail-closed 한다.
64개 외부 manifest 중 완전한 controller runtime과 binding을 가진 ready 항목만
`run` API로 진입하며, 나머지는 plan-only 상태로 lease 획득 전 거부된다.
baseline·관측 값은 코드에 고정된 read-only LiveProbeSet이 승인된 check/query
ID만 실행한다.

Runner watchdog은 lease/heartbeat 만료 시 original run의 plan binding과 fencing
token으로 cleanup을 한 번 claim한다. 모든 profile recovery가 성공하면 lease를
해제하고, plan drift·cleanup·recovery 실패는 DIRTY로 남겨 신규 실행을 차단한다.
DIRTY external run은 동일 시나리오의 cleanup API로만 재시도할 수 있다.

정상 종료(CLEAN)만 capture job을 만든다. 캡처 범위는 정확히
`[t1-2시간, t2+45분]`이고, worker는 `t2+45분` 이후 global v1 `model.json`을
먼저 controller run 디렉터리에 checkpoint한 뒤 그 파일을 `MODEL_SOURCE`로
고정해 저장소 dump를 실행한다. `golden.anomaly.json`은 생성하지 않는다.

## API

| Method | Path | 설명 |
|--------|------|------|
| GET | `/healthz` | 헬스체크 |
| GET | `/api/scenarios` | 시나리오 목록 |
| GET | `/api/scenarios/{id}` | 단일 시나리오 |
| POST | `/api/scenarios/{id}/run` | 시나리오 실행 (비동기, 즉시 반환) |
| POST | `/api/scenarios/{id}/cleanup` | Cleanup 실행 (멱등) |
| POST | `/api/scenarios/{id}/dry-run` | 무접속 실행 계획·위치·script hash 검증 |
| GET | `/api/scenarios/{id}/status` | 현재 실행 상태 + log_tail (200줄) |
| GET | `/api/scenarios/{id}/logs?run_id=X` | 전체 로그 파일 (text/plain) |
| GET | `/api/history` | 최근 실행 이력 (20건) |
| GET | `/api/live-queue/readiness` | 11개 live 큐의 자격증명·mount·DIRTY 준비 상태 |
| GET | `/api/live-queue` | 영속 큐 진행률, 현재 run, clean-window deadline, 중단 사유 |
| POST | `/api/live-queue/start` | 고정 순서 11개 큐 신규 시작 |
| POST | `/api/live-queue/resume` | 원인 조치 후 중단된 동일 시나리오부터 재실행 |

Live 큐 순서는 기존 검증 8개인
`F01-R → F01-H → F03-G → F06-R → F07-H → F08-H → F09-P → F11-G` 뒤에
새로 승격한 `F01-G → F05-G → F11-R`를 이어 붙인다. 따라서 실행 중인 큐의
prefix는 바뀌지 않고 새 큐부터 11개 순서를 사용한다. 각 항목은 controller 성공,
CLEAN recovery, capture 완료를 증명한 뒤
`t2+2h`까지 기다린다. 첫 항목의 evidence/capture gate 전에 두 번째 항목은
시작하지 않는다. DIRTY, evidence 실패, capture 실패는 큐를 `paused`로 남긴다.

## Operations

```bash
# 로그 보기
docker logs -f scenario-runner

# 컨테이너 상태
docker ps --filter name=scenario-runner

# 시작 전 readiness (ready=true만 허용)
curl -fsS http://localhost:8090/api/live-queue/readiness | jq .

# 11개 순차 큐 시작 / 모니터링
curl -fsS -X POST http://localhost:8090/api/live-queue/start | jq .
watch -n 5 'curl -fsS http://localhost:8090/api/live-queue | jq .'

# paused 원인과 DIRTY를 해소한 뒤 동일 시나리오부터 재시도
curl -fsS -X POST http://localhost:8090/api/live-queue/resume | jq .

# 정지
docker compose down

# 컨테이너 내부로 진입 (디버깅)
docker exec -it scenario-runner bash
```

## License

Internal use only — NKIA.
