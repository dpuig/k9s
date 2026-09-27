"""Read-only tools the model may call (ADR-0003).

Tools are closures bound to one request: one namespace, one Evidence, one call
budget. They only use GET-backed client calls; the guard rejects anything else
anyway. Output is redacted and numbered by Evidence.append before the model sees it.

The dry-run and SubjectAccessReview exceptions of ADR-0005 are deliberately NOT
reachable from here; test_tools asserts it.
"""

from __future__ import annotations

import functools
from collections.abc import Callable

import yaml
from kubernetes.client import ApiException

from k9sai import collect
from k9sai.evidence import Evidence
from k9sai.kube import Kube
from k9sai.redact import redact_obj

TOOL_NAMES = ("get_resource", "get_events", "get_logs", "get_node_conditions", "rbac_lookup")

_READERS = {
    "pod": "read_namespaced_pod",
    "service": "read_namespaced_service",
    "endpoints": "read_namespaced_endpoints",
    "configmap": "read_namespaced_config_map",
    "persistentvolumeclaim": "read_namespaced_persistent_volume_claim",
    "serviceaccount": "read_namespaced_service_account",
    "deployment": "read_namespaced_deployment",
    "replicaset": "read_namespaced_replica_set",
    "statefulset": "read_namespaced_stateful_set",
    "daemonset": "read_namespaced_daemon_set",
}
_MAX_CHARS = 6000


def _strip(obj: dict) -> dict:
    meta = obj.get("metadata") or {}
    for k in ("managed_fields", "uid", "resource_version", "generation", "self_link"):
        meta.pop(k, None)
    ann = meta.get("annotations") or {}
    ann.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return obj


def make_tools(k: Kube, ns: str, ev: Evidence, max_calls: int) -> list[Callable[..., str]]:
    calls = {"n": 0}

    def budget(fn: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(fn)  # keeps name, docstring and signature for the SDK schema
        def wrapped(*args, **kwargs) -> str:
            if calls["n"] >= max_calls:
                return "Tool budget exhausted. Answer with the evidence you already have."
            calls["n"] += 1
            try:
                return ev.append(f"tool {fn.__name__}", fn(*args, **kwargs))
            except ApiException as e:
                return ev.append(f"tool {fn.__name__}", f"error: {e.status} {e.reason}")
            except ValueError as e:
                return f"error: {e}"

        return wrapped

    def get_resource(kind: str, name: str) -> str:
        """Returns spec and status of one object in the current namespace.
        kind is one of: pod, service, endpoints, configmap, persistentvolumeclaim,
        serviceaccount, deployment, replicaset, statefulset, daemonset.
        For configmaps only the key names are returned, never values."""
        reader = _READERS.get(kind.lower())
        if reader is None:
            raise ValueError(f"unsupported kind {kind!r}")
        api = k.core if hasattr(k.core, reader) else k.apps
        obj = k.api.sanitize_for_serialization(getattr(api, reader)(name, ns))
        if kind.lower() == "configmap":
            obj = {"metadata": obj.get("metadata"), "data_keys": sorted(obj.get("data") or {})}
        return yaml.safe_dump(redact_obj(_strip(obj)), sort_keys=False)[:_MAX_CHARS]

    def get_events(kind: str, name: str) -> str:
        """Returns events of the last hour for one object in the current namespace.
        kind is the Kubernetes kind, e.g. Pod, Deployment, PersistentVolumeClaim."""
        return "\n".join(collect.events_for(k, ns, kind, name)) or "no events"

    def get_logs(pod: str, container: str = "", previous: bool = False, tail: int = 100) -> str:
        """Returns the last `tail` log lines (max 300) of a container in the current
        namespace. previous=true reads the previous (crashed) instance."""
        return (
            k.core.read_namespaced_pod_log(
                pod, ns, container=container or None, previous=previous, tail_lines=min(tail, 300)
            )
            or "<empty>"
        )

    def get_node_conditions(node: str) -> str:
        """Returns allocatable resources, abnormal conditions and taints of a node."""
        return "\n".join(collect.node_conditions(k, node))

    def rbac_lookup(service_account: str) -> str:
        """Lists RoleBindings in the current namespace and ClusterRoleBindings that
        grant roles to the given service account of the current namespace."""
        out = []

        def match(b) -> bool:
            return any(
                s.kind == "ServiceAccount" and s.name == service_account and (s.namespace or ns) == ns
                for s in b.subjects or []
            )

        for b in k.rbac.list_namespaced_role_binding(ns).items:
            if match(b):
                out.append(f"RoleBinding {b.metadata.name} -> {b.role_ref.kind}/{b.role_ref.name}")
        for b in k.rbac.list_cluster_role_binding().items:
            if match(b):
                out.append(f"ClusterRoleBinding {b.metadata.name} -> ClusterRole/{b.role_ref.name}")
        return "\n".join(out) or f"no bindings for serviceaccount {ns}/{service_account}"

    tools = [get_resource, get_events, get_logs, get_node_conditions, rbac_lookup]
    assert tuple(t.__name__ for t in tools) == TOOL_NAMES
    return [budget(t) for t in tools]
