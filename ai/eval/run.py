"""k9sai eval harness (ai/DESIGN.md "Evaluation").

Live mode creates/uses a dedicated kind cluster, applies each scenario into its own
namespace, waits for the failure to manifest, captures a redacted fixture, then
diagnoses it with tools on (live cluster) and tools off (fixture replay).
--replay skips the cluster and grades the committed fixtures only.

This is a dev tool: unlike the daemon it mutates a cluster, so every kubectl call is
pinned to --context kind-<cluster> and it refuses to run anywhere else.

Bar: true cause in the top-2 hypotheses for >= 70% of runs, and zero invented citations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from k9sai import config, kube, tasks
from k9sai.backend import SDKBackend
from k9sai.tasks import Deps, Request

HERE = Path(__file__).parent
SCENARIOS, FIXTURES, REPORTS = HERE / "scenarios", HERE / "fixtures", HERE / "reports"
TOP2_BAR = 0.70


def kubectl(ctx: str, *args: str, check: bool = True) -> str:
    out = subprocess.run(["kubectl", "--context", ctx, *args], capture_output=True, text=True)
    if check and out.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def ensure_cluster(name: str) -> str:
    clusters = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True).stdout.split()
    if name not in clusters:
        print(f"creating kind cluster {name} ...", flush=True)
        previous = subprocess.run(
            ["kubectl", "config", "current-context"], capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(["kind", "create", "cluster", "--name", name, "--wait", "90s"], check=True)
        if previous:  # kind switches current-context; leave the user's kubeconfig as it was
            subprocess.run(["kubectl", "config", "use-context", previous], check=True)
    return f"kind-{name}"


def load_scenario(path: Path) -> dict:
    docs = list(yaml.safe_load_all(path.read_text()))
    w = next(d for d in docs if d["kind"] in ("Deployment", "StatefulSet"))
    ann = w["metadata"]["annotations"]
    return {
        "id": path.stem,
        "path": path,
        "ns": f"eval-{path.stem}",
        "resource": {"Deployment": "deployments", "StatefulSet": "statefulsets"}[w["kind"]],
        "name": w["metadata"]["name"],
        "expect": ann["k9sai.eval/expect"].split(","),
        "wait": ann["k9sai.eval/wait"],
    }


def _reasons(cs: dict) -> set[str]:
    out = set()
    for key in ("state", "lastState"):
        for sub in (cs.get(key) or {}).values():
            if isinstance(sub, dict) and sub.get("reason"):
                out.add(sub["reason"])
    return out


def condition_met(ctx: str, s: dict) -> bool:
    kind, _, value = s["wait"].partition("=")
    pods = json.loads(kubectl(ctx, "-n", s["ns"], "get", "pods", "-o", "json"))["items"]
    statuses = [cs for p in pods for cs in p.get("status", {}).get("containerStatuses") or []]
    if kind == "restarts>":  # "restarts>=N" partitions as ("restarts>", "=", "N")
        return any(cs.get("restartCount", 0) >= int(value) for cs in statuses)
    if kind == "reason":
        wanted = set(value.split("|"))
        return any(_reasons(cs) & wanted for cs in statuses)
    if kind == "event":
        wanted = set(value.split("|"))
        events = json.loads(kubectl(ctx, "-n", s["ns"], "get", "events", "-o", "json"))["items"]
        return any(e.get("reason") in wanted for e in events)
    if kind == "notready":
        secs = int(value.rstrip("s"))
        now = datetime.now(timezone.utc)
        for p in pods:
            started = p.get("status", {}).get("startTime")
            if started and statuses and not any(cs.get("ready") for cs in statuses):
                age = (now - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()
                if age >= secs:
                    return True
        return False
    raise ValueError(f"unknown wait condition {s['wait']!r}")


def apply_and_wait(ctx: str, scenarios: list[dict], timeout: int) -> list[dict]:
    for s in scenarios:
        kubectl(ctx, "create", "namespace", s["ns"], check=False)
        kubectl(ctx, "-n", s["ns"], "apply", "-f", str(s["path"]))
    pending, ready, deadline = list(scenarios), [], time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for s in list(pending):
            if condition_met(ctx, s):
                print(f"  ready: {s['id']} ({s['wait']})", flush=True)
                ready.append(s)
                pending.remove(s)
        if pending:
            time.sleep(5)
    for s in pending:
        print(f"  TIMEOUT: {s['id']} never reached {s['wait']}; skipped", flush=True)
    return ready


async def capture(deps: Deps, ctx: str, s: dict) -> dict:
    r = Request(
        task="capture",
        context=ctx,
        namespace=s["ns"],
        resource=s["resource"],
        name=s["name"],
    )
    [(_, cap)] = [item async for item in tasks.capture(deps, r)]
    cap.update(scenario=s["id"], expect=s["expect"])
    (FIXTURES / f"{s['id']}.json").write_text(json.dumps(cap, indent=1))
    return cap


async def diagnose(deps: Deps, req: Request) -> dict:
    result, error = {}, None
    async for event, data in tasks.diagnose(deps, req):
        if event == "result":
            result = data
        elif event == "error":
            error = data
    return result or {"error": error or "no result"}


def grade(run: dict, expect: list[str]) -> dict:
    cats = [h["category"] for h in run.get("hypotheses", [])]
    return {
        "categories": cats,
        "top1": bool(cats[:1]) and cats[0] in expect,
        "top2": any(c in expect for c in cats[:2]),
        "invalid": run.get("invalid_citations", []),
        "seconds": run.get("seconds"),
        "error": run.get("error"),
    }


def report(rows: list[dict], backend: config.Backend, started: datetime) -> tuple[str, bool]:
    lines = [
        f"# k9sai eval — {started:%Y-%m-%d %H:%M} UTC",
        "",
        f"Backend `{backend.name}` · model `{backend.model or backend.model_path}` · "
        f"bar: top-2 ≥ {TOP2_BAR:.0%} and zero invented citations",
        "",
        "| mode | runs | top-1 | top-2 | runs w/ invented citations | median s | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    passed_all = True
    for mode in sorted({r["mode"] for r in rows}):
        rs = [r for r in rows if r["mode"] == mode]
        n = len(rs)
        top1 = sum(r["top1"] for r in rs) / n
        top2 = sum(r["top2"] for r in rs) / n
        bad = sum(bool(r["invalid"]) for r in rs)
        secs = [r["seconds"] for r in rs if r["seconds"] is not None]
        ok = top2 >= TOP2_BAR and bad == 0
        passed_all &= ok
        med = f"{statistics.median(secs):.0f}" if secs else "-"
        lines.append(
            f"| {mode} | {n} | {top1:.0%} | {top2:.0%} | {bad} | {med} | {'PASS' if ok else 'FAIL'} |"
        )
    lines += [
        "",
        "| scenario | mode | expected | got (top 2) | top-2 | invented | s |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        got = ", ".join(r["categories"][:2]) or (f"error: {r['error']}" if r["error"] else "-")
        inv = ", ".join(f"E{i}" for i in r["invalid"]) or "-"
        lines.append(
            f"| {r['id']} | {r['mode']} | {'/'.join(r['expect'])} | {got} | "
            f"{'✅' if r['top2'] else '❌'} | {inv} | {r['seconds'] or '-'} |"
        )
    return "\n".join(lines) + "\n", passed_all


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cluster", default="k9sai-eval")
    p.add_argument("--only", default="", help="comma-separated scenario ids")
    p.add_argument("--replay", action="store_true", help="grade committed fixtures; no cluster")
    p.add_argument("--keep", action="store_true", help="keep scenario namespaces")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--config", type=Path, default=None)
    args = p.parse_args()

    cfg = config.load(args.config)
    backend = cfg.backend_for("diagnose")
    deps = Deps(cfg=cfg, backend=SDKBackend, kube=kube.connect)
    only = set(filter(None, args.only.split(",")))
    started = datetime.now(timezone.utc)
    rows: list[dict] = []

    if args.replay:
        fixtures = sorted(FIXTURES.glob("*.json"))
        work = [(json.loads(f.read_text()), None) for f in fixtures if not only or f.stem in only]
    else:
        ctx = ensure_cluster(args.cluster)
        paths = sorted(SCENARIOS.glob("*.yaml"))
        scenarios = [load_scenario(f) for f in paths if not only or f.stem in only]
        print(f"applying {len(scenarios)} scenarios to {ctx}", flush=True)
        ready = apply_and_wait(ctx, scenarios, args.timeout)
        work = [(await capture(deps, ctx, s), s) for s in ready]

    for fixture, live in work:
        sid, expect = fixture["scenario"], fixture["expect"]
        modes = [
            (
                "no-tools",
                Request(task="diagnose", fixture=fixture, json=True, tools=False),
            )
        ]
        if live:
            modes.append(
                (
                    "tools",
                    Request(
                        task="diagnose",
                        context=ensure_cluster(args.cluster),
                        namespace=live["ns"],
                        resource=live["resource"],
                        name=live["name"],
                        json=True,
                        tools=True,
                    ),
                )
            )
        for mode, req in modes:
            print(f"diagnosing {sid} [{mode}] ...", flush=True)
            g = grade(await diagnose(deps, req), expect)
            rows.append({"id": sid, "mode": mode, "expect": expect, **g})
            print(
                f"  got {g['categories'][:2]} top2={g['top2']} invalid={g['invalid']}",
                flush=True,
            )

    if not args.replay and not args.keep:
        for s in scenarios:
            kubectl(
                ensure_cluster(args.cluster),
                "delete",
                "namespace",
                s["ns"],
                "--wait=false",
                check=False,
            )
    if not rows:
        print("no runs; nothing to report", file=sys.stderr)
        return 1
    text, ok = report(rows, backend, started)
    REPORTS.mkdir(exist_ok=True)
    slug = (backend.model or Path(backend.model_path).stem).replace(":", "-").replace("/", "-")
    stem = f"{started:%Y-%m-%d}-{slug}{'-replay' if args.replay else ''}"
    (REPORTS / f"{stem}.md").write_text(text)
    (REPORTS / f"{stem}.json").write_text(json.dumps(rows, indent=1))
    print(text)
    print(f"report: {REPORTS / (stem + '.md')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
