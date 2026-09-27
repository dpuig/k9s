"""Builds evidence bundles from the cluster (pre-gathered half of ADR-0003).

Renders compact, describe-like text rather than raw objects: small models do
better with short labelled lines, and every line becomes a citable E#.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kubernetes.client import ApiException

from k9sai.evidence import Evidence
from k9sai.kube import Kube, read_log

LOG_TAIL = 60
MAX_PODS = 3
EVENT_WINDOW = timedelta(hours=1)

WORKLOADS = {
    "pods": "Pod",
    "deployments": "Deployment",
    "statefulsets": "StatefulSet",
    "daemonsets": "DaemonSet",
    "replicasets": "ReplicaSet",
}


def _ts(t) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else "?"


def _resources(r) -> str:
    if not r:
        return "none"
    req = ",".join(f"{k}={v}" for k, v in (r.requests or {}).items()) or "-"
    lim = ",".join(f"{k}={v}" for k, v in (r.limits or {}).items()) or "-"
    return f"requests[{req}] limits[{lim}]"


def _probe(name: str, p) -> str | None:
    if not p:
        return None
    if p.http_get:
        how = f"http-get :{p.http_get.port}{p.http_get.path or ''}"
    elif p.tcp_socket:
        how = f"tcp :{p.tcp_socket.port}"
    elif p._exec:
        how = "exec " + " ".join(p._exec.command or [])
    else:
        how = "grpc"
    return (
        f"{name}: {how} delay={p.initial_delay_seconds or 0}s period={p.period_seconds or 10}s "
        f"timeout={p.timeout_seconds or 1}s failureThreshold={p.failure_threshold or 3}"
    )


def _env(c) -> list[str]:
    out = []
    for e in c.env or []:
        src = e.value_from
        if src and src.secret_key_ref:
            out.append(f"env {e.name} <- secret {src.secret_key_ref.name}/{src.secret_key_ref.key}")
        elif src and src.config_map_key_ref:
            r = src.config_map_key_ref
            out.append(f"env {e.name} <- configmap {r.name}/{r.key}")
        elif src:
            out.append(f"env {e.name} <- fieldRef/resourceFieldRef")
        else:
            out.append(f"env {e.name}={e.value}")  # redact() scrubs sensitive names
    for ef in c.env_from or []:
        if ef.config_map_ref:
            out.append(f"envFrom configmap {ef.config_map_ref.name}")
        if ef.secret_ref:
            out.append(f"envFrom secret {ef.secret_ref.name}")
    return out


def _state(label: str, s) -> str | None:
    if not s:
        return None
    if s.waiting:
        return f"{label}: Waiting reason={s.waiting.reason} message={s.waiting.message or ''}"
    if s.terminated:
        t = s.terminated
        return (
            f"{label}: Terminated reason={t.reason} exitCode={t.exit_code} "
            f"started={_ts(t.started_at)} finished={_ts(t.finished_at)} message={t.message or ''}"
        )
    if s.running:
        return f"{label}: Running since {_ts(s.running.started_at)}"
    return None


def describe_pod(pod) -> list[str]:
    m, sp, st = pod.metadata, pod.spec, pod.status
    lines = [
        f"Pod {m.namespace}/{m.name} phase={st.phase} node={sp.node_name or '<unscheduled>'} "
        f"qos={st.qos_class} created={_ts(m.creation_timestamp)}",
    ]
    if st.reason or st.message:
        lines.append(f"Pod status reason={st.reason} message={st.message}")
    for c in st.conditions or []:
        extra = f" reason={c.reason} message={c.message}" if c.reason or c.message else ""
        lines.append(f"Condition {c.type}={c.status}{extra}")
    statuses = {s.name: s for s in (st.init_container_statuses or []) + (st.container_statuses or [])}
    for kind, containers in (("init container", sp.init_containers or []), ("container", sp.containers)):
        for c in containers:
            s = statuses.get(c.name)
            head = f"{kind} {c.name} image={c.image}"
            if s:
                head += f" ready={s.ready} restarts={s.restart_count}"
            lines.append(head)
            lines.append(f"  resources: {_resources(c.resources)}")
            if s:
                states = (_state("state", s.state), _state("lastState", s.last_state))
                lines += [f"  {x}" for x in states if x]
            for name, p in (
                ("liveness", c.liveness_probe),
                ("readiness", c.readiness_probe),
                ("startup", c.startup_probe),
            ):
                if (line := _probe(name, p)) is not None:
                    lines.append(f"  {line}")
            if c.command or c.args:
                lines.append(f"  command: {' '.join((c.command or []) + (c.args or []))}")
            lines += [f"  {x}" for x in _env(c)]
    for v in sp.volumes or []:
        if v.persistent_volume_claim:
            lines.append(f"volume {v.name} <- pvc {v.persistent_volume_claim.claim_name}")
        elif v.config_map:
            lines.append(f"volume {v.name} <- configmap {v.config_map.name}")
        elif v.secret:
            lines.append(f"volume {v.name} <- secret {v.secret.secret_name}")
    for t in sp.tolerations or []:
        if t.key and not t.key.startswith("node.kubernetes.io/"):
            lines.append(f"toleration {t.key}{'=' + t.value if t.value else ''}:{t.effect}")
    if sp.node_selector:
        lines.append(f"nodeSelector {sp.node_selector}")
    return lines


def events_for(k: Kube, ns: str, kind: str, name: str) -> list[str]:
    try:
        evs = k.core.list_namespaced_event(
            ns, field_selector=f"involvedObject.name={name},involvedObject.kind={kind}"
        ).items
    except ApiException as e:
        return [f"events unavailable: {e.reason}"]
    cutoff = datetime.now(timezone.utc) - EVENT_WINDOW
    rows = []
    for e in evs:
        when = e.last_timestamp or e.event_time or e.first_timestamp
        if when and when < cutoff:
            continue
        rows.append((when or cutoff, f"{_ts(when)} {e.type} {e.reason} x{e.count or 1}: {e.message}"))
    return [r for _, r in sorted(rows, key=lambda r: r[0])]


def pod_logs(k: Kube, pod, tail: int = LOG_TAIL) -> list[tuple[str, list[str]]]:
    """(title, lines) per container: previous logs when it restarted, then current."""
    out = []
    statuses = {s.name: s for s in pod.status.container_statuses or []}
    for c in pod.spec.containers:
        s = statuses.get(c.name)
        wanted = [("previous", True)] if s and s.restart_count else []
        wanted.append(("current", False))
        for label, prev in wanted:
            try:
                text = read_log(
                    k,
                    pod.metadata.name,
                    pod.metadata.namespace,
                    container=c.name,
                    previous=prev,
                    tail_lines=tail,
                )
                lines = text.splitlines() or ["<empty>"]
            except ApiException as e:
                lines = [f"logs unavailable: {e.reason}"]
            out.append((f"logs {c.name} ({label})", lines))
    return out


def owner_chain(k: Kube, ns: str, refs) -> list[str]:
    lines, seen = [], 0
    while refs and seen < 4:
        ref = next((r for r in refs if r.controller), refs[0])
        seen += 1
        try:
            if ref.kind == "ReplicaSet":
                o = k.apps.read_namespaced_replica_set(ref.name, ns)
            elif ref.kind == "Deployment":
                o = k.apps.read_namespaced_deployment(ref.name, ns)
            elif ref.kind == "StatefulSet":
                o = k.apps.read_namespaced_stateful_set(ref.name, ns)
            elif ref.kind == "DaemonSet":
                o = k.apps.read_namespaced_daemon_set(ref.name, ns)
            else:
                lines.append(f"owner {ref.kind}/{ref.name}")
                break
        except ApiException as e:
            lines.append(f"owner {ref.kind}/{ref.name} unavailable: {e.reason}")
            break
        lines += workload_summary(o, ref.kind)
        refs = o.metadata.owner_references
    return lines


def workload_summary(o, kind: str) -> list[str]:
    m, st = o.metadata, o.status
    rev = (m.annotations or {}).get("deployment.kubernetes.io/revision", "-")
    images = ",".join(c.image for c in o.spec.template.spec.containers)
    lines = [
        f"{kind} {m.name} revision={rev} replicas={getattr(o.spec, 'replicas', '-')} "
        f"ready={getattr(st, 'ready_replicas', None) or 0} "
        f"available={getattr(st, 'available_replicas', None) or 0} images={images} "
        f"created={_ts(m.creation_timestamp)}"
    ]
    for c in getattr(st, "conditions", None) or []:
        lines.append(f"  {kind} condition {c.type}={c.status} reason={c.reason} message={c.message}")
    return lines


def node_conditions(k: Kube, name: str) -> list[str]:
    try:
        n = k.core.read_node(name)
    except ApiException as e:
        return [f"node {name} unavailable: {e.reason}"]
    lines = [
        f"Node {name} allocatable cpu={n.status.allocatable.get('cpu')} "
        f"memory={n.status.allocatable.get('memory')}"
    ]
    for c in n.status.conditions or []:
        normal = (c.type == "Ready") == (c.status == "True")
        if not normal or c.type == "Ready":
            lines.append(f"  condition {c.type}={c.status} reason={c.reason} message={c.message}")
    lines += [f"  taint {t.key}={t.value or ''}:{t.effect}" for t in n.spec.taints or []]
    return lines


def pvc_lines(k: Kube, ns: str, pod) -> list[str]:
    lines = []
    for v in pod.spec.volumes or []:
        if not v.persistent_volume_claim:
            continue
        name = v.persistent_volume_claim.claim_name
        try:
            pvc = k.core.read_namespaced_persistent_volume_claim(name, ns)
            lines.append(
                f"PVC {name} phase={pvc.status.phase} storageClass="
                f"{pvc.spec.storage_class_name} request={pvc.spec.resources.requests}"
            )
        except ApiException as e:
            lines.append(f"PVC {name} unavailable: {e.reason}")
        lines += [f"  {x}" for x in events_for(k, ns, "PersistentVolumeClaim", name)]
    return lines


def is_failing(pod) -> bool:
    if pod.status.phase in ("Pending", "Failed", "Unknown"):
        return True
    for s in pod.status.container_statuses or []:
        if not s.ready or s.restart_count:
            return True
    return False


def diagnose_evidence(k: Kube, ns: str, resource: str, name: str) -> Evidence:
    """Standard bundle, priority order: describe, events, logs, owners, nodes."""
    ev = Evidence()
    if resource == "pods":
        pods = [k.core.read_namespaced_pod(name, ns)]
        owners = owner_chain(k, ns, pods[0].metadata.owner_references)
    else:
        kind = WORKLOADS.get(resource)
        if kind is None or kind == "Pod":
            raise ValueError(f"diagnose does not support {resource!r}")
        read = {
            "Deployment": k.apps.read_namespaced_deployment,
            "StatefulSet": k.apps.read_namespaced_stateful_set,
            "DaemonSet": k.apps.read_namespaced_daemon_set,
            "ReplicaSet": k.apps.read_namespaced_replica_set,
        }[kind]
        sel = read(name, ns).spec.selector.match_labels or {}
        selector = ",".join(f"{a}={b}" for a, b in sel.items())
        all_pods = k.core.list_namespaced_pod(ns, label_selector=selector).items
        pods = [p for p in all_pods if is_failing(p)][:MAX_PODS] or all_pods[:1]
        owners = owner_chain(k, ns, pods[0].metadata.owner_references) if pods else []
        ev.add(
            f"{kind} {name}",
            [
                f"{kind} {name} selector={selector} pods={len(all_pods)} "
                f"failing={sum(is_failing(p) for p in all_pods)}"
            ],
        )
        ev.add(f"events {kind} {name}", events_for(k, ns, kind, name))
    for p in pods:
        pn = p.metadata.name
        ev.add(f"describe pod {pn}", describe_pod(p))
        ev.add(f"events pod {pn}", events_for(k, ns, "Pod", pn))
        ev.add(f"pvcs pod {pn}", pvc_lines(k, ns, p))
    for p in pods:
        for title, lines in pod_logs(k, p):
            ev.add(f"{title} [{p.metadata.name}]", lines, keep="tail")
    ev.add("owner chain", owners)
    for node in sorted({p.spec.node_name for p in pods if p.spec.node_name}):
        ev.add(f"node {node}", node_conditions(k, node))
    return ev
