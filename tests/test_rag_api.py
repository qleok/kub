import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from k8s_incident_copilot.main import app

pytestmark = pytest.mark.anyio

DIAGNOSE = "/api/v1/diagnose"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT_ROOT / "examples"
KNOWLEDGE = PROJECT_ROOT / "src" / "k8s_incident_copilot" / "knowledge"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client


async def diagnose(
    client: AsyncClient, payload: dict[str, Any], mode: str | None = None
) -> dict[str, Any]:
    params = {"mode": mode} if mode is not None else None
    response = await client.post(DIAGNOSE, params=params, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def read_example(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def without_incident_id(report: dict[str, Any]) -> dict[str, Any]:
    return {field: value for field, value in report.items() if field != "incident_id"}


async def test_omitting_mode_preserves_the_rules_report(client: AsyncClient) -> None:
    payload = read_example("imagepullbackoff")
    implicit = await diagnose(client, payload)
    explicit = await diagnose(client, payload, mode="rules")

    assert without_incident_id(implicit) == without_incident_id(explicit)
    assert implicit["diagnosis_method"] == "rules"
    assert implicit["references"]
    assert all(set(reference) == {"title", "url"} for reference in implicit["references"])


async def test_rejects_unsupported_mode(client: AsyncClient) -> None:
    response = await client.post(DIAGNOSE, params={"mode": "hybrid"}, json={"status": "OOMKilled"})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    ("example", "failure_type"),
    [
        ("crashloopbackoff", "CrashLoopBackOff"),
        ("imagepullbackoff", "ImagePullBackOff"),
        ("oomkilled", "OOMKilled"),
    ],
)
async def test_rag_adds_official_citations_without_changing_the_diagnosis(
    client: AsyncClient, example: str, failure_type: str
) -> None:
    payload = read_example(example)
    baseline = await diagnose(client, payload, mode="rules")
    enriched = await diagnose(client, payload, mode="rag")

    assert enriched["diagnosis_method"] == "rules+rag"
    assert enriched["status"] == "diagnosed"
    assert enriched["failure_type"] == failure_type
    for field in ("status", "failure_type", "severity", "evidence"):
        assert enriched[field] == baseline[field]

    steps = enriched["recommended_steps"]
    assert isinstance(steps, list) and steps
    assert all(isinstance(step, str) and step.strip() for step in steps)

    references = enriched["references"]
    assert isinstance(references, list) and references
    source = (KNOWLEDGE / f"{example}.md").read_text(encoding="utf-8")
    source_lines = source.splitlines()
    source_text = " ".join(source.split())
    for reference in references:
        assert set(reference) == {"title", "url", "chunk_id", "excerpt", "recommended_step"}
        for field in ("title", "chunk_id", "excerpt", "recommended_step"):
            assert isinstance(reference[field], str) and reference[field].strip()
        url = urlsplit(reference["url"])
        assert url.scheme == "https"
        assert url.hostname in {"kubernetes.io", "www.kubernetes.io"}
        assert url.fragment
        assert f"## {reference['title']}" in source_lines
        assert f"Source: {reference['url']}" in source_lines
        assert " ".join(reference["excerpt"].split()) in source_text
        step = reference["recommended_step"]
        assert f"Action: {step}" in source_lines
        assert step in steps
    assert len({reference["chunk_id"] for reference in references}) == len(references)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"events": [{"reason": "BackOff", "message": "Retry scheduled"}]},
            id="unrecognized",
        ),
        pytest.param(
            {
                "events": [
                    {"reason": "Failed", "message": "Failed to pull image example:1: unauthorized"},
                    {"reason": "BackOff", "message": "Back-off restarting failed container app"},
                ],
            },
            id="conflicting",
        ),
    ],
)
async def test_rag_leaves_insufficient_evidence_report_unchanged(
    client: AsyncClient, payload: dict[str, Any]
) -> None:
    baseline = await diagnose(client, payload, mode="rules")
    report = await diagnose(client, payload, mode="rag")

    assert report["status"] == "insufficient_evidence"
    assert report["diagnosis_method"] == "rules"
    assert report["references"] == []
    assert without_incident_id(report) == without_incident_id(baseline)


@pytest.mark.parametrize("status", ["Pending", "ImagePullBackOff"])
async def test_rag_ranks_image_pull_citations_by_event_detail(
    client: AsyncClient, status: str
) -> None:
    def image_pull(detail: str) -> dict[str, Any]:
        message = f"Failed to pull image private-registry.example.com/app:v2: {detail}"
        return {
            "status": status,
            "events": [{"reason": "Failed", "message": message}],
        }

    auth_payload = image_pull("unauthorized")
    auth_report = await diagnose(client, auth_payload, mode="rag")
    repeated_report = await diagnose(client, auth_payload, mode="rag")
    manifest_report = await diagnose(client, image_pull("manifest unknown"), mode="rag")

    assert auth_report["failure_type"] == manifest_report["failure_type"] == "ImagePullBackOff"
    assert auth_report["diagnosis_method"] == manifest_report["diagnosis_method"] == "rules+rag"
    assert auth_report["references"] == repeated_report["references"]
    assert auth_report["evidence"] == manifest_report["evidence"]
    assert all("private-registry.example.com" not in item for item in auth_report["evidence"])

    auth_reference = auth_report["references"][0]
    manifest_reference = manifest_report["references"][0]
    assert urlsplit(auth_reference["url"]).path.endswith("/pull-image-private-registry/")
    assert manifest_reference["chunk_id"] != auth_reference["chunk_id"]
    manifest_excerpt = manifest_reference["excerpt"].casefold()
    assert any(marker in manifest_excerpt for marker in ("tag", "digest", "manifest"))
