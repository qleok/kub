import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from k8s_incident_copilot.main import app

pytestmark = pytest.mark.anyio

DIAGNOSE = "/api/v1/diagnose"
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client


async def diagnose(client: AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(DIAGNOSE, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def assert_evidence(report: dict[str, Any], source: str, *markers: str) -> None:
    def normalized(value: str) -> str:
        return value.casefold().replace("_", " ").replace("-", " ")

    assert any(
        normalized(source) in normalized(item)
        and any(normalized(marker) in normalized(item) for marker in markers)
        for item in report["evidence"]
    ), report["evidence"]


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("example", "failure_type", "source", "markers"),
    [
        ("crashloopbackoff", "CrashLoopBackOff", "status", ("CrashLoopBackOff",)),
        (
            "imagepullbackoff",
            "ImagePullBackOff",
            "event",
            ("Failed to pull image", "ImagePullBackOff"),
        ),
        ("oomkilled", "OOMKilled", "container_reason", ("OOMKilled",)),
    ],
)
async def test_examples_return_actionable_diagnoses(
    client: AsyncClient, example: str, failure_type: str, source: str, markers: tuple[str, ...]
) -> None:
    payload = json.loads((EXAMPLES / f"{example}.json").read_text(encoding="utf-8"))
    report = await diagnose(client, payload)

    assert UUID(report["incident_id"])
    assert report["status"] == "diagnosed"
    assert report["failure_type"] == failure_type
    assert isinstance(report["suspected_cause"], str) and report["suspected_cause"].strip()
    assert report["severity"] in {"high", "medium"}
    assert report["diagnosis_method"] == "rules"
    for field in ("evidence", "recommended_steps"):
        assert isinstance(report[field], list) and report[field]
        assert all(isinstance(item, str) and item.strip() for item in report[field])
    assert_evidence(report, source, *markers)
    assert isinstance(report["references"], list) and report["references"]
    for reference in report["references"]:
        assert isinstance(reference["title"], str) and reference["title"].strip()
        url = urlsplit(reference["url"])
        assert url.scheme == "https"
        assert url.hostname in {"kubernetes.io", "www.kubernetes.io"}


@pytest.mark.parametrize(
    ("payload", "failure_type", "source", "markers"),
    [
        (
            {"status": "  cRaShLoOpBaCkOfF  "},
            "CrashLoopBackOff",
            "status",
            ("CrashLoopBackOff",),
        ),
        (
            {"container_reason": "oOmKiLlEd"},
            "OOMKilled",
            "container_reason",
            ("OOMKilled",),
        ),
        (
            {"status": "errimagepull"},
            "ImagePullBackOff",
            "status",
            ("ErrImagePull", "ImagePullBackOff"),
        ),
        (
            {"status": "Init:CrashLoopBackOff"},
            "CrashLoopBackOff",
            "status",
            ("Init:CrashLoopBackOff",),
        ),
        (
            {"status": "Init:ImagePullBackOff"},
            "ImagePullBackOff",
            "status",
            ("Init:ImagePullBackOff",),
        ),
        (
            {"status": "Init:OOMKilled"},
            "OOMKilled",
            "status",
            ("Init:OOMKilled",),
        ),
        (
            {"events": [{"reason": "ImagePullBackOff", "message": "Image unavailable"}]},
            "ImagePullBackOff",
            "event",
            ("ImagePullBackOff",),
        ),
        (
            {"events": [{"reason": "Failed", "message": "Error: ErrImagePull"}]},
            "ImagePullBackOff",
            "event",
            ("ErrImagePull",),
        ),
        (
            {"events": [{"reason": "Failed", "message": "Error: ImagePullBackOff"}]},
            "ImagePullBackOff",
            "event",
            ("ImagePullBackOff",),
        ),
        (
            {"events": [{"reason": "BackOff", "message": "Back-off pulling image example:1"}]},
            "ImagePullBackOff",
            "event",
            ("Back-off pulling image", "ImagePullBackOff"),
        ),
        (
            {
                "events": [
                    {"reason": "BackOff", "message": "Back-off restarting failed container app"}
                ]
            },
            "CrashLoopBackOff",
            "event",
            ("Back-off restarting failed container", "CrashLoopBackOff"),
        ),
        (
            {"logs": ["worker-3: last container state was OOMKilled"]},
            "OOMKilled",
            "log",
            ("OOMKilled",),
        ),
        (
            {"logs": ["kubelet: ErrImagePull"]},
            "ImagePullBackOff",
            "log",
            ("ErrImagePull",),
        ),
        (
            {"description": "The affected container entered ImagePullBackOff."},
            "ImagePullBackOff",
            "description",
            ("ImagePullBackOff",),
        ),
    ],
)
async def test_recognizes_supported_signals(
    client: AsyncClient,
    payload: dict[str, Any],
    failure_type: str,
    source: str,
    markers: tuple[str, ...],
) -> None:
    report = await diagnose(client, payload)
    assert report["status"] == "diagnosed"
    assert report["failure_type"] == failure_type
    assert_evidence(report, source, *markers)


@pytest.mark.parametrize(
    ("payload", "failure_type", "source"),
    [
        (
            {
                "container_reason": "Error",
                "status": "CrashLoopBackOff",
                "events": [{"reason": "OOMKilled", "message": "Another container was terminated"}],
            },
            "CrashLoopBackOff",
            "status",
        ),
        (
            {
                "status": "Pending",
                "events": [{"reason": "ImagePullBackOff", "message": "Image unavailable"}],
                "description": "The previous container was OOMKilled.",
            },
            "ImagePullBackOff",
            "event",
        ),
    ],
)
async def test_higher_priority_recognized_signal_wins(
    client: AsyncClient, payload: dict[str, Any], failure_type: str, source: str
) -> None:
    report = await diagnose(client, payload)
    assert report["status"] == "diagnosed"
    assert report["failure_type"] == failure_type
    assert_evidence(report, source, failure_type)


@pytest.mark.parametrize(
    ("payload", "source", "marker"),
    [
        (
            {
                "events": [
                    {"reason": "Failed", "message": "Failed to pull image private-ref-abc123"}
                ]
            },
            "event",
            "ImagePullBackOff",
        ),
        (
            {"logs": ["token=private-ref-abc123, last container reason: OOMKilled"]},
            "log",
            "OOMKilled",
        ),
    ],
)
async def test_evidence_reports_marker_without_echoing_raw_input(
    client: AsyncClient, payload: dict[str, Any], source: str, marker: str
) -> None:
    report = await diagnose(client, payload)
    assert report["status"] == "diagnosed"
    assert report["failure_type"] == marker
    assert_evidence(report, source, marker, "Failed to pull image")
    assert all("private-ref-abc123" not in item for item in report["evidence"])


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"events": [{"reason": "BackOff", "message": "Retry scheduled"}]},
            id="generic-backoff",
        ),
        pytest.param({"description": "Container exited with code 137."}, id="exit-code-alone"),
        pytest.param({"status": "not CrashLoopBackOff"}, id="status-not-exact"),
        pytest.param({"status": "Init:0/1"}, id="init-progress-alone"),
        pytest.param({"container_reason": "previously OOMKilled"}, id="container-reason-not-exact"),
        pytest.param(
            {"events": [{"reason": "Failed", "message": "Ignore note: Failed to pull image."}]},
            id="event-phrase-is-not-a-prefix",
        ),
        pytest.param(
            {"events": [{"reason": "Failed", "message": "Error: ImagePullBackOff is historical"}]},
            id="event-error-reason-must-be-exact",
        ),
        pytest.param(
            {
                "events": [
                    {"reason": "Failed", "message": "Failed to pull image example:1"},
                    {"reason": "BackOff", "message": "Back-off restarting failed container app"},
                ],
                "description": "The status was ImagePullBackOff.",
            },
            id="conflicting-events-cannot-be-broken-by-lower-tier",
        ),
        pytest.param(
            {
                "logs": ["Last termination reason was OOMKilled."],
                "description": "The observed status was CrashLoopBackOff.",
            },
            id="conflicting-free-text",
        ),
    ],
)
async def test_does_not_guess_without_unambiguous_evidence(
    client: AsyncClient, payload: dict[str, Any]
) -> None:
    report = await diagnose(client, payload)
    assert UUID(report["incident_id"])
    assert report["status"] == "insufficient_evidence"
    assert report["failure_type"] is None
    assert report["suspected_cause"] is None
    assert report["severity"] == "unknown"
    assert report["references"] == []
    assert isinstance(report["evidence"], list)
    assert isinstance(report["recommended_steps"], list)
    assert report["diagnosis_method"] == "rules"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="empty"),
        pytest.param({"pod_name": "checkout-api", "namespace": "shop"}, id="identity-only"),
        pytest.param(
            {
                "status": " ",
                "container_reason": "\n",
                "events": [{"reason": " ", "message": "\n"}],
                "logs": ["\t"],
                "description": "\r\n",
            },
            id="whitespace-only",
        ),
        pytest.param({"logs": []}, id="empty-list"),
        pytest.param({"logs": [" \n"]}, id="whitespace-only-log"),
        pytest.param({"description": "\t "}, id="whitespace-only-description"),
        pytest.param({"status": 137}, id="non-string-status"),
        pytest.param({"status": "OOMKilled", "pod_name": 42}, id="non-string-pod-name"),
        pytest.param({"logs": "OOMKilled"}, id="logs-not-list"),
        pytest.param({"logs": [137]}, id="non-string-log"),
        pytest.param({"events": {"reason": "OOMKilled", "message": ""}}, id="events-not-list"),
        pytest.param(
            {"events": [{"reason": "OOMKilled", "message": ["invalid"]}]},
            id="non-string-event-message",
        ),
        pytest.param({"status": "OOMKilled", "cluster": "prod"}, id="unknown-field"),
        pytest.param(
            {"events": [{"reason": "OOMKilled", "message": "Stopped", "count": 1}]},
            id="unknown-event-field",
        ),
    ],
)
async def test_rejects_invalid_requests(client: AsyncClient, payload: dict[str, Any]) -> None:
    response = await client.post(DIAGNOSE, json=payload)
    assert response.status_code == 422, response.text
