# ADR-0003: Hybrid evidence — pre-gathered bundle plus capped read-only tools

Status: Accepted, 2026-09-26

## Context

The Phase 1 success bar requires diagnoses to rank the true cause in the top 2 in ≥ 70% of
graded incidents, with **zero** invented evidence citations. That means evidence must be
addressable and checkable.

The model is untrusted: logs and annotations can carry prompt injection. The SDK docs make no
promise that tool calling works on local models, and we route tasks to models of varying
size and backend.

## Decision

- The daemon always **pre-gathers** a standard evidence bundle, in priority order:
  - describe
  - events from the last hour
  - current and previous container logs
  - the owner chain
  - node conditions
- The bundle is redacted, numbered line by line as `E1…En`, and packed into a token budget.
- The agent also gets typed read-only tools (`get`, `describe`, `events`, `logs`,
  `node_conditions`, `rbac_lookup`) under `policies=[deny("*"), allow(...)]`, capped at
  **3 follow-up calls**. Tool output passes through the same redactor and is appended as more
  `E#` lines.
- The model tags claims with `[E#]`. After the stream ends, the daemon checks every cited ID
  and appends a ⚠️ footer for any that don't exist.
- All Kubernetes access goes through a guard that permits only `get`, `list` and `watch`, plus
  two named exceptions: `dryRun=All` writes, and SubjectAccessReview creates. The exceptions are
  callable only from our own code and are never exposed as tools. A test enforces this
  (see [ADR-0005](0005-read-only-cluster-access.md)).

## Alternatives considered

- **Pre-gathered only (no tools).** Deterministic, but it wastes the SDK's main capability and
  can't follow a lead.
- **Fully agentic.** Quality depends entirely on tool calling that isn't promised on local
  models, and evidence would differ from run to run.

## Consequences

- Every diagnosis has a baseline of citable evidence, even if tool calls fail.
- Prompt injection can at worst steer the model into up to 3 extra *reads*. It can never write,
  because the exceptions aren't tools.
- The eval runs each scenario with tools on and off, which tells us per backend whether
  the tools are worth it.
