import inspect

import pytest

from k9sai import backend, tools
from k9sai.backend import build_config
from k9sai.config import Backend
from k9sai.evidence import Evidence
from k9sai.tools import TOOL_NAMES, make_tools


def test_tool_module_cannot_reach_write_exceptions():
    """ADR-0005: the dry-run and SAR exceptions are never model tools."""
    src = inspect.getsource(tools)
    for forbidden in (
        "create_",
        "patch_",
        "replace_",
        "delete_",
        "dry_run",
        "dryRun",
        "subject_access",
        "SubjectAccessReview(",
    ):
        assert forbidden not in src, forbidden


def test_every_tool_call_is_a_get(cluster):
    ev = Evidence()
    fns = {t.__name__: t for t in make_tools(cluster.kube, "shop", ev, max_calls=10)}
    assert tuple(fns) == TOOL_NAMES
    fns["get_resource"]("pod", "web-1")
    fns["get_resource"]("configmap", "web-cfg")
    fns["get_events"]("Pod", "web-1")
    fns["get_logs"]("web-1", "web", True, 50)
    fns["get_node_conditions"]("node-1")
    fns["rbac_lookup"]("default")
    assert cluster.requests
    assert {m for m, _ in cluster.requests} == {"GET"}


def test_tool_output_is_redacted_and_numbered(cluster):
    ev = Evidence()
    get_resource = make_tools(cluster.kube, "shop", ev, 3)[0]
    out = get_resource("pod", "web-1")
    assert "hunter2" not in out and "E1:" in out
    cm = get_resource("configmap", "web-cfg")
    assert "prod" not in cm and "abc" not in cm and "mode" in cm  # keys only


def test_tool_budget_is_enforced(cluster):
    get_events = make_tools(cluster.kube, "shop", Evidence(), max_calls=2)[1]
    get_events("Pod", "web-1")
    get_events("Pod", "web-1")
    assert "budget exhausted" in get_events("Pod", "web-1")
    assert len(cluster.requests) == 2


def test_tool_signatures_survive_wrapping(cluster):
    get_logs = make_tools(cluster.kube, "shop", Evidence(), 3)[2]
    assert list(inspect.signature(get_logs).parameters) == ["pod", "container", "previous", "tail"]
    assert "previous" in get_logs.__doc__


def test_unsupported_kind_is_refused(cluster):
    get_resource = make_tools(cluster.kube, "shop", Evidence(), 3)[0]
    assert get_resource("secret", "x").startswith("error:")
    assert cluster.requests == []


@pytest.mark.parametrize(
    "b",
    [
        Backend("o", "openai", model="m", base_url="http://localhost:11434/v1"),
        Backend("l", "litert", model_path="/tmp/m.litertlm"),
    ],
)
def test_sdk_lockdown(b):
    """.lightweight() enables run_command/view_file/... by default; we must not."""

    def get_x(name: str) -> str:
        """doc"""
        return name

    cfg = build_config(b, "sys", [get_x])
    assert cfg.capabilities.enabled_tools == []
    assert cfg.capabilities.enable_subagents is False
    assert cfg.workspaces == []


def test_lockdown_detects_sdk_regression(monkeypatch):
    from google.antigravity import LocalOpenAIAgentConfig, types

    real = LocalOpenAIAgentConfig.lightweight

    def reopened(self):
        cfg = real(self)
        return cfg.model_copy(
            update={
                "capabilities": types.CapabilitiesConfig(
                    enabled_tools=types.BuiltinTools.minimal(), enable_subagents=False
                )
            }
        )

    monkeypatch.setattr(LocalOpenAIAgentConfig, "lightweight", reopened)
    with pytest.raises(backend.UnsafeConfigError):
        build_config(Backend("o", "openai", model="m", base_url="http://localhost:1/v1"), "s", [])
