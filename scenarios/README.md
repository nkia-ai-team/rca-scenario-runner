# scenarios

서비스별 장애 시나리오 스크립트 저장소. `rca-scenario-runner` 가 이 디렉토리를 표준 위치로 찾습니다.

## 구조

```
scenarios/
├── services/
│   └── <service-name>/
│       ├── service-spec.yaml   # 서비스 메타 + 시나리오 목록
│       └── scripts/
│           ├── scenario-01-*.sh
│           ├── scenario-02-*.sh
│           └── ...
└── README.md
```

## 현재 서비스

- 이 디렉터리의 `service-spec.yaml` 은 manifest 가 없을 때만 쓰는 옛 카탈로그다. 66 러너가 화면에 보이는 시나리오는 testbed-services 의 manifest(`SCENARIO_MANIFEST_HOST_PATH`)에서 온다.
- **plopvape-shop** — 1세대. 109서버 K3s `rca-testbed` 네임스페이스. 쇼핑몰 마이크로서비스 대상 4종 시나리오. 66 으로 옮기지 않았다.

## 새 서비스 추가 절차

1. `services/<new-service>/` 디렉토리 생성
2. `service-spec.yaml` 작성 (기존 예시 참조)
3. `scripts/` 에 실행 가능한 bash 스크립트 배치 (`chmod +x`)
4. 각 스크립트는 `trap cleanup EXIT` 로 원상복구 보장
5. 스크립트는 `./scenario-XX.sh` / `./scenario-XX.sh cleanup` 두 모드 지원

## 배포

스크립트는 **레포에 포함**. 66 tb-ops 의 `/root/rca-scenario-runner/scenarios/services` 는 git 이 아닌 사본이라, 바뀐 커밋을 그 사본에 덮어 쓴다(testbed-services `docs/runbook-testbed-move-109-to-66.md` 4단계).

- `docker-compose.yml` 의 기본 마운트: `./scenarios/services/plopvape-shop/scripts:/app/scripts:ro`
- 다른 서비스로 바꾸려면 `.env` 에서 오버라이드:
  ```bash
  SCRIPTS_HOST_PATH=./scenarios/services/<다른-서비스>/scripts
  ```
- 레포 밖의 경로를 써야 할 경우에만 절대경로 지정 (과거 `/home/nkia/scenarios/` 방식은 폐지)

## Runner 연동 (미래 방향)

현재 시나리오 메타데이터(title/warnings)는 `backend/app/scenarios.py` 에 하드코딩됨. 향후 `service-spec.yaml` 을 source of truth로 전환 예정 (runner가 파일 스캔 → YAML 파싱 → UI 렌더).
