"""Read-only guard for all Kubernetes API traffic (ADR-0005).

Every request the daemon sends goes through GuardedRESTClient.request, the lowest
level of the kubernetes Python client, so a check here cannot be bypassed by a
higher-level API call. Allowed:

  * GET (includes list, watch, logs, discovery), except anything under secrets.
  * PATCH/PUT only with dryRun=All              -- named exception, never a model tool.
  * POST only to subjectaccessreviews           -- named exception, never a model tool.

Everything else is rejected before any bytes leave the process.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from kubernetes.client import rest

# Searched, not anchored: the server URL may carry a path prefix (e.g. a Rancher proxy).
SAR_PATH = re.compile(r"/apis/authorization\.k8s\.io/v1/subjectaccessreviews/?$")
# Matches the secrets collection or a named secret, core or namespaced.
SECRETS_PATH = re.compile(r"/api/v1/(namespaces/[^/]+/)?secrets(/|$)")


class GuardViolation(PermissionError):
    """Raised when a request would violate the read-only contract."""


def _query(url: str, query_params) -> dict[str, list[str]]:
    params: dict[str, list[str]] = {}
    pairs = list(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    if query_params:
        items = query_params.items() if isinstance(query_params, dict) else query_params
        pairs.extend((str(k), str(v)) for k, v in items)
    for k, v in pairs:
        params.setdefault(k, []).append(v)
    return params


def check(method: str, url: str, query_params=None) -> None:
    """Raises GuardViolation unless the request is permitted."""
    method = method.upper()
    path = urlsplit(url).path
    if SECRETS_PATH.search(path):
        raise GuardViolation(f"secrets are never read: {method} {path}")
    if method == "GET":
        return
    if method in ("PATCH", "PUT"):
        dry = _query(url, query_params).get("dryRun", [])
        if dry and all(v == "All" for v in dry):
            return
        raise GuardViolation(f"{method} {path} permitted only with dryRun=All")
    if method == "POST" and SAR_PATH.search(path):
        return
    raise GuardViolation(f"{method} {path} is not permitted")


class GuardedRESTClient(rest.RESTClientObject):
    """RESTClientObject that enforces check() on every request."""

    def request(self, method, url, query_params=None, *args, **kwargs):
        check(method, url, query_params)
        return super().request(method, url, query_params, *args, **kwargs)


def install(api_client) -> None:
    """Replaces an ApiClient's transport with the guarded one."""
    api_client.rest_client = GuardedRESTClient(api_client.configuration)
