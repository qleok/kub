"""A request-scoped LangGraph workflow for local incident diagnosis."""

from asyncio import get_running_loop
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from langchain_core.tools import tool
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, RetryPolicy
from langsmith import tracing_context
from typing_extensions import TypedDict

from .diagnosis import diagnose
from .llm import (
    InvalidModelResponse,
    ModelConfig,
    ModelTimeout,
    ModelUnavailable,
    RetryableModelError,
    prioritize_steps,
    read_model_config,
)
from .retrieval import augment
from .schemas import (
    AgentDetails,
    AgentDiagnosisReport,
    AgentToolRun,
    AgentToolStatus,
    DiagnosisReport,
    IncidentInput,
    ModelStatus,
)

# Keep stalled corpus reads out of the executor used by graph routing and finalization.
_RETRIEVAL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kic-retrieval")


class AgentInput(TypedDict):
    incident: IncidentInput
    incident_id: UUID


class AgentOutput(TypedDict):
    result: AgentDiagnosisReport


class AgentState(AgentInput, total=False):
    report: DiagnosisReport
    retrieval_status: AgentToolStatus
    model_status: ModelStatus
    prioritized_steps: list[str]
    result: AgentDiagnosisReport


@dataclass(frozen=True)
class AgentContext:
    model: ModelConfig | None


@tool("classify")
def classify_tool(incident: IncidentInput, incident_id: UUID) -> DiagnosisReport:
    """Classify a submitted Pod snapshot with local Kubernetes diagnosis rules."""
    return diagnose(incident, incident_id=incident_id)


@tool("retrieve")
async def retrieve_tool(incident: IncidentInput, report: DiagnosisReport) -> DiagnosisReport:
    """Add troubleshooting guidance from the bundled Kubernetes documentation."""
    loop = get_running_loop()
    return await loop.run_in_executor(_RETRIEVAL_EXECUTOR, augment, incident, report)


async def _classify(state: AgentState) -> dict[str, DiagnosisReport]:
    report = await classify_tool.ainvoke(
        {"incident": state["incident"], "incident_id": state["incident_id"]}
    )
    return {"report": cast(DiagnosisReport, report)}


def _after_classify(state: AgentState) -> Literal["retrieve", "finalize"]:
    return "retrieve" if state["report"].failure_type is not None else "finalize"


async def _retrieve(state: AgentState) -> dict[str, DiagnosisReport | AgentToolStatus]:
    report = await retrieve_tool.ainvoke({"incident": state["incident"], "report": state["report"]})
    return {"report": cast(DiagnosisReport, report), "retrieval_status": "ok"}


def _retry_retrieval(error: Exception) -> bool:
    return isinstance(error, NodeTimeoutError) or (
        isinstance(error, OSError) and not isinstance(error, FileNotFoundError)
    )


def _retrieval_failed(state: AgentState, error: NodeError) -> Command[Literal["choose_model"]]:
    if isinstance(error.error, NodeTimeoutError):
        status: AgentToolStatus = "timed_out"
    elif isinstance(error.error, (OSError, ValueError)):
        status = "unavailable"
    else:
        raise error.error
    return Command(update={"retrieval_status": status}, goto="choose_model")


def _choose_model(
    state: AgentState, runtime: Runtime[AgentContext]
) -> Command[Literal["prioritize", "finalize"]]:
    if runtime.context.model is None:
        return Command(update={"model_status": "not_configured"}, goto="finalize")
    return Command(goto="prioritize")


async def _prioritize(
    state: AgentState, runtime: Runtime[AgentContext]
) -> dict[str, ModelStatus | list[str]]:
    model = runtime.context.model
    if model is None:
        raise RuntimeError("The model node requires an enabled provider.")
    steps = await prioritize_steps(model, state["report"])
    return {"prioritized_steps": steps, "model_status": "used"}


def _model_failed(state: AgentState, error: NodeError) -> Command[Literal["finalize"]]:
    if isinstance(error.error, (NodeTimeoutError, ModelTimeout)):
        status: ModelStatus = "timed_out"
    elif isinstance(error.error, InvalidModelResponse):
        status = "invalid_response"
    elif isinstance(error.error, ModelUnavailable):
        status = "unavailable"
    else:
        raise error.error
    return Command(update={"model_status": status}, goto="finalize")


def _finalize(state: AgentState) -> AgentOutput:
    details = AgentDetails(
        tools=[
            AgentToolRun(name="classify", status="ok"),
            AgentToolRun(name="retrieve", status=state.get("retrieval_status", "skipped")),
        ],
        model_status=state.get("model_status", "skipped"),
        prioritized_steps=state.get("prioritized_steps", []),
    )
    return {"result": AgentDiagnosisReport(**state["report"].model_dump(), agent=details)}


def build_agent_graph(
    *, retrieval_timeout: float = 1.0, model_timeout: float = 4.0
) -> CompiledStateGraph[AgentState, AgentContext, AgentInput, AgentOutput]:
    graph = StateGraph(
        AgentState, context_schema=AgentContext, input_schema=AgentInput, output_schema=AgentOutput
    )
    graph.add_node("classify", _classify)
    graph.add_node(
        "retrieve",
        _retrieve,
        timeout=retrieval_timeout,
        retry_policy=RetryPolicy(
            max_attempts=2, initial_interval=0.05, jitter=False, retry_on=_retry_retrieval
        ),
        error_handler=_retrieval_failed,
    )
    graph.add_node("choose_model", _choose_model)
    graph.add_node(
        "prioritize",
        _prioritize,
        timeout=model_timeout,
        retry_policy=RetryPolicy(
            max_attempts=2,
            initial_interval=0.05,
            jitter=False,
            retry_on=(RetryableModelError, NodeTimeoutError),
        ),
        error_handler=_model_failed,
    )
    graph.add_node("finalize", _finalize)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges("classify", _after_classify)
    graph.add_edge("retrieve", "choose_model")
    graph.add_edge("prioritize", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


AGENT_GRAPH = build_agent_graph()


async def run_agent(incident: IncidentInput, *, incident_id: UUID) -> AgentDiagnosisReport:
    context = AgentContext(model=read_model_config())
    # Incident input stays in request-local graph state and out of external traces.
    with tracing_context(enabled=False):
        output = await AGENT_GRAPH.ainvoke(
            {"incident": incident, "incident_id": incident_id}, context=context
        )
    return output["result"]
