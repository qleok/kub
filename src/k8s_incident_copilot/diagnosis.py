"""Deterministic diagnosis from a caller-supplied incident snapshot."""

import re
from dataclasses import dataclass
from uuid import UUID

from pydantic import AnyHttpUrl

from .schemas import DiagnosisReport, FailureType, IncidentInput, PodEvent, Reference, Severity


@dataclass(frozen=True)
class Rule:
    suspected_cause: str
    severity: Severity
    steps: tuple[str, ...]
    reference_title: str
    reference_url: str


@dataclass(frozen=True)
class Signal:
    failure_type: FailureType
    evidence: str


RULES: dict[FailureType, Rule] = {
    "CrashLoopBackOff": Rule(
        suspected_cause=(
            "The container is repeatedly exiting and Kubernetes is delaying restarts. "
            "The backoff marker alone does not identify why it exits."
        ),
        severity="high",
        steps=(
            "Inspect the previous container logs, last termination reason, and exit code.",
            "Check the entrypoint, required configuration, and startup or liveness probe events.",
            "Fix the confirmed startup failure, then check whether the restart count stabilizes.",
        ),
        reference_title="Kubernetes: How Pods handle problems with containers",
        reference_url=(
            "https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/"
            "#how-pods-handle-problems-with-containers"
        ),
    ),
    "ImagePullBackOff": Rule(
        suspected_cause=(
            "The container cannot start because the kubelet could not pull its image. "
            "The pull error is needed to distinguish a bad reference, access issue, or outage."
        ),
        severity="medium",
        steps=(
            "Inspect the Pod pull events for the registry error.",
            "Verify the image reference and the pull secret in the Pod's namespace.",
            "Correct the image reference, credentials, or registry connectivity as indicated.",
        ),
        reference_title="Kubernetes: ImagePullBackOff",
        reference_url="https://kubernetes.io/docs/concepts/containers/images/#imagepullbackoff",
    ),
    "OOMKilled": Rule(
        suspected_cause=(
            "The container appears to have been terminated by an out-of-memory kill. "
            "Check whether container memory limits or node memory pressure contributed."
        ),
        severity="high",
        steps=(
            "Confirm OOMKilled in the last termination state for the affected container.",
            "Compare peak memory with the container limit and check node memory pressure.",
            "Investigate memory spikes or leaks; adjust resources only after measuring demand.",
        ),
        reference_title="Kubernetes: Troubleshoot container termination",
        reference_url=(
            "https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/"
            "#my-container-is-terminated"
        ),
    ),
}

REASON_MARKERS: dict[str, tuple[FailureType, str]] = {
    **{failure.casefold(): (failure, failure) for failure in RULES},
    "errimagepull": ("ImagePullBackOff", "ErrImagePull"),
}
EVENT_PREFIXES: dict[str, FailureType] = {
    "Failed to pull image": "ImagePullBackOff",
    "Back-off pulling image": "ImagePullBackOff",
    "Back-off restarting failed container": "CrashLoopBackOff",
}
# Recognize standalone Kubernetes tokens even when adjacent to non-Latin prose.
TEXT_MARKERS = re.compile(rf"\b(?:{'|'.join(REASON_MARKERS)})\b", re.IGNORECASE | re.ASCII)


def _reason_signal(value: str | None, source: str) -> Signal | None:
    key = (value or "").casefold()
    # kubectl prefixes a failure reason with Init: for init container status.
    prefix = "Init:" if source == "status" and key.startswith("init:") else ""
    match = REASON_MARKERS.get(key.removeprefix("init:") if prefix else key)
    if match is None:
        return None
    failure, marker = match
    return Signal(failure, f"{source} reports {prefix}{marker}.")


def _event_signals(events: list[PodEvent]) -> list[Signal]:
    signals = []
    for index, event in enumerate(events):
        source = f"events[{index}]"
        signal = _reason_signal(event.reason, f"{source}.reason")
        if signal:
            signals.append(signal)
        message = (event.message or "").casefold()
        if (event.reason or "").casefold() == "failed" and message.startswith("error:"):
            error_reason = message.removeprefix("error:").strip()
            if signal := _reason_signal(error_reason, f"{source}.message"):
                signals.append(signal)
        for prefix, failure in EVENT_PREFIXES.items():
            if re.match(rf"{re.escape(prefix.casefold())}\b", message):
                signals.append(Signal(failure, f"{source}.message begins with '{prefix}'."))
    return signals


def _text_signals(value: str, source: str) -> list[Signal]:
    # Evidence reports the recognized marker rather than echoing the caller's text.
    return [
        Signal(failure, f"{source} contains marker {marker}.")
        for failure, marker in dict.fromkeys(
            REASON_MARKERS[match.group().lower()] for match in TEXT_MARKERS.finditer(value)
        )
    ]


def _best_signals(incident: IncidentInput) -> list[Signal]:
    for value, source in (
        (incident.container_reason, "container_reason"),
        (incident.status, "status"),
    ):
        if signal := _reason_signal(value, source):
            return [signal]

    if signals := _event_signals(incident.events):
        return signals

    for index, line in enumerate(incident.logs):
        signals.extend(_text_signals(line, f"logs[{index}]"))
    signals.extend(_text_signals(incident.description or "", "description"))
    return signals


def diagnose(incident: IncidentInput, *, incident_id: UUID) -> DiagnosisReport:
    signals = _best_signals(incident)
    candidates = {signal.failure_type for signal in signals}
    evidence = [signal.evidence for signal in signals]

    if len(candidates) != 1:
        next_step = (
            "Separate conflicting signals by container and timestamp."
            if candidates
            else "Collect the current container state and last termination reason."
        )
        return DiagnosisReport(
            incident_id=incident_id,
            status="insufficient_evidence",
            failure_type=None,
            suspected_cause=None,
            severity="unknown",
            evidence=evidence,
            recommended_steps=[next_step, "Include recent Pod events and relevant container logs."],
            references=[],
        )

    failure = candidates.pop()
    rule = RULES[failure]
    return DiagnosisReport(
        incident_id=incident_id,
        status="diagnosed",
        failure_type=failure,
        suspected_cause=rule.suspected_cause,
        severity=rule.severity,
        evidence=evidence,
        recommended_steps=list(rule.steps),
        references=[Reference(title=rule.reference_title, url=AnyHttpUrl(rule.reference_url))],
    )
