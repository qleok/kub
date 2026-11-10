"""HTTP entry point for the diagnosis service."""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from anyio import CapacityLimiter, to_thread
from fastapi import FastAPI, HTTPException, Query

from .agent import run_agent
from .diagnosis import diagnose
from .kubernetes_snapshot import SnapshotError, collect_snapshot
from .retrieval import augment
from .schemas import (
    AgentDiagnosisReport,
    DiagnosisReport,
    HealthResponse,
    IncidentInput,
    PodDiagnosisReport,
    PodLookup,
)

app = FastAPI(
    title="K8s Incident Copilot",
    description="Kubernetes incident diagnosis with local retrieval and optional Pod collection.",
    version="0.4.0",
)
_KUBERNETES_WORKERS = CapacityLimiter(4)


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


@app.post("/api/v1/diagnose/pod")
async def diagnose_pod(lookup: PodLookup) -> PodDiagnosisReport:
    try:
        incident, source = await to_thread.run_sync(
            collect_snapshot, lookup, limiter=_KUBERNETES_WORKERS
        )
    except SnapshotError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None

    # Workload text can guide local retrieval but cannot establish a failure type.
    classification_input = incident.model_copy(
        update={"events": [], "logs": [], "description": None}
    )
    report = await run_agent(
        incident, incident_id=uuid4(), classification_input=classification_input
    )
    return PodDiagnosisReport(**report.model_dump(), kubernetes=source)


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
