"""HTTP entry point for the Phase 1 diagnosis service."""

from uuid import uuid4

from fastapi import FastAPI

from .diagnosis import diagnose
from .schemas import DiagnosisReport, HealthResponse, IncidentInput

app = FastAPI(
    title="K8s Incident Copilot",
    description="Local, rule-based Kubernetes incident diagnosis for a single Pod snapshot.",
    version="0.1.0",
)


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse()


@app.post("/api/v1/diagnose")
def create_diagnosis(incident: IncidentInput) -> DiagnosisReport:
    return diagnose(incident, incident_id=uuid4())
