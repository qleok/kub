"""Collect a bounded diagnostic snapshot with namespaced, read-only Kubernetes calls."""

import os
import re
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import urlsplit

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException, OpenApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError, MaxRetryError, NewConnectionError
from urllib3.exceptions import TimeoutError as UrllibTimeoutError

from .redaction import redact_text
from .schemas import IncidentInput, KubernetesDetails, PodEvent, PodLookup

_REQUEST_TIMEOUT = (2.0, 3.0)
_EVENT_LIMIT = 50
_LOG_LINES = 50
_LOG_BYTES = 16_384
_SELECTOR_UID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_STATIC_CONFIG_LOCK = Lock()
_API_ERRORS = (
    OpenApiException,
    HTTPError,
    OSError,
    ConfigException,
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
)
_EvidenceStatus = Literal["ok", "unavailable"]


class SnapshotError(Exception):
    """A collection failure with a message suitable for the API response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _load_static_config(path: Path, configuration: client.Configuration) -> None:
    kubeconfig = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(kubeconfig, dict):
        raise ConfigException("Invalid kubeconfig.")

    for entry in kubeconfig.get("users") or []:
        user = entry["user"]
        if "exec" in user or "auth-provider" in user:
            raise ConfigException("Kubeconfig authentication plugins are disabled.")
        _resolve_file_fields(user, path.parent, "tokenFile", "client-certificate", "client-key")
    for entry in kubeconfig.get("clusters") or []:
        _resolve_file_fields(entry["cluster"], path.parent, "certificate-authority")

    # The SDK shares temporary files for inline TLS credentials between clients.
    with _STATIC_CONFIG_LOCK:
        config.load_kube_config_from_dict(
            kubeconfig,
            context=os.environ.get("KIC_K8S_CONTEXT") or None,
            client_configuration=configuration,
            persist_config=False,
        )


def _resolve_file_fields(values: dict, directory: Path, *keys: str) -> None:
    # The SDK's dict loader has no base directory for relative credential paths.
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value:
            values[key] = str(directory / value)


def _create_api_client() -> client.ApiClient:
    try:
        configuration = client.Configuration(host="")
        configuration.retries = False
        if path := os.environ.get("KIC_K8S_KUBECONFIG"):
            _load_static_config(Path(path).expanduser().absolute(), configuration)
        else:
            config.load_incluster_config(client_configuration=configuration)
        endpoint = urlsplit(configuration.host)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            raise ConfigException("Missing or invalid Kubernetes API endpoint.")
        return client.ApiClient(configuration=configuration)
    except _API_ERRORS + (yaml.YAMLError, RuntimeError):
        pass
    raise SnapshotError(503, "Kubernetes client configuration is unavailable.")


def _pod_read_error(error: Exception) -> SnapshotError:
    if isinstance(error, ApiException):
        if error.status == 403:
            return SnapshotError(403, "Kubernetes denied access to the Pod.")
        if error.status == 404:
            return SnapshotError(404, "The Pod was not found.")
        if error.status in {408, 504}:
            return SnapshotError(504, "The Kubernetes Pod request timed out.")

    transport_error = error.reason if isinstance(error, MaxRetryError) else error
    if isinstance(transport_error, (TimeoutError, UrllibTimeoutError)) and not isinstance(
        transport_error, NewConnectionError
    ):
        return SnapshotError(504, "The Kubernetes Pod request timed out.")
    return SnapshotError(502, "The Kubernetes Pod request failed.")


def _read_pod(api: client.CoreV1Api, lookup: PodLookup) -> client.V1Pod:
    try:
        pod = api.read_namespaced_pod(
            name=lookup.pod_name, namespace=lookup.namespace, _request_timeout=_REQUEST_TIMEOUT
        )
        if (
            pod is not None
            and pod.metadata is not None
            and pod.metadata.name == lookup.pod_name
            and pod.metadata.namespace == lookup.namespace
            and pod.spec is not None
            and isinstance(pod.spec.containers, list)
        ):
            return pod
    except _API_ERRORS as error:
        failure = _pod_read_error(error)
    else:
        failure = SnapshotError(502, "Kubernetes returned an invalid Pod response.")
    raise failure


def _select_container(
    pod: client.V1Pod, requested: str | None
) -> tuple[str, str, client.V1ContainerStatus | None]:
    regular = pod.spec.containers
    init = pod.spec.init_containers or []
    if requested is None:
        if len(regular) != 1:
            raise SnapshotError(422, "Specify a container name for this Pod.")
        requested = regular[0].name

    if any(container.name == requested for container in regular):
        statuses = pod.status.container_statuses if pod.status else None
        field_path = f"spec.containers{{{requested}}}"
    elif any(container.name == requested for container in init):
        statuses = pod.status.init_container_statuses if pod.status else None
        field_path = f"spec.initContainers{{{requested}}}"
    else:
        raise SnapshotError(422, "The requested container is not in this Pod.")

    status = next((item for item in statuses or [] if item.name == requested), None)
    return requested, field_path, status


def _bounded_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    return redact_text(value).strip()[:limit] or None


def _collect_events(
    api: client.CoreV1Api, lookup: PodLookup, uid: str | None, field_path: str
) -> tuple[list[PodEvent], _EvidenceStatus]:
    if not isinstance(uid, str) or not _SELECTOR_UID.fullmatch(uid):
        return [], "unavailable"

    try:
        response = api.list_namespaced_event(
            namespace=lookup.namespace,
            field_selector=f"involvedObject.uid={uid},involvedObject.namespace={lookup.namespace}",
            limit=_EVENT_LIMIT,
            _request_timeout=_REQUEST_TIMEOUT,
        )
    except _API_ERRORS:
        return [], "unavailable"
    items = getattr(response, "items", None)
    if not isinstance(items, list):
        return [], "unavailable"

    events = []
    for event in items[:_EVENT_LIMIT]:
        involved = getattr(event, "involved_object", None)
        metadata = getattr(event, "metadata", None)
        if (
            involved is None
            or involved.uid != uid
            or involved.namespace != lookup.namespace
            or involved.kind not in {None, "Pod"}
            or involved.name not in {None, lookup.pod_name}
            or involved.field_path not in {None, "", field_path}
            or (metadata and metadata.namespace not in {None, lookup.namespace})
        ):
            continue
        reason = _bounded_text(event.reason, 128)
        message = _bounded_text(event.message, 2048)
        if reason or message:
            events.append(PodEvent(reason=reason, message=message))
    return events, "ok"


def _collect_logs(
    api: client.CoreV1Api, lookup: PodLookup, container_name: str, previous: bool
) -> tuple[list[str], _EvidenceStatus]:
    try:
        response = api.read_namespaced_pod_log(
            name=lookup.pod_name,
            namespace=lookup.namespace,
            container=container_name,
            previous=previous,
            tail_lines=_LOG_LINES,
            limit_bytes=_LOG_BYTES,
            _preload_content=False,
            _request_timeout=_REQUEST_TIMEOUT,
        )
        try:
            encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
            if encoding != "identity":
                return [], "unavailable"
            content = response.read(_LOG_BYTES, decode_content=False)
        finally:
            try:
                response.close()
            finally:
                response.release_conn()
    except _API_ERRORS:
        return [], "unavailable"
    if not isinstance(content, bytes):
        return [], "unavailable"

    # Redact the full excerpt before splitting lines so multiline secrets are masked.
    text = content[:_LOG_BYTES].decode("utf-8", errors="replace")
    bounded = redact_text(text).encode("utf-8", errors="replace")[:_LOG_BYTES]
    lines = bounded.decode("utf-8", errors="ignore").splitlines()[-_LOG_LINES:]
    return [line.strip()[:4096] for line in lines if line.strip()], "ok"


def collect_snapshot(lookup: PodLookup) -> tuple[IncidentInput, KubernetesDetails]:
    namespace = os.environ.get("KIC_K8S_NAMESPACE")
    if not namespace:
        raise SnapshotError(503, "Kubernetes collection is not configured.")
    if lookup.namespace != namespace:
        raise SnapshotError(403, "Kubernetes collection is disabled for this namespace.")

    api_client = _create_api_client()
    try:
        api = client.CoreV1Api(api_client)
        pod = _read_pod(api, lookup)
        name, field_path, selected = _select_container(pod, lookup.container_name)
        current = selected.state if selected else None
        waiting = current.waiting if current else None
        terminated = current.terminated if current else None
        reason = _bounded_text(waiting.reason if waiting else None, 128) or _bounded_text(
            terminated.reason if terminated else None, 128
        )
        phase = _bounded_text(pod.status.phase if pod.status else None, 128)
        status = reason or phase or "Unknown"
        restarted = selected is not None and selected.restart_count > 0
        crash_loop = reason == "CrashLoopBackOff"
        last = selected.last_state.terminated if selected and selected.last_state else None
        last_oom = last is not None and last.reason == "OOMKilled"
        recovered_oom = (
            restarted
            and current is not None
            and current.running is not None
            and waiting is None
            and terminated is None
            and last_oom
        )
        container_reason = "OOMKilled" if recovered_oom or (crash_loop and last_oom) else reason
        previous = restarted and (crash_loop or recovered_oom)

        uid = pod.metadata.uid
        events, events_status = _collect_events(api, lookup, uid, field_path)
        logs, logs_status = _collect_logs(api, lookup, name, previous)
    finally:
        api_client.close()

    return (
        IncidentInput(
            namespace=lookup.namespace,
            pod_name=lookup.pod_name,
            status=status,
            container_reason=container_reason,
            events=events,
            logs=logs,
        ),
        KubernetesDetails(
            namespace=lookup.namespace,
            pod_name=lookup.pod_name,
            container_name=name,
            events_status=events_status,
            logs_status=logs_status,
        ),
    )
