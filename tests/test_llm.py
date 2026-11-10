import gzip
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeAlias
from uuid import UUID

import httpx
import pytest

from k8s_incident_copilot import llm
from k8s_incident_copilot.diagnosis import diagnose
from k8s_incident_copilot.schemas import DiagnosisReport, IncidentInput

BASE_URL = "https://model.example/v1/"
MODEL = "demo-model"
API_KEY = "test-token"
INCIDENT_ID = UUID("49e180a8-f537-4288-aadc-b8b6cb39da99")
MockHTTP: TypeAlias = Callable[[Callable[[httpx.Request], httpx.Response]], None]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def report() -> DiagnosisReport:
    incident = IncidentInput(
        pod_name="private-pod-name",
        namespace="private-namespace",
        status="CrashLoopBackOff",
        events=[{"message": "private-event"}],
        logs=["private-log"],
        description="private-description",
    )
    baseline = diagnose(incident, incident_id=INCIDENT_ID)
    return baseline.model_copy(
        update={
            "recommended_steps": [*baseline.recommended_steps, "Review the Pod restart policy."],
            "diagnosis_method": "rules+rag",
        }
    )


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch) -> MockHTTP:
    client = httpx.AsyncClient

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        def build_client(**kwargs: Any) -> httpx.AsyncClient:
            return client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(llm.httpx, "AsyncClient", build_client)

    return install


def model_response(content: str | None) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


class UnreadableBody(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        pytest.fail("The adapter tried to read an error response body.")
        yield b""


def test_reads_optional_model_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIC_LLM_BASE_URL", " https://model.example/v1/ ")
    monkeypatch.setenv("KIC_LLM_MODEL", " demo-model ")
    monkeypatch.setenv("KIC_LLM_API_KEY", " test-token ")

    config = llm.read_model_config()

    assert config is not None
    assert config.base_url == "https://model.example/v1/"
    assert config.model == "demo-model"
    assert config.api_key == "test-token"
    assert "test-token" not in repr(config)


@pytest.mark.parametrize("missing", ["KIC_LLM_BASE_URL", "KIC_LLM_MODEL"])
def test_model_disabled_without_both_nonblank_settings(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("KIC_LLM_BASE_URL", BASE_URL)
    monkeypatch.setenv("KIC_LLM_MODEL", MODEL)
    monkeypatch.setenv(missing, " ")

    assert llm.read_model_config() is None


@pytest.mark.anyio
@pytest.mark.parametrize(("api_key", "priorities"), [(API_KEY, [4, 1, 2]), (None, [3])])
async def test_prioritization_sends_only_generated_data_and_returns_existing_steps(
    mock_http: MockHTTP,
    report: DiagnosisReport,
    api_key: str | None,
    priorities: list[int],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://model.example/v1/chat/completions"
        assert request.headers.get("Authorization") == (f"Bearer {api_key}" if api_key else None)
        body = json.loads(request.content)
        assert body["model"] == MODEL
        assert body["response_format"] == {"type": "json_object"}
        assert json.loads(body["messages"][1]["content"]) == {
            "failure_type": report.failure_type,
            "severity": report.severity,
            "steps": [
                {"index": index, "text": step}
                for index, step in enumerate(report.recommended_steps, start=1)
            ],
        }
        assert b"private-" not in request.content
        assert str(INCIDENT_ID).encode() not in request.content
        assert API_KEY.encode() not in request.content
        return model_response(json.dumps({"priority_steps": priorities}))

    mock_http(handle)
    original_steps = report.recommended_steps.copy()

    result = await llm.prioritize_steps(llm.ModelConfig(BASE_URL, MODEL, api_key), report)

    assert result == [original_steps[index - 1] for index in priorities]
    assert report.recommended_steps == original_steps


@pytest.mark.anyio
@pytest.mark.parametrize(
    "base_url",
    ["not-a-url", "ftp://model.example/v1", "https://user:secret@model.example/v1", f"{BASE_URL}?"],
)
async def test_invalid_base_url_is_unavailable_without_sending_a_request(
    mock_http: MockHTTP,
    report: DiagnosisReport,
    base_url: str,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid base URL was used for an outbound request.")

    mock_http(handle)

    with pytest.raises(llm.ModelUnavailable) as failure:
        await llm.prioritize_steps(llm.ModelConfig(base_url, MODEL, API_KEY), report)

    assert type(failure.value) is llm.ModelUnavailable
    assert base_url not in str(failure.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "retryable"),
    [(307, False), (400, False), (401, False), (408, True), (429, True), (500, True), (503, True)],
)
async def test_http_failure_classification_and_no_redirects(
    mock_http: MockHTTP,
    report: DiagnosisReport,
    status: int,
    retryable: bool,
) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, headers={"Location": "https://unexpected.example"}, stream=UnreadableBody()
        )

    mock_http(handle)
    error = llm.RetryableModelError if retryable else llm.ModelUnavailable

    with pytest.raises(error) as failure:
        await llm.prioritize_steps(llm.ModelConfig(BASE_URL, MODEL, API_KEY), report)

    assert type(failure.value) is error
    assert len(requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("network_error", [httpx.ConnectError, httpx.ReadTimeout])
async def test_transport_failures_are_retryable_and_sanitized(
    mock_http: MockHTTP,
    report: DiagnosisReport,
    network_error: type[httpx.TransportError],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise network_error("private-transport-details", request=request)

    mock_http(handle)
    error = llm.ModelTimeout if network_error is httpx.ReadTimeout else llm.RetryableModelError

    with pytest.raises(error) as failure:
        await llm.prioritize_steps(llm.ModelConfig(BASE_URL, MODEL, API_KEY), report)

    assert type(failure.value) is error
    assert "private-transport-details" not in str(failure.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    [
        None,
        "private-model-text",
        '{"priority_steps":[]}',
        '{"priority_steps":[1,2,3,4]}',
        '{"priority_steps":[2,2]}',
        '{"priority_steps":[0]}',
        '{"priority_steps":[5]}',
        '{"priority_steps":[true]}',
        '{"priority_steps":[1.0]}',
        '{"priority_steps":["1"]}',
        '{"priority_steps":[1],"diagnosis":"Invented diagnosis"}',
    ],
)
async def test_invalid_priorities_are_unavailable_without_retry(
    mock_http: MockHTTP,
    report: DiagnosisReport,
    content: str | None,
) -> None:
    mock_http(lambda request: model_response(content))

    with pytest.raises(llm.InvalidModelResponse) as failure:
        await llm.prioritize_steps(llm.ModelConfig(BASE_URL, MODEL, API_KEY), report)

    assert not isinstance(failure.value, llm.RetryableModelError)
    assert "private-model-text" not in str(failure.value)


@pytest.mark.anyio
async def test_model_rejects_an_oversized_success_response(
    mock_http: MockHTTP, report: DiagnosisReport
) -> None:
    response = {
        "choices": [{"message": {"content": '{"priority_steps":[1]}'}}],
        "padding": "x" * 65536,
    }
    mock_http(lambda request: httpx.Response(200, json=response))

    with pytest.raises(llm.InvalidModelResponse):
        await llm.prioritize_steps(llm.ModelConfig(BASE_URL, MODEL), report)


@pytest.mark.anyio
async def test_model_rejects_compressed_responses(
    mock_http: MockHTTP, report: DiagnosisReport
) -> None:
    body = gzip.compress(model_response('{"priority_steps":[1]}').content)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=body)

    mock_http(handle)

    with pytest.raises(llm.InvalidModelResponse):
        await llm.prioritize_steps(llm.ModelConfig(BASE_URL, MODEL), report)
