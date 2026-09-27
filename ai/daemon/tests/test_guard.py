import pytest
from kubernetes import client

from k9sai import guard
from k9sai.guard import GuardViolation, check

H = "https://10.0.0.1:6443"


@pytest.mark.parametrize(
    "url",
    [
        f"{H}/api/v1/namespaces/a/pods",
        f"{H}/api/v1/namespaces/a/pods/p/log?previous=true",
        f"{H}/apis/apps/v1/namespaces/a/deployments?watch=true",
        f"{H}/apis",
        f"{H}/api/v1/nodes/n1",
        f"{H}/api/v1/namespaces/a/secretsauce",  # not the secrets resource
    ],
)
def test_reads_allowed(url):
    check("GET", url)


@pytest.mark.parametrize(
    "url",
    [
        f"{H}/api/v1/secrets",
        f"{H}/api/v1/namespaces/a/secrets",
        f"{H}/api/v1/namespaces/a/secrets/db-creds",
        "https://rancher.example/k8s/clusters/c-1/api/v1/namespaces/a/secrets/x",
    ],
)
def test_secrets_never_read(url):
    with pytest.raises(GuardViolation):
        check("GET", url)


@pytest.mark.parametrize("method", ["POST", "DELETE", "PATCH", "PUT", "OPTIONS", "HEAD"])
def test_writes_rejected(method):
    with pytest.raises(GuardViolation):
        check(method, f"{H}/api/v1/namespaces/a/pods/p")


@pytest.mark.parametrize("method", ["PATCH", "PUT"])
def test_dry_run_exception(method):
    check(method, f"{H}/apis/apps/v1/namespaces/a/deployments/d", [("dryRun", "All")])
    check(method, f"{H}/apis/apps/v1/namespaces/a/deployments/d?dryRun=All")
    with pytest.raises(GuardViolation):
        check(method, f"{H}/apis/apps/v1/namespaces/a/deployments/d", [("dryRun", "None")])
    with pytest.raises(GuardViolation):  # mixed values must not slip through
        check(method, f"{H}/x?dryRun=All", [("dryRun", "")])


def test_sar_exception_only_for_sar():
    check("POST", f"{H}/apis/authorization.k8s.io/v1/subjectaccessreviews")
    for url in (
        f"{H}/apis/authorization.k8s.io/v1/selfsubjectrulesreviews",
        f"{H}/apis/authorization.k8s.io/v1/subjectaccessreviews/../../../api/v1/pods",
        f"{H}/api/v1/namespaces/a/pods",
    ):
        with pytest.raises(GuardViolation):
            check("POST", url)


def test_installed_client_blocks_before_network():
    cfg = client.Configuration()
    cfg.host = "https://127.0.0.1:1"  # nothing listens; a leak would be a connection error
    api = client.ApiClient(cfg)
    guard.install(api)
    with pytest.raises(GuardViolation):
        client.CoreV1Api(api).delete_namespaced_pod("p", "a")
    with pytest.raises(GuardViolation):
        client.CoreV1Api(api).read_namespaced_secret("s", "a")
