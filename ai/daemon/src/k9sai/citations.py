"""Checks [E#] citations in model output against the evidence that was shown."""

from __future__ import annotations

import re

_TAG = re.compile(r"\[(E\d+(?:\s*(?:[,;]|[-–])\s*E?\d+)*)\]")
_PART = re.compile(r"E?(\d+)\s*[-–]\s*E?(\d+)|E?(\d+)")
_MAX_RANGE = 200


def cited(text: str) -> list[int]:
    """Returns every cited id, in order of first appearance, ranges expanded."""
    seen: dict[int, None] = {}
    for tag in _TAG.finditer(text):
        for m in _PART.finditer(tag.group(1)):
            if m.group(3):
                seen.setdefault(int(m.group(3)))
                continue
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= hi and hi - lo <= _MAX_RANGE:
                for i in range(lo, hi + 1):
                    seen.setdefault(i)
            else:  # a malformed range cites both ends, so bogus ends get flagged
                seen.setdefault(lo)
                seen.setdefault(hi)
    return list(seen)


def invalid(text: str, valid_ids: set[int]) -> list[int]:
    return [i for i in cited(text) if i not in valid_ids]


def footer(text: str, valid_ids: set[int]) -> str:
    bad = invalid(text, valid_ids)
    if not bad:
        return ""
    ids = ", ".join(f"E{i}" for i in bad)
    return f"\n\n---\n⚠️  Cited evidence that does not exist: {ids}. Treat those claims as unsupported.\n"
