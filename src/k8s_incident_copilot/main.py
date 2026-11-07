"""HTTP entry point for the diagnosis service."""

from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query

from .diagnosis import diagnose
from .retrieval import augment
from .schemas import DiagnosisReport, HealthResponse, IncidentInput

app = FastAPI(
    title="K8s Incident Copilot",
    description="Local Kubernetes incident diagnosis with optional documentation retrieval.",
    version="0.2.0",
)


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse()


@app.post("/api/v1/diagnose")
def create_diagnosis(
    incident: IncidentInput,
    mode: Annotated[
        Literal["rules", "rag"],
        Query(description="Use rules alone or enrich them with local Kubernetes documentation."),
    ] = "rules",
) -> DiagnosisReport:
    report = diagnose(incident, incident_id=uuid4())
    if mode == "rules":
        return report

    try:
        return augment(incident, report)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail="The bundled Kubernetes knowledge base is unavailable."
        ) from exc
