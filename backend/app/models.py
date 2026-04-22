from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["idle", "running", "succeeded", "failed", "cleanup_running"]


class Scenario(BaseModel):
    id: str
    name: str
    description: str
    cause: str
    propagation: str
    expected_alarms: list[str]
    estimated_duration_sec: int
    script_filename: str


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
