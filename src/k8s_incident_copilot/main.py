"""HTTP entry point for the diagnosis service."""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from anyio import to_thread
from fastapi import FastAPI, HTTPException, Query

from .agent import run_agent
from .diagnosis import diagnose
from .retrieval import augment
from .schemas import AgentDiagnosisReport, DiagnosisReport, HealthResponse, IncidentInput

app = FastAPI(
    title="K8s Incident Copilot",
    description="Local Kubernetes incident diagnosis with retrieval and a LangGraph agent.",
    version="0.3.0",
)


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse()


@app.post("/api/v1/diagnose")
async def create_diagnosis(
    incident: IncidentInput,
    mode: Annotated[
        Literal["rules", "rag", "agent"],
        Query(description="Choose rules, retrieval enrichment, or the LangGraph agent."),
    ] = "rules",
) -> AgentDiagnosisReport | DiagnosisReport:
    incident_id = uuid4()
    if mode == "agent":
        return await run_agent(incident, incident_id=incident_id)

    return await to_thread.run_sync(_diagnose_local, incident, incident_id, mode)


def _diagnose_local(
    incident: IncidentInput, incident_id: UUID, mode: Literal["rules", "rag"]
) -> DiagnosisReport:
    report = diagnose(incident, incident_id=incident_id)
    if mode == "rules":
        return report

    try:
        return augment(incident, report)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail="The bundled Kubernetes knowledge base is unavailable."
        ) from exc
