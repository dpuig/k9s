"""Task pipelines: gather -> redact/number -> prompt -> stream -> validate.

Each task is an async generator of (event, payload) pairs that server.py writes
as SSE. Cluster calls are blocking, so they run in a worker thread.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from k9sai import citations, collect, drainlogs, prompts
from k9sai.backend import AgentBackend
from k9sai.config import Backend, Config
from k9sai.evidence import Evidence
from k9sai.kube import Kube, get_json, read_log
from k9sai.tools import make_tools

Stream = AsyncIterator[tuple[str, dict]]


@dataclass
class Request:
    task: str
    context: str = ""
    kubeconfig: str = ""
    namespace: str = "default"
    resource: str = "pods"
    name: str = ""
    container: str = ""
    question: str = ""
    budget: int = 0
    tools: bool = True
    thoughts: bool = False
    json: bool = False
    fixture: dict = field(default_factory=dict)  # replay a capture instead of the cluster


@dataclass
class Deps:
    cfg: Config
    backend: Callable[[Backend], AgentBackend]
    kube: Callable[[str, str], Kube]


_HYP = re.compile(r"^\s*\d+\.\s+\*\*(?P<title>.+?)\*\*\s*\(category:\s*(?P<cat>[\w-]+)", re.M)


def parse_hypotheses(text: str) -> list[dict]:
    body = text.split("## Suggested fix")[0]
    starts = list(_HYP.finditer(body))
    out = []
    for i, m in enumerate(starts):
        chunk = body[m.start() : starts[i + 1].start() if i + 1 < len(starts) else len(body)]
        out.append({"title": m["title"], "category": m["cat"], "cites": citations.cited(chunk)})
    return out


def _target(r: Request) -> str:
    if r.task == "ask":
        return f"{r.question!r} (namespace {r.namespace})"
    return f"{r.resource}/{r.name} in namespace {r.namespace}"


TURN_BREAK = "\x00turn\x00"


def final_turn(out: list[str]) -> str:
    """Text of the last turn that produced any; earlier turns were drafts before tool calls."""
    turns = [t for t in "".join(out).split(TURN_BREAK) if t.strip()]
    return turns[-1] if turns else ""


async def _generate(
    deps: Deps, r: Request, backend: Backend, prompt: str, tools, ev: Evidence, out: list[str]
) -> Stream:
    agent = deps.backend(backend)
    async for e in agent.stream(prompts.SYSTEM, prompt, tools):
        if e.kind == "token":
            out.append(e.text)
            yield "token", {"text": e.text}
        elif e.kind == "thought" and r.thoughts:
            yield "thought", {"text": e.text}
        elif e.kind == "tool":
            out.append(TURN_BREAK)  # text before a tool call was a draft
            yield "tool", {"name": e.text}


def _meta(backend: Backend, r: Request, ev: Evidence | None = None) -> dict:
    meta = {
        "task": r.task,
        "backend": backend.name,
        "model": backend.model or backend.model_path,
        "target": _target(r),
    }
    if ev is not None:
        meta.update(evidence_lines=len(ev.numbered), dropped=ev.dropped)
    return meta


async def diagnose_evidence(deps: Deps, r: Request) -> Evidence:
    if r.fixture:
        return Evidence.from_dict(r.fixture["evidence"])
    k = await asyncio.to_thread(deps.kube, r.context, r.kubeconfig)
    return await asyncio.to_thread(collect.diagnose_evidence, k, r.namespace, r.resource, r.name)


async def diagnose(deps: Deps, r: Request) -> Stream:
    backend = deps.cfg.backend_for("diagnose")
    ev = await diagnose_evidence(deps, r)
    ev.pack(r.budget or deps.cfg.budget_tokens)
    yield "meta", _meta(backend, r, ev)
    use_tools = r.tools and not r.fixture
    tools = []
    if use_tools:
        k = await asyncio.to_thread(deps.kube, r.context, r.kubeconfig)
        tools = make_tools(k, r.namespace, ev, deps.cfg.max_tool_calls)
    prompt = prompts.diagnose(_target(r), ev.render(), deps.cfg.max_tool_calls if use_tools else 0)
    out: list[str] = []
    started = time.monotonic()
    async for item in _generate(deps, r, backend, prompt, tools, ev, out):
        yield item
    # Citations are checked over everything shown, drafts included; grading uses the final turn.
    text = "".join(out).replace(TURN_BREAK, "\n")
    if f := citations.footer(text, ev.ids):
        yield "footer", {"text": f}
    if r.json:
        yield (
            "result",
            {
                "hypotheses": parse_hypotheses(final_turn(out)),
                "invalid_citations": citations.invalid(text, ev.ids),
                "evidence_ids": len(ev.ids),
                "seconds": round(time.monotonic() - started, 1),
                "text": text,
            },
        )


async def capture(deps: Deps, r: Request) -> Stream:
    ev = await diagnose_evidence(deps, r)
    yield (
        "result",
        {
            "target": {
                "context": r.context,
                "namespace": r.namespace,
                "resource": r.resource,
                "name": r.name,
            },
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "evidence": ev.to_dict(),
        },
    )


def _log_lines(k: Kube, r: Request) -> tuple[str, list[str]]:
    pod = k.core.read_namespaced_pod(r.name, r.namespace)
    names = [c.name for c in pod.spec.containers]
    container = r.container or (pod.metadata.annotations or {}).get(
        "kubectl.kubernetes.io/default-container", names[0]
    )
    text = read_log(
        k,
        r.name,
        r.namespace,
        container=container,
        tail_lines=drainlogs.WINDOW_LINES,
        timestamps=True,
    )
    return container, text.splitlines()


async def logs(deps: Deps, r: Request) -> Stream:
    backend = deps.cfg.backend_for("logs")
    k = await asyncio.to_thread(deps.kube, r.context, r.kubeconfig)
    container, lines = await asyncio.to_thread(_log_lines, k, r)
    target = f"pod {r.namespace}/{r.name} container {container}"
    ev = drainlogs.log_evidence(lines, datetime.now(timezone.utc), target)
    ev.pack(r.budget or deps.cfg.budget_tokens)
    yield "meta", _meta(backend, r, ev)
    out: list[str] = []
    async for item in _generate(deps, r, backend, prompts.logs(target, ev.render()), [], ev, out):
        yield item
    if f := citations.footer("".join(out).replace(TURN_BREAK, "\n"), ev.ids):
        yield "footer", {"text": f}


_FACTS_TTL = 600
_facts: dict[str, tuple[float, list[str], list[str]]] = {}


def _cluster_facts(k: Kube, key: str) -> tuple[list[str], list[str]]:
    hit = _facts.get(key)
    if hit and time.monotonic() - hit[0] < _FACTS_TTL:
        return hit[1], hit[2]
    namespaces = sorted(n.metadata.name for n in k.core.list_namespace().items)[:100]
    resources: set[str] = set()
    for group in [get_json(k, "/api/v1")] + [
        get_json(k, f"/apis/{g['preferredVersion']['groupVersion']}")
        for g in get_json(k, "/apis").get("groups", [])
    ]:
        for res in group.get("resources", []):
            if "/" not in res["name"]:
                short = res.get("shortNames") or []
                resources.add(res["name"] + (f"({','.join(short)})" if short else ""))
    _facts[key] = (time.monotonic(), namespaces, sorted(resources))
    return namespaces, sorted(resources)


def _label_keys(k: Kube, ns: str) -> list[str]:
    keys: set[str] = set()
    for p in k.core.list_namespaced_pod(ns, limit=200).items:
        keys.update((p.metadata.labels or {}).keys())
    return sorted(keys)[:50]


_COMMAND = re.compile(r"^\s*COMMAND:\s*`?([:/].*?)`?\s*$", re.M)


async def ask(deps: Deps, r: Request) -> Stream:
    backend = deps.cfg.backend_for("ask")
    k = await asyncio.to_thread(deps.kube, r.context, r.kubeconfig)
    namespaces, resources = await asyncio.to_thread(_cluster_facts, k, f"{r.kubeconfig}|{r.context}")
    labels = await asyncio.to_thread(_label_keys, k, r.namespace)
    yield "meta", _meta(backend, r)
    prompt = prompts.ask(r.question, r.namespace, namespaces, labels, resources)
    out: list[str] = []
    async for item in _generate(deps, r, backend, prompt, [], Evidence(), out):
        yield item
    m = _COMMAND.search(final_turn(out))
    yield "result", {"command": m.group(1).strip() if m else ""}


TASKS = {"diagnose": diagnose, "logs": logs, "ask": ask, "capture": capture}
