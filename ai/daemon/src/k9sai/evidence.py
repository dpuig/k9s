"""Numbered, redacted, budgeted evidence (ADR-0003).

Sections are added in priority order. pack() assigns E# ids only to lines that fit
the token budget, so the model can never see -- and therefore never legitimately
cite -- an id that was dropped. Tool results are appended later and continue the
numbering.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from k9sai.redact import redact


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


@dataclass
class Section:
    title: str
    lines: list[str]
    keep: str = "head"  # "tail" keeps the newest lines (logs)


@dataclass
class Evidence:
    sections: list[Section] = field(default_factory=list)
    numbered: list[tuple[int, str, str]] = field(default_factory=list)  # (id, section, line)
    dropped: list[str] = field(default_factory=list)

    def add(self, title: str, lines: list[str], keep: str = "head") -> None:
        # Redact whole texts before splitting: some secrets span lines (YAML env pairs).
        clean = [line for text in lines for line in redact(text).splitlines() if line.strip()]
        if clean:
            self.sections.append(Section(title, clean, keep))

    @property
    def ids(self) -> set[int]:
        return {i for i, _, _ in self.numbered}

    def _number(self, title: str, lines: list[str]) -> None:
        start = len(self.numbered) + 1
        self.numbered.extend((start + k, title, line) for k, line in enumerate(lines))

    def pack(self, budget_tokens: int) -> None:
        """Numbers as many lines as fit, section by section, in priority order."""
        remaining = budget_tokens
        for s in self.sections:
            cost = [estimate_tokens(line) + 2 for line in s.lines]
            if sum(cost) <= remaining:
                self._number(s.title, s.lines)
                remaining -= sum(cost)
                continue
            order = range(len(s.lines) - 1, -1, -1) if s.keep == "tail" else range(len(s.lines))
            kept: list[int] = []
            for i in order:
                if cost[i] > remaining:
                    break
                kept.append(i)
                remaining -= cost[i]
            lines = [s.lines[i] for i in sorted(kept)]
            if lines:
                self._number(s.title, lines)
            self.dropped.append(f"{s.title}: {len(s.lines) - len(lines)}/{len(s.lines)} lines")

    def append(self, title: str, text: str) -> str:
        """Adds a tool result; returns it rendered with its new E# ids."""
        start = len(self.numbered)
        self._number(title, [line for line in redact(text).splitlines() if line.strip()])
        return self._render(self.numbered[start:])

    def render(self) -> str:
        return self._render(self.numbered)

    @staticmethod
    def _render(rows: list[tuple[int, str, str]]) -> str:
        out, current = [], None
        for i, title, line in rows:
            if title != current:
                out.append(f"### {title}")
                current = title
            out.append(f"E{i}: {line}")
        return "\n".join(out)

    def to_dict(self) -> dict:
        return {"sections": [{"title": s.title, "keep": s.keep, "lines": s.lines} for s in self.sections]}

    @classmethod
    def from_dict(cls, data: dict) -> Evidence:
        """Rebuilds from a capture fixture. Lines were redacted at capture time."""
        ev = cls()
        ev.sections = [Section(s["title"], s["lines"], s.get("keep", "head")) for s in data["sections"]]
        return ev
