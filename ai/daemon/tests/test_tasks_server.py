import asyncio
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import pytest

from k9sai import drainlogs, server, tasks
from k9sai.backend import StubBackend
from k9sai.config import parse
from k9sai.tasks import Deps, Request

CFG = parse(
    {
        "default": "o",
        "backends": {"o": {"type": "openai", "base_url": "http://localhost:1/v1", "model": "stub"}},
    }
)

REPLY = """## Summary
The container is OOMKilled [E6].

## Hypotheses
1. **Memory limit too low** (category: oom, confidence: high)
   Last state OOMKilled with exit 137 [E6], limit 64Mi [E4].
2. **Leak** (category: app-crash, confidence: low)
   Cache allocation [E999].

## Suggested fix
Raise the limit."""


def deps(cluster, stub):
    return Deps(cfg=CFG, backend=lambda b: stub, kube=lambda ctx, kc: cluster.kube)


async def collect(stream):
    return [item async for item in stream]


async def test_diagnose_end_to_end(cluster):
    stub = StubBackend(REPLY)
    r = Request(task="diagnose", namespace="shop", resource="pods", name="web-1", json=True)
    events = await collect(tasks.diagnose(deps(cluster, stub), r))
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and "token" in kinds and kinds[-1] == "result"
    prompt = stub.prompts[0]
    assert "OOMKilled" in prompt and "hunter2" not in prompt and "E1:" in prompt
    footer = dict(events)["footer"]["text"]
    assert "E999" in footer
    result = dict(events)["result"]
    assert [h["category"] for h in result["hypotheses"]] == ["oom", "app-crash"]
    assert result["invalid_citations"] == [999]
    assert {m for m, _ in cluster.requests} == {"GET"}


async def test_logs_are_real_lines_not_bytes_repr(cluster):
    """Regression: the generated client returns "b'...'" for the log endpoint."""
    stub = StubBackend("## Summary\nx")
    r = Request(task="diagnose", namespace="shop", resource="pods", name="web-1")
    await collect(tasks.diagnose(deps(cluster, stub), r))
    prompt = stub.prompts[0]
    assert "b'" not in prompt and "\\n" not in prompt
    assert re.search(r"E\d+: 2026-09-26T19:59:01Z allocating cache 900Mi", prompt) or re.search(
        r"E\d+: allocating cache 900Mi", prompt
    )


async def test_diagnose_deployment_finds_failing_pods(cluster):
    stub = StubBackend("## Summary\nx")
    r = Request(task="diagnose", namespace="shop", resource="deployments", name="web")
    await collect(tasks.diagnose(deps(cluster, stub), r))
    assert "describe pod web-1" in stub.prompts[0]


async def test_model_tool_calls_extend_evidence(cluster):
    stub = StubBackend(
        lambda p: f"see [E{p.count('E')}]", tool_calls=[("get_events", {"kind": "Pod", "name": "web-1"})]
    )
    r = Request(task="diagnose", namespace="shop", name="web-1")
    events = await collect(tasks.diagnose(deps(cluster, stub), r))
    assert ("tool", {"name": "get_events"}) in events


async def test_capture_then_replay_without_cluster(cluster):
    r = Request(task="capture", namespace="shop", name="web-1")
    ((_, cap),) = await collect(tasks.capture(deps(cluster, None), r))
    assert "hunter2" not in json.dumps(cap)
    stub = StubBackend(REPLY)
    replay = Request(task="diagnose", fixture=json.loads(json.dumps(cap)), json=True)
    offline = Deps(cfg=CFG, backend=lambda b: stub, kube=lambda c, kc: pytest.fail("no cluster"))
    events = await collect(tasks.diagnose(offline, replay))
    assert dict(events)["result"]["hypotheses"][0]["category"] == "oom"


def test_drain_new_templates():
    now = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)
    old = [f"2026-09-26T19:{m:02d}:00Z GET /api/items/{m} 200 {m}ms" for m in range(30, 45)]
    new = [f"2026-09-26T19:{m:02d}:00Z ERROR db timeout after {m}s" for m in range(55, 59)]
    ev = drainlogs.log_evidence(old + new, now, "t")
    ev.pack(5000)
    text = ev.render()
    assert "new in last 10m" in text and "db timeout" in text.split("### templates")[0]
    assert "count=15" in text


def test_drain_short_window_is_reported():
    now = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)
    ev = drainlogs.log_evidence(["2026-09-26T19:58:00Z hello"], now, "t")
    ev.pack(1000)
    assert "cannot compute" in ev.render()


def test_parse_ts_nanoseconds():
    ts, msg = drainlogs.parse_ts("2026-09-26T19:59:00.123456789Z hello world")
    assert ts.microsecond == 123456 and msg == "hello world"


async def test_server_streams_over_unix_socket(cluster):
    sock = Path(tempfile.mkdtemp(prefix="k9sai", dir="/tmp")) / "s" / "d.sock"
    d = deps(cluster, StubBackend(REPLY))
    srv = asyncio.create_task(server.serve(d, sock))
    for _ in range(50):
        if sock.exists():
            break
        await asyncio.sleep(0.05)
    assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(sock.parent).st_mode) == 0o700
    conn = aiohttp.UnixConnector(path=str(sock))
    async with aiohttp.ClientSession(connector=conn) as s:
        async with s.get("http://k9sai/healthz") as r:
            assert (await r.json())["ok"]
        body = {"namespace": "shop", "resource": "pods", "name": "web-1"}
        async with s.post("http://k9sai/v1/diagnose", json=body) as r:
            text = await r.text()
        assert (
            "event: meta" in text and "event: footer" in text and text.endswith("event: done\ndata: {}\n\n")
        )
        async with s.post("http://k9sai/v1/nope", json={}) as r:
            assert r.status == 404
    srv.cancel()
    with pytest.raises(asyncio.CancelledError):
        await srv
    assert not sock.exists()


def test_socket_path_length_is_checked(tmp_path):
    with pytest.raises(SystemExit, match="too long"):
        server._prepare_socket(tmp_path / ("x" * 120) / "d.sock")


def test_final_turn_ignores_drafts_before_tool_calls():
    out = [
        "## Hypotheses\n1. **Draft** (category: other",
        tasks.TURN_BREAK,
        "   ",
        tasks.TURN_BREAK,
        "## Hypotheses\n1. **Final** (category: oom, confidence: high)",
    ]
    assert [h["category"] for h in tasks.parse_hypotheses(tasks.final_turn(out))] == ["oom"]
    assert tasks.final_turn([]) == ""


async def test_ask_grounds_prompt_in_cluster_facts(cluster):
    stub = StubBackend("COMMAND: `:pods payments /web`\nWHY: pods in payments named web")
    r = Request(task="ask", namespace="shop", question="web pods in payments")
    events = await collect(tasks.ask(deps(cluster, stub), r))
    prompt = stub.prompts[0]
    assert "namespaces: payments, shop" in prompt
    assert "deployments(deploy)" in prompt and "pods(po)" in prompt and "pods/log" not in prompt
    assert "label keys in shop: app" in prompt
    assert dict(events)["result"] == {"command": ":pods payments /web"}
    assert {m for m, _ in cluster.requests} == {"GET"}
