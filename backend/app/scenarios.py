from app.models import Scenario

SCENARIOS: dict[str, Scenario] = {
    "01": Scenario(
        id="01",
        name="Inventory Row Lock Contention",
        description="inventory 테이블 long-held SELECT FOR UPDATE lock 로 동시 주문 contention 재현",
        cause="장시간 SELECT FOR UPDATE lock 점유 + 동시 주문 폭주",
        propagation="inventory lock wait → product timeout → order 5xx → Nginx 502",
        expected_alarms=[
            "DPM lock wait",
            "APM inventory response time 증가",
            "SMS postgres process CPU%",
        ],
        estimated_duration_sec=120,
        script_filename="scenario-01-inventory-lock.sh",
    ),
    "02": Scenario(
        id="02",
        name="External PG API Timeout",
        description="pg-mock 중단 + TCP black-hole 로 payment read-timeout 유발 → order 캐스케이드 장애",
        cause="외부 PG API 무응답 (TCP 연결은 되지만 HTTP 응답 없음, 10초 대기)",
        propagation="payment read-timeout → order thread starvation → 502 응답",
        expected_alarms=[
            "APM payment 평균응답시간 초과 (3~5s, 외부 PG 대기)",
            "APM order 평균응답시간 초과 (10s+, payment 대기 누적)",
            "APM order 서비스 에러율 급증 (최대 100%)",
            "DPM 트랜잭션 시간 초과 (≥5s)",
            "DPM DB Lock 수 급증 (≥40 Lock)",
        ],
        estimated_duration_sec=120,
        script_filename="scenario-02-payment-timeout.sh",
    ),
    "03": Scenario(
        id="03",
        name="PostgreSQL Pod CPU Throttle",
        description="postgres pod CPU limit 도달로 전 서비스 쿼리 지연",
        cause="K8s CPU limit (500m) 도달 → CPU throttling",
        propagation="DB slow query → 전 서비스 RT 증가 (GET 180x, POST 17x)",
        expected_alarms=[
            "KCM postgres Pod CPU throttling",
            "APM 전 서비스 response time 증가",
        ],
        estimated_duration_sec=120,
        script_filename="scenario-03-db-cpu-throttle.sh",
        warnings=[
            "cleanup 중 postgres pod 가 재기동되면서 KCM 의 postgres 개별 알람 설정이 disable 될 수 있음",
            "재기동 후 KCM 콘솔에서 개별 알람 룰을 다시 활성화해야 함",
        ],
    ),
    "04": Scenario(
        id="04",
        name="Black Friday Traffic Flood",
        description="동시 주문 요청 폭주로 order-service thread pool 포화 + DB 커넥션/락 경합",
        cause="4단계 점진적 트래픽 폭주 (5 → 50 → 200 → 500 concurrent)",
        propagation="order thread pool 포화 → 하위 서비스 cascading 5xx → DB 세션/Lock 급증",
        expected_alarms=[
            "APM 평균응답시간 초과 (order/inventory/payment/product)",
            "DPM DB 연결 수 폭증 (≥55 세션)",
            "DPM DB Lock 수 급증 (≥40 Lock)",
            "DPM 트랜잭션 시간 초과",
        ],
        estimated_duration_sec=240,
        script_filename="scenario-04-traffic-flood.sh",
        warnings=[
            "cleanup 시 애플리케이션 서비스가 rolling restart 되어 WPM 에이전트가 재등록됨",
            "기존 에이전트는 disabled 상태로 남고, 에이전트 총 개수가 실행할 때마다 증가",
            "disabled 에이전트를 삭제하면 해당 에이전트가 수집한 WPM 데이터 전부 함께 삭제됨 — 주의해서 관리",
        ],
    ),
}


def get_scenario(scenario_id: str) -> Scenario | None:
    return SCENARIOS.get(scenario_id)


def list_scenarios() -> list[Scenario]:
    return list(SCENARIOS.values())
