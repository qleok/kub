"""Optional HTTP adapter that lets a model prioritize existing diagnosis steps."""

import asyncio
import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .schemas import DiagnosisReport

_INVALID_PRIORITIES = "The model returned invalid step priorities."
_INVALID_URL = "The model API base URL is invalid."
_MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = field(repr=False)
    model: str
    api_key: str | None = field(default=None, repr=False)


class ModelUnavailable(Exception):
    """The optional model could not provide priorities."""


class RetryableModelError(ModelUnavailable):
    """A transient failure that the graph may retry."""


class ModelTimeout(RetryableModelError):
    """The model request exceeded a timeout."""


class InvalidModelResponse(ModelUnavailable):
    """The model response did not contain valid priorities."""


def read_model_config() -> ModelConfig | None:
    base_url = os.getenv("KIC_LLM_BASE_URL", "").strip()
    model = os.getenv("KIC_LLM_MODEL", "").strip()
    if not base_url or not model:
        return None
    api_key = os.getenv("KIC_LLM_API_KEY", "").strip() or None
    return ModelConfig(base_url=base_url, model=model, api_key=api_key)


def _chat_endpoint(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.port != 0
            and not any(char.isspace() or char in "?#\\" for char in base_url)
        ):
            return f"{base_url.rstrip('/')}/chat/completions"
    except ValueError:
        pass
    raise ModelUnavailable(_INVALID_URL)


def _selected_steps(payload: bytearray, steps: tuple[str, ...]) -> list[str]:
    try:
        completion = json.loads(payload)["choices"][0]["message"]["content"]
        result = json.loads(completion)
    except (IndexError, KeyError, RecursionError, TypeError, ValueError):
        raise InvalidModelResponse(_INVALID_PRIORITIES) from None

    if not isinstance(result, dict) or result.keys() != {"priority_steps"}:
        raise InvalidModelResponse(_INVALID_PRIORITIES)
    indices = result["priority_steps"]
    if (
        not isinstance(indices, list)
        or not 1 <= len(indices) <= 3
        or any(type(index) is not int or not 1 <= index <= len(steps) for index in indices)
        or len(set(indices)) != len(indices)
    ):
        raise InvalidModelResponse(_INVALID_PRIORITIES)
    return [steps[index - 1] for index in indices]


async def prioritize_steps(config: ModelConfig, report: DiagnosisReport) -> list[str]:
    endpoint = _chat_endpoint(config.base_url)
    steps = tuple(report.recommended_steps)
    selection = {
        "failure_type": report.failure_type,
        "severity": report.severity,
        "steps": [{"index": index, "text": step} for index, step in enumerate(steps, start=1)],
    }
    body = {
        "model": config.model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Prioritize the most useful next checks for the given Kubernetes failure. "
                    'Return only a JSON object such as {"priority_steps":[1,2]}. '
                    "Select one to three distinct step indices in priority order."
                ),
            },
            {"role": "user", "content": json.dumps(selection)},
        ],
    }
    headers = {"Accept-Encoding": "identity"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    payload = bytearray()
    try:
        async with (
            asyncio.timeout(10.0),
            httpx.AsyncClient(
                timeout=httpx.Timeout(5.0, connect=3.0), follow_redirects=False, trust_env=False
            ) as client,
            client.stream("POST", endpoint, json=body, headers=headers) as response,
        ):
            if response.status_code in {408, 429} or response.is_server_error:
                raise RetryableModelError("The model service is temporarily unavailable.")
            if not response.is_success:
                raise ModelUnavailable("The model service rejected the request.")
            if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                raise InvalidModelResponse(_INVALID_PRIORITIES)
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise InvalidModelResponse(_INVALID_PRIORITIES)
                payload.extend(chunk)
    except (TimeoutError, httpx.TimeoutException):
        raise ModelTimeout("The model request timed out.") from None
    except httpx.TransportError:
        raise RetryableModelError("The model service could not be reached.") from None
    except httpx.DecodingError:
        raise InvalidModelResponse(_INVALID_PRIORITIES) from None
    except (httpx.InvalidURL, UnicodeError):
        raise ModelUnavailable("The model connection settings are invalid.") from None

    return _selected_steps(payload, steps)
