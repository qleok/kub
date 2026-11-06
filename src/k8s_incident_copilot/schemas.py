"""Request and response models for the diagnosis API."""

from typing import Annotated, Literal, Self, TypeAlias
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StringConstraints, model_validator

FailureType: TypeAlias = Literal["CrashLoopBackOff", "ImagePullBackOff", "OOMKilled"]
Severity: TypeAlias = Literal["medium", "high", "unknown"]
Reason: TypeAlias = Annotated[str, StringConstraints(max_length=128)]
LogLine: TypeAlias = Annotated[str, StringConstraints(max_length=4096)]


class PodEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    reason: Reason | None = None
    message: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def require_event_evidence(self) -> Self:
        if not (self.reason or self.message):
            raise ValueError("An event must have a nonblank reason or message.")
        return self


class IncidentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    pod_name: str | None = Field(default=None, min_length=1, max_length=253)
    namespace: str | None = Field(default=None, min_length=1, max_length=63)
    status: Reason | None = Field(
        default=None, description="Displayed Pod status, such as the kubectl STATUS column."
    )
    container_reason: Reason | None = Field(
        default=None,
        description="Current state or last termination reason of the affected container.",
    )
    events: list[PodEvent] = Field(default_factory=list, max_length=100)
    logs: list[LogLine] = Field(default_factory=list, max_length=200)
    description: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def require_incident_evidence(self) -> Self:
        if not (
            self.status
            or self.container_reason
            or self.events
            or any(self.logs)
            or self.description
        ):
            raise ValueError(
                "Provide at least one nonblank status, reason, event, log, or description."
            )
        return self


class Reference(BaseModel):
    title: str
    url: AnyHttpUrl


class DiagnosisReport(BaseModel):
    incident_id: UUID
    status: Literal["diagnosed", "insufficient_evidence"]
    failure_type: FailureType | None
    suspected_cause: str | None
    severity: Severity
    evidence: list[str]
    recommended_steps: list[str]
    references: list[Reference]
    diagnosis_method: Literal["rules"] = "rules"


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
