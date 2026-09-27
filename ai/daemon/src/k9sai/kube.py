"""Guarded Kubernetes API access. The only way the daemon reaches a cluster."""

from __future__ import annotations

import json
from dataclasses import dataclass

from kubernetes import client, config

from k9sai import guard


@dataclass
class Kube:
    """Typed API handles sharing one guarded ApiClient."""

    api: client.ApiClient
    core: client.CoreV1Api
    apps: client.AppsV1Api
    rbac: client.RbacAuthorizationV1Api
    ext: client.ApiextensionsV1Api


def connect(context: str | None, kubeconfig: str | None = None) -> Kube:
    """kubeconfig comes from the calling k9s session ($KUBECONFIG), not the daemon's env."""
    cfg = client.Configuration()
    config.load_kube_config(config_file=kubeconfig or None, context=context or None, client_configuration=cfg)
    api = client.ApiClient(cfg)
    guard.install(api)
    return Kube(
        api=api,
        core=client.CoreV1Api(api),
        apps=client.AppsV1Api(api),
        rbac=client.RbacAuthorizationV1Api(api),
        ext=client.ApiextensionsV1Api(api),
    )


def get_json(k: Kube, path: str) -> dict:
    """GET an arbitrary API path (discovery) through the guard, parsed as JSON.

    Raw response + json.loads: the generated deserializer's call_api signature
    changes between client majors, the wire format does not.
    """
    resp = k.api.call_api(
        path, "GET", auth_settings=["BearerToken"], _preload_content=False, _return_http_data_only=True
    )
    try:
        return json.loads(resp.data or b"{}")
    finally:
        if hasattr(resp, "release_conn"):
            resp.release_conn()


def read_log(k: Kube, pod: str, ns: str, **kwargs) -> str:
    """Pod logs as text.

    The generated client deserializes this endpoint into the repr of a bytes object
    (``"b'line1\\nline2'"``), so read the raw response and decode it ourselves.
    """
    resp = k.core.read_namespaced_pod_log(pod, ns, _preload_content=False, **kwargs)
    try:
        return resp.data.decode("utf-8", errors="replace")
    finally:
        if hasattr(resp, "release_conn"):
            resp.release_conn()
