"""Best-effort credential redaction for Kubernetes observations."""

import re

_MASK = "[REDACTED]"
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
    r".*?(?:-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_AUTH = re.compile(
    r"""(\bauthorization["']?[ \t]*[:=][ \t]*["']?(?:bearer|basic)[ \t]+|\bbearer[ \t]+)"""
    r"""[^\s"',;]+""",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(
    r"((?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://)[^/\s:@]+:[^/\s@]+@",
    re.IGNORECASE,
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_ASSIGNMENT = re.compile(
    r"""((?<![a-z0-9_-])(?:[a-z0-9_-]+[_-])?"""
    r"""(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key)"""
    r"""["']?[ \t]*[:=][ \t]*)"""
    r"""(?:"(?:\\.|[^"\\\r\n])*"?|'(?:\\.|[^'\\\r\n])*'?|[^\s,;&"'{}]+)""",
    re.IGNORECASE,
)


def redact_text(text: str) -> str:
    """Mask common credential formats while preserving useful diagnostic terms."""
    text = _PRIVATE_KEY.sub(_MASK, text)
    text = _URL_CREDENTIALS.sub(r"\g<1>[REDACTED]@", text)
    text = _AUTH.sub(r"\g<1>[REDACTED]", text)
    text = _JWT.sub(_MASK, text)
    return _ASSIGNMENT.sub(r"\g<1>[REDACTED]", text)
