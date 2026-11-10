"""Request and response models for the diagnosis API."""

from typing import Annotated, Literal, Self, TypeAlias
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StringConstraints, model_validator

FailureType: TypeAlias = Literal["CrashLoopBackOff", "ImagePullBackOff", "OOMKilled"]
Severity: TypeAlias = Literal["medium", "high", "unknown"]
Reason: TypeAlias = Annotated[str, StringConstraints(max_length=128)]
LogLine: TypeAlias = Annotated[str, StringConstraints(max_length=4096)]
_DNS_LABEL = r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
DnsLabel: TypeAlias = Annotated[str, StringConstraints(max_length=63, pattern=f"^{_DNS_LABEL}$")]
DnsSubdomain: TypeAlias = Annotated[
    str, StringConstraints(max_length=253, pattern=rf"^{_DNS_LABEL}(?:\.{_DNS_LABEL})*$")
]


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


class RetrievedReference(Reference):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    excerpt: str
    recommended_step: str


class DiagnosisReport(BaseModel):
    incident_id: UUID
    status: Literal["diagnosed", "insufficient_evidence"]
    failure_type: FailureType | None
    suspected_cause: str | None
    severity: Severity
    evidence: list[str]
    recommended_steps: list[str]
    references: list[RetrievedReference | Reference]
    diagnosis_method: Literal["rules", "rules+rag"] = "rules"


AgentToolStatus: TypeAlias = Literal["ok", "skipped", "unavailable", "timed_out"]
ModelStatus: TypeAlias = Literal[
    "not_configured", "skipped", "used", "unavailable", "timed_out", "invalid_response"
]


class AgentToolRun(BaseModel):
    name: Literal["classify", "retrieve"]
    status: AgentToolStatus


class AgentDetails(BaseModel):
    tools: list[AgentToolRun]
    model_status: ModelStatus
    prioritized_steps: list[str]


class AgentDiagnosisReport(DiagnosisReport):
    agent: AgentDetails


class PodLookup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    namespace: DnsLabel
    pod_name: DnsSubdomain
    container_name: DnsLabel | None = None


class KubernetesDetails(BaseModel):
    namespace: str
    pod_name: str
    container_name: str
    events_status: Literal["ok", "unavailable"]
    logs_status: Literal["ok", "unavailable"]


class PodDiagnosisReport(AgentDiagnosisReport):
    kubernetes: KubernetesDetails


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
