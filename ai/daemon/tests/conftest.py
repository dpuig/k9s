"""A fake cluster behind the real guarded client: every request goes through
guard.check, is recorded, and is answered from canned JSON."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import pytest
from kubernetes import client
from kubernetes.client import rest

from k9sai import guard
from k9sai.kube import Kube

NOW = "2026-09-26T20:00:00Z"


def _pod(name="web-1"):
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "shop",
            "labels": {"app": "web"},
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "web-7d9",
                    "uid": "u",
                    "controller": True,
                }
            ],
        },
        "spec": {
            "nodeName": "node-1",
            "containers": [
                {
                    "name": "web",
                    "image": "shop/web:1.4",
                    "resources": {"limits": {"memory": "64Mi"}},
                    "env": [
                        {"name": "DB_PASSWORD", "value": "hunter2"},
                        {
                            "name": "MODE",
                            "valueFrom": {"configMapKeyRef": {"name": "web-cfg", "key": "mode"}},
                        },
                    ],
                }
            ],
        },
        "status": {
            "phase": "Running",
            "qosClass": "Burstable",
            "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady"}],
            "containerStatuses": [
                {
                    "name": "web",
                    "image": "shop/web:1.4",
                    "imageID": "x",
                    "ready": False,
                    "restartCount": 5,
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": NOW}},
                }
            ],
        },
    }


ROUTES = [
    (
        r"/api/v1/namespaces$",
        "GET",
        {"items": [{"metadata": {"name": "shop"}}, {"metadata": {"name": "payments"}}]},
    ),
    (
        r"/api/v1$",
        "GET",
        {"resources": [{"name": "pods", "shortNames": ["po"]}, {"name": "pods/log"}, {"name": "services"}]},
    ),
    (r"/apis$", "GET", {"groups": [{"preferredVersion": {"groupVersion": "apps/v1"}}]}),
    (r"/apis/apps/v1$", "GET", {"resources": [{"name": "deployments", "shortNames": ["deploy"]}]}),
    (
        r"/api/v1/namespaces/shop/pods/web-1/log",
        "GET",
        "2026-09-26T19:59:00.123456789Z starting web\n2026-09-26T19:59:01Z allocating cache 900Mi\n",
    ),
    (r"/api/v1/namespaces/shop/pods/web-1$", "GET", _pod()),
    (r"/api/v1/namespaces/shop/pods$", "GET", {"kind": "PodList", "items": [_pod()]}),
    (
        r"/api/v1/namespaces/shop/events$",
        "GET",
        {
            "kind": "EventList",
            "items": [
                {
                    "metadata": {"name": "e1"},
                    "involvedObject": {"kind": "Pod", "name": "web-1"},
                    "type": "Warning",
                    "reason": "BackOff",
                    "count": 12,
                    "lastTimestamp": NOW,
                    "message": "Back-off restarting failed container web",
                }
            ],
        },
    ),
    (
        r"/apis/apps/v1/namespaces/shop/replicasets/web-7d9$",
        "GET",
        {
            "metadata": {
                "name": "web-7d9",
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": "web",
                        "uid": "d",
                        "controller": True,
                    }
                ],
            },
            "spec": {
                "selector": {"matchLabels": {"app": "web"}},
                "template": {"spec": {"containers": [{"name": "web", "image": "shop/web:1.4"}]}},
            },
            "status": {"replicas": 1},
        },
    ),
    (
        r"/apis/apps/v1/namespaces/shop/deployments/web$",
        "GET",
        {
            "metadata": {"name": "web", "annotations": {"deployment.kubernetes.io/revision": "4"}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "web"}},
                "template": {"spec": {"containers": [{"name": "web", "image": "shop/web:1.4"}]}},
            },
            "status": {"replicas": 1, "readyReplicas": 0},
        },
    ),
    (
        r"/api/v1/nodes/node-1$",
        "GET",
        {
            "metadata": {"name": "node-1"},
            "spec": {},
            "status": {
                "allocatable": {"cpu": "4", "memory": "8Gi"},
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        },
    ),
    (
        r"/api/v1/namespaces/shop/configmaps/web-cfg$",
        "GET",
        {"metadata": {"name": "web-cfg"}, "data": {"mode": "prod", "token": "abc"}},
    ),
    (
        r"/apis/rbac.authorization.k8s.io/v1/namespaces/shop/rolebindings$",
        "GET",
        {
            "items": [
                {
                    "metadata": {"name": "rb"},
                    "roleRef": {"apiGroup": "", "kind": "Role", "name": "reader"},
                    "subjects": [{"kind": "ServiceAccount", "name": "default"}],
                }
            ]
        },
    ),
    (r"/apis/rbac.authorization.k8s.io/v1/clusterrolebindings$", "GET", {"items": []}),
]


class _Resp:
    """Stands in for urllib3.HTTPResponse."""

    def __init__(self, status, body):
        self.status, self.reason = status, "OK" if status == 200 else "Not Found"
        self.data = body.encode()
        self.headers = {"content-type": "application/json"}

    def getheaders(self):
        return self.headers

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def release_conn(self):
        pass


class FakeCluster:
    def __init__(self):
        self.requests: list[tuple[str, str]] = []

    def respond(self, method, url, preload=True):
        path = urlsplit(url).path
        self.requests.append((method, path))
        for pattern, m, body in ROUTES:
            if m == method and re.search(pattern, path):
                text = body if isinstance(body, str) else json.dumps(body)
                # Like the real client: raw urllib3 response unless preloading.
                return rest.RESTResponse(_Resp(200, text)) if preload else _Resp(200, text)
        raise rest.ApiException(status=404, reason="Not Found")


@pytest.fixture
def cluster(monkeypatch):
    fake = FakeCluster()

    def request(
        self,
        method,
        url,
        query_params=None,
        headers=None,
        body=None,
        post_params=None,
        _preload_content=True,
        _request_timeout=None,
    ):
        return fake.respond(method, url, _preload_content)

    # Patch the *network* layer only; GuardedRESTClient.request still runs first.
    monkeypatch.setattr(rest.RESTClientObject, "request", request)
    cfg = client.Configuration()
    cfg.host = "https://fake:6443"
    api = client.ApiClient(cfg)
    guard.install(api)
    fake.kube = Kube(
        api=api,
        core=client.CoreV1Api(api),
        apps=client.AppsV1Api(api),
        rbac=client.RbacAuthorizationV1Api(api),
        ext=client.ApiextensionsV1Api(api),
    )
    return fake
