# rca-scenario-runner

RCA 테스트베드 장애 시나리오를 브라우저에서 실행할 수 있는 내부용 웹 UI.
Linear 이슈: NKIAAI-498.

## What it does

- 109서버 K3s 상의 `rca-testbed` 네임스페이스에 정의된 4종 장애 시나리오 스크립트를 웹 UI로 실행
- 테스터가 SSH 없이 브라우저에서 버튼 클릭만으로 장애 주입 + 실시간 상태/로그 확인
- 시나리오 스크립트는 선행 이슈(NKIAAI-480)의 결과물을 볼륨 마운트로 참조

## Architecture

```
Browser  ──HTTP──▶  109서버:8090  ──Docker──▶  [Nginx → FastAPI ─subprocess→ bash]
                                                                 │
                                                                 ▼
                                              K3s(rca-testbed): testbed-postgres-0,
                                              5 shop services, nginx, etc.
```

- Frontend: React + Vite + TypeScript + Tailwind
- Backend: FastAPI + asyncio subprocess
- Deploy: Docker + docker-compose on 109서버 host (ARM64 네이티브)

## Status

Early development. See detailed plan: `.omc/plans/nkiaai-498-scenario-runner-ui.md` (in sjbang's workspace).

## License

Internal use only — NKIA.
