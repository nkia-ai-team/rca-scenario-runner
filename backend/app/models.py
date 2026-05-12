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
