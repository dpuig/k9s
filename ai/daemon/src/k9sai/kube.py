"""Guarded Kubernetes API access. The only way the daemon reaches a cluster."""

from __future__ import annotations

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
    """GET an arbitrary API path (discovery, owner lookups) through the guard."""
    data, _, _ = k.api.call_api(
        path, "GET", response_type="object", auth_settings=["BearerToken"], _return_http_data_only=False
    )
    return data or {}
