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
        description="payment-service 에서 외부 PG API 타임아웃 + order 재시도 폭주 유발",
        cause="payment-service 의 외부 API 호출이 10~20초 지연",
        propagation="payment timeout → order 재시도 storm → thread pool 고갈",
        expected_alarms=[
            "APM payment ApiTime 증가",
            "APM order ActiveService / TotThreadCount 증가",
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
    ),
}


def get_scenario(scenario_id: str) -> Scenario | None:
    return SCENARIOS.get(scenario_id)


def list_scenarios() -> list[Scenario]:
    return list(SCENARIOS.values())
