from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["idle", "running", "succeeded", "failed", "cleanup_running"]


class Scenario(BaseModel):
    id: str                       # composite "<domain>:<short_id>" — globally unique
    short_id: str                 # within-domain id, e.g. "01"
    domain: str                   # folder slug, e.g. "plopvape-shop"
    domain_label: str             # human-readable, e.g. "Plopvape Shop"
    name: str
    description: str
    cause: str
    propagation: str
    expected_alarms: list[str]
    estimated_duration_sec: int
    script_filename: str
    warnings: list[str] = Field(default_factory=list)
    # --- RCA ground-truth (optional; populated from service-spec.yaml) ---
    # 1~5. 5 = 결정적 (트레이스/메트릭 만으로 RCA 가 근본 원인 짚어야 함),
    # 1 = 추정만 가능 (수집 데이터 한계로 RCA 보고서 가 가설 형태여도 합격).
    difficulty: Optional[int] = None
    # RCA agent 가 "이 근본 원인" 이라고 보고해야 하는 기대 결론.
    # 반드시 수집 가능한 시그널 (트레이스 span / 메트릭 / 로그) 기반으로만 기술.
    # 보이지 않는 것 (코드 어노테이션, 런타임 설정값) 채점 기준 금지.
    expected_rca_root_cause: Optional[str] = None
    # --- 구조화 스키마 (testbed-services spec-scenario-design §4 / load §3) ---
    # 레거시 항목은 root_cause/propagation 이 문자열이라 위의 cause/propagation 으로
    # 그대로 들어오고, 구조화 항목은 로더가 표시용 문자열로 정규화한 뒤 원본을
    # 아래 필드에 보존한다. (둘 다 없으면 None)
    cause_domain: Optional[str] = None                    # APM|DPM|SMS|NMS|KCM|WPM
    expected_depth: Optional[str] = None                  # entity|dimension
    root_cause_detail: Optional[dict] = None              # target_kind/mechanism 등 원본
    propagation_steps: Optional[list[str]] = None         # 단계 리스트 원본
    injection: Optional[dict] = None                      # script/parameters 원본
    expected_anomalies: Optional[list[dict]] = None       # 이상감지 기대값 (load §3)
    signals: Optional[dict] = None                        # must_support/must_rule_out


class Domain(BaseModel):
    slug: str
    label: str
    scenario_count: int


class ActiveRun(BaseModel):
    """Returned by /api/active. Tells everyone if the runner is busy and on what."""
    is_active: bool
    scenario_id: Optional[str] = None
    run_id: Optional[str] = None
    mode: Optional[Literal["run", "cleanup"]] = None
    started_at: Optional[datetime] = None


class RunInfo(BaseModel):
    run_id: str
    scenario_id: str
    mode: Literal["run", "cleanup"]
    status: Status
    started_at: datetime
    finished_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    log_tail: list[str] = Field(default_factory=list)


class HistoryEntry(BaseModel):
    run_id: str
    scenario_id: str
    mode: Literal["run", "cleanup"]
    status: Status
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_sec: Optional[float] = None
    exit_code: Optional[int] = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str = "0.1.0"


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
