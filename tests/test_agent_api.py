import asyncio
import os
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from threading import Event
from types import ModuleType, SimpleNamespace
from typing import Any

import anyio
import pytest
from httpx import ASGITransport, AsyncClient

from k8s_incident_copilot.main import app
from k8s_incident_copilot.schemas import DiagnosisReport, IncidentInput

pytestmark = pytest.mark.anyio

DIAGNOSE = "/api/v1/diagnose"
KNOWN_INCIDENT = {
    "status": "Pending",
    "events": [{"reason": "Failed", "message": "Failed to pull image example:1: unauthorized"}],
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in os.environ.copy():
        if key.startswith("KIC_LLM_"):
            monkeypatch.delenv(key)


@pytest.fixture
async def client(offline_env: None) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client


@pytest.fixture
def agent_module() -> ModuleType:
    return import_module("k8s_incident_copilot.agent")


@pytest.fixture
def configured_model(offline_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIC_LLM_BASE_URL", "https://model.example.invalid/v1")
    monkeypatch.setenv("KIC_LLM_MODEL", "test-model")


async def diagnose(
    client: AsyncClient, payload: dict[str, Any], mode: str | None
) -> dict[str, Any]:
    params = {"mode": mode} if mode is not None else None
    response = await client.post(DIAGNOSE, params=params, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def baseline_fields(report: dict[str, Any]) -> dict[str, Any]:
    return {
        field: value for field, value in report.items() if field not in {"incident_id", "agent"}
    }


async def test_agent_uses_rules_and_retrieval_without_a_model(client: AsyncClient) -> None:
    baseline = await diagnose(client, KNOWN_INCIDENT, "rag")
    report = await diagnose(client, KNOWN_INCIDENT, "agent")

    assert report["failure_type"] == "ImagePullBackOff"
    assert report["diagnosis_method"] == "rules+rag"
    assert baseline_fields(report) == baseline_fields(baseline)
    agent = report["agent"]
    assert agent["tools"] == [
        {"name": "classify", "status": "ok"},
        {"name": "retrieve", "status": "ok"},
    ]
    assert agent["model_status"] == "not_configured"
    assert agent["prioritized_steps"] == []


async def test_agent_skips_retrieval_and_model_when_evidence_is_insufficient(
    client: AsyncClient,
) -> None:
    payload = {"events": [{"reason": "BackOff", "message": "Retry scheduled"}]}
    baseline = await diagnose(client, payload, "rules")
    report = await diagnose(client, payload, "agent")

    assert report["status"] == "insufficient_evidence"
    assert baseline_fields(report) == baseline_fields(baseline)
    assert report["agent"] == {
        "tools": [{"name": "classify", "status": "ok"}, {"name": "retrieve", "status": "skipped"}],
        "model_status": "skipped",
        "prioritized_steps": [],
    }


@pytest.mark.usefixtures("configured_model")
async def test_ordinary_modes_do_not_use_a_configured_model(
    client: AsyncClient, agent_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def model(_config: object, report: DiagnosisReport) -> list[str]:
        nonlocal calls
        calls += 1
        return report.recommended_steps[:1]

    monkeypatch.setattr(agent_module, "prioritize_steps", model)

    for mode, method in ((None, "rules"), ("rules", "rules"), ("rag", "rules+rag")):
        report = await diagnose(client, KNOWN_INCIDENT, mode)
        assert report["diagnosis_method"] == method
        assert "agent" not in report
    assert calls == 0


async def test_agent_retries_transient_retrieval_error(
    client: AsyncClient, agent_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = await diagnose(client, KNOWN_INCIDENT, "rag")
    actual_augment = agent_module.augment
    attempts = 0

    def interrupted(incident: IncidentInput, report: DiagnosisReport) -> DiagnosisReport:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("Temporary retrieval failure")
        return actual_augment(incident, report)

    monkeypatch.setattr(agent_module, "augment", interrupted)
    report = await diagnose(client, KNOWN_INCIDENT, "agent")

    assert attempts == 2
    assert baseline_fields(report) == baseline_fields(baseline)
    assert {"name": "retrieve", "status": "ok"} in report["agent"]["tools"]


@pytest.mark.parametrize(("error", "expected_attempts"), [(OSError, 2), (ValueError, 1)])
async def test_agent_preserves_rules_when_retrieval_fails(
    client: AsyncClient,
    agent_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
    expected_attempts: int,
) -> None:
    baseline = await diagnose(client, KNOWN_INCIDENT, "rules")
    attempts = 0

    def failed(_incident: IncidentInput, _report: DiagnosisReport) -> DiagnosisReport:
        nonlocal attempts
        attempts += 1
        raise error("Retrieval failed")

    monkeypatch.setattr(agent_module, "augment", failed)
    report = await diagnose(client, KNOWN_INCIDENT, "agent")

    assert attempts == expected_attempts
    assert baseline_fields(report) == baseline_fields(baseline)
    assert {"name": "retrieve", "status": "unavailable"} in report["agent"]["tools"]


async def test_agent_preserves_rules_when_retrieval_times_out(
    client: AsyncClient, agent_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = await diagnose(client, KNOWN_INCIDENT, "rules")
    attempts = 0

    async def slow_retrieval(_payload: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        await anyio.sleep_forever()

    monkeypatch.setattr(agent_module, "retrieve_tool", SimpleNamespace(ainvoke=slow_retrieval))
    monkeypatch.setattr(
        agent_module, "AGENT_GRAPH", agent_module.build_agent_graph(retrieval_timeout=0.03)
    )
    with anyio.fail_after(2):
        report = await diagnose(client, KNOWN_INCIDENT, "agent")

    assert attempts == 2
    assert baseline_fields(report) == baseline_fields(baseline)
    assert {"name": "retrieve", "status": "timed_out"} in report["agent"]["tools"]


async def test_blocking_retrieval_does_not_block_agent_fallback(
    client: AsyncClient, agent_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = Event()
    release = Event()

    def blocking_retrieval(_incident: IncidentInput, report: DiagnosisReport) -> DiagnosisReport:
        started.set()
        release.wait(5)
        return report

    monkeypatch.setattr(agent_module, "augment", blocking_retrieval)
    monkeypatch.setattr(
        agent_module, "AGENT_GRAPH", agent_module.build_agent_graph(retrieval_timeout=0.1)
    )
    with ThreadPoolExecutor(max_workers=1) as default_executor:
        asyncio.get_running_loop().set_default_executor(default_executor)
        try:
            with anyio.fail_after(2):
                report = await diagnose(client, KNOWN_INCIDENT, "agent")
        finally:
            release.set()

    assert started.is_set()
    assert report["diagnosis_method"] == "rules"
    assert {"name": "retrieve", "status": "timed_out"} in report["agent"]["tools"]


@pytest.mark.usefixtures("configured_model")
async def test_agent_retries_transient_model_error_and_returns_prioritized_steps(
    client: AsyncClient, agent_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = await diagnose(client, KNOWN_INCIDENT, "rag")
    attempts = 0

    async def model(_config: object, report: DiagnosisReport) -> list[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise agent_module.RetryableModelError("Temporary model failure")
        return report.recommended_steps[-1:]

    monkeypatch.setattr(agent_module, "prioritize_steps", model)
    report = await diagnose(client, KNOWN_INCIDENT, "agent")

    assert attempts == 2
    assert baseline_fields(report) == baseline_fields(baseline)
    assert report["agent"]["model_status"] == "used"
    assert report["agent"]["prioritized_steps"] == baseline["recommended_steps"][-1:]


@pytest.mark.usefixtures("configured_model")
async def test_agent_does_not_retry_an_invalid_model_response(
    client: AsyncClient, agent_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = await diagnose(client, KNOWN_INCIDENT, "rag")
    attempts = 0

    async def model(_config: object, _report: DiagnosisReport) -> list[str]:
        nonlocal attempts
        attempts += 1
        raise agent_module.InvalidModelResponse("Model returned invalid steps")

    monkeypatch.setattr(agent_module, "prioritize_steps", model)
    report = await diagnose(client, KNOWN_INCIDENT, "agent")

    assert attempts == 1
    assert baseline_fields(report) == baseline_fields(baseline)
    assert report["agent"]["model_status"] == "invalid_response"
    assert report["agent"]["prioritized_steps"] == []


@pytest.mark.usefixtures("configured_model")
async def test_agent_preserves_retrieval_when_model_times_out(
    client: AsyncClient, agent_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = await diagnose(client, KNOWN_INCIDENT, "rag")

    async def model(_config: object, _report: DiagnosisReport) -> list[str]:
        await anyio.sleep_forever()
        return []

    monkeypatch.setattr(agent_module, "prioritize_steps", model)
    monkeypatch.setattr(
        agent_module, "AGENT_GRAPH", agent_module.build_agent_graph(model_timeout=0.03)
    )
    with anyio.fail_after(2):
        report = await diagnose(client, KNOWN_INCIDENT, "agent")

    assert baseline_fields(report) == baseline_fields(baseline)
    assert {"name": "retrieve", "status": "ok"} in report["agent"]["tools"]
    assert report["agent"]["model_status"] == "timed_out"
    assert report["agent"]["prioritized_steps"] == []
