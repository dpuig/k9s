"""Log template mining with drain3 plus a "new in the last N minutes" diff."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from drain3 import TemplateMiner
from drain3.masking import MaskingInstruction
from drain3.template_miner_config import TemplateMinerConfig

from k9sai.evidence import Evidence

WINDOW_LINES = 5000
NEW_WINDOW = timedelta(minutes=10)
TOP_CLUSTERS = 25

_MASKS = [
    (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "UUID"),
    (r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "IP"),
    (r"\b0x[0-9a-fA-F]+\b|\b[0-9a-f]{12,}\b", "HEX"),
    (r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:ms|s|m|h|%|Mi|Gi|Ki)?(?![\w.])", "NUM"),
]


@dataclass
class Cluster:
    id: int
    template: str
    count: int
    first: datetime | None
    last: datetime | None
    example: str


def _miner() -> TemplateMiner:
    cfg = TemplateMinerConfig()
    cfg.masking_instructions = [MaskingInstruction(p, name) for p, name in _MASKS]
    cfg.profiling_enabled = False
    return TemplateMiner(config=cfg)


_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d) (.*)$")


def parse_ts(line: str) -> tuple[datetime | None, str]:
    """Splits the RFC3339Nano prefix added by `timestamps=true`."""
    m = _TS.match(line)
    if not m:
        return None, line
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.fromisoformat(f"{m.group(1)}.{frac}{tz}"), m.group(4)


def mine(lines: list[str]) -> list[Cluster]:
    miner, stats = _miner(), {}
    for raw in lines:
        ts, msg = parse_ts(raw)
        if not msg.strip():
            continue
        r = miner.add_log_message(msg)
        cid = r["cluster_id"]
        c = stats.get(cid)
        if c is None:
            stats[cid] = Cluster(cid, "", 1, ts, ts, msg[:300])
        else:
            c.count += 1
            c.last = ts or c.last
    for c in miner.drain.clusters:
        if c.cluster_id in stats:
            stats[c.cluster_id].template = c.get_template()
    return sorted(stats.values(), key=lambda c: -c.count)


def _fmt(t: datetime | None) -> str:
    return t.strftime("%H:%M:%S") if t else "?"


def log_evidence(lines: list[str], now: datetime, title: str) -> Evidence:
    clusters = mine(lines)
    stamps = [t for t, _ in map(parse_ts, lines) if t]
    oldest = min(stamps) if stamps else None
    ev = Evidence()
    ev.add(
        "window",
        [
            f"{title}: {len(lines)} lines, {len(clusters)} templates, "
            f"oldest={_fmt(oldest)} newest={_fmt(max(stamps) if stamps else None)} now={_fmt(now)}"
        ],
    )
    cutoff = now - NEW_WINDOW
    if oldest is None:
        ev.add("new in last 10m", ["cannot compute: log lines carry no timestamps"])
    elif oldest > cutoff:
        ev.add(
            "new in last 10m",
            [f"cannot compute: window only reaches back to {_fmt(oldest)}, less than 10 minutes"],
        )
    else:
        new = [c for c in clusters if c.first and c.first >= cutoff]
        ev.add(
            "new in last 10m",
            [f"C{c.id} count={c.count} first={_fmt(c.first)} template={c.template}" for c in new]
            or ["none: every template also appeared before the last 10 minutes"],
        )
    ev.add(
        "templates by count",
        [
            f"C{c.id} count={c.count} first={_fmt(c.first)} last={_fmt(c.last)} template={c.template}"
            for c in clusters[:TOP_CLUSTERS]
        ],
    )
    ev.add("examples", [f"C{c.id} example: {c.example}" for c in clusters[:TOP_CLUSTERS]])
    return ev
