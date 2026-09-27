"""Scrubs credentials from evidence before it reaches any model (ADR-0004).

This is the single choke point: the context builder and every tool result call
redact(). Emails are deliberately left intact. Secret objects never get here --
the guard refuses to read them at all.
"""

from __future__ import annotations

import re

_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)

_SENSITIVE_KEY = (
    r"[\w.-]*(?:passw(?:or)?d|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|credentials?|auth)[\w.-]*"
)

_SENSITIVE_NAME = re.compile(rf"(?i)^{_SENSITIVE_KEY}$")

# Order matters: specific token shapes first, then the generic key=value rule.
_RULES: list[tuple[re.Pattern[str], str]] = [
    # Kubernetes env entries, where the sensitive name and its value are separate fields:
    #   - name: DB_PASSWORD            {"name": "DB_PASSWORD", "value": "x"}
    #     value: hunter2
    (
        re.compile(
            rf"(?i)(name:\s*[\"']?{_SENSITIVE_KEY}[\"']?[ \t]*\r?\n[ \t]*value:[ \t]*)"
            r"(\"[^\"\n]*\"|'[^'\n]*'|[^\n]+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(rf"(?i)(\"name\"\s*:\s*\"{_SENSITIVE_KEY}\"\s*,\s*\"value\"\s*:\s*)\"[^\"]*\""),
        r'\1"[REDACTED]"',
    ),
    (
        re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(bearer|basic|token)\s+[^\s\"',]+"),
        r"\1\2 [REDACTED]",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED:jwt]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    (
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"),
        "[REDACTED:github-token]",
    ),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack-token]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED:google-key]"),
    (re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"), "[REDACTED:stripe-key]"),
    # scheme://user:password@host -- keep scheme and user, drop the password.
    (re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+):[^/\s@]+@"), r"\1:[REDACTED]@"),
    # KEY=value, key: value, "key": "value" where the key name looks sensitive.
    (
        re.compile(rf"(?i)(\b{_SENSITIVE_KEY}[\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}}]+)"),
        r"\1[REDACTED]",
    ),
]

# Values that are clearly not secrets; keeps describe output readable.
_BENIGN = re.compile(r"^(?:\"\"|''|<none>|<unset>|true|false|\d{1,6}|\[REDACTED[^\]]*\])$")


def _kv(match: re.Match[str]) -> str:
    return match.group(0) if _BENIGN.match(match.group(2)) else f"{match.group(1)}[REDACTED]"


def redact(text: str) -> str:
    """Returns text with credentials replaced by [REDACTED] markers."""
    text = _PEM.sub("[REDACTED:private-key]", text)
    for pattern, repl in _RULES[:-1]:
        text = pattern.sub(repl, text)
    return _RULES[-1][0].sub(_kv, text)


def redact_obj(obj):
    """Structured pass for API objects before they are rendered as text.

    Redacts `value` of any {name, value} pair whose name looks sensitive (env vars),
    and the value of any mapping key that looks sensitive. Returns a new object.
    """
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    sensitive_pair = isinstance(obj.get("name"), str) and _SENSITIVE_NAME.match(obj["name"])
    for k, v in obj.items():
        if (k == "value" and sensitive_pair) or (
            isinstance(v, (str, int))
            and not isinstance(v, bool)
            and _SENSITIVE_NAME.match(str(k))
            and not _BENIGN.match(str(v))
        ):
            out[k] = "[REDACTED]"
        else:
            out[k] = redact_obj(v)
    return out
