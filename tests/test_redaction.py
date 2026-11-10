from time import perf_counter

import pytest

from k8s_incident_copilot.redaction import redact_text


@pytest.mark.parametrize(
    ("text", "secrets"),
    [
        ("Authorization: Bearer test-bearer-value", ("test-bearer-value",)),
        ("authorization=Basic ZGVtbzpmaXh0dXJl", ("ZGVtbzpmaXh0dXJl",)),
        ('{"client_secret": "fixture client secret"}', ("fixture client secret",)),
        ("password='fixture password' port=5432", ("fixture password",)),
        (
            "AWS_SECRET_ACCESS_KEY=fixture-key DB_PASSWORD=fixture-pass",
            ("fixture-key", "fixture-pass"),
        ),
        (
            "https://fixture-user:fixture-password@registry.example/v2",
            ("fixture-user", "fixture-password"),
        ),
        ("url=https://registry.example/v2?access_token=fixture-token&retry=1", ("fixture-token",)),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkZW1vIn0.signature123", ("signature123",)),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nfixture-body\n-----END RSA PRIVATE KEY-----",
            ("fixture-body",),
        ),
        ("-----BEGIN PRIVATE KEY-----\nfixture-truncated", ("fixture-truncated",)),
    ],
)
def test_redaction_removes_common_credential_formats(text: str, secrets: tuple[str, ...]) -> None:
    cleaned = redact_text(text)
    assert all(secret not in cleaned for secret in secrets)
    assert "[REDACTED]" in cleaned


def test_redaction_preserves_diagnostic_context() -> None:
    text = "Failed to pull image registry.example/app:v2: unauthorized"
    assert redact_text(text) == text


def test_redaction_handles_long_hyphenated_text() -> None:
    text = "x-" * 8192
    start = perf_counter()
    assert redact_text(text) == text
    assert perf_counter() - start < 2
