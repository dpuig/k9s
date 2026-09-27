# ADR-0005: k9sai never mutates the cluster; read-only guard with two named exceptions

Status: Accepted, 2026-09-26

## Context

The daemon uses your own kubeconfig and credentials, which often carry write access. The
model is untrusted: log lines, annotations and CRD fields are attacker-controllable, and
since [ADR-0003](0003-hybrid-evidence.md) the model drives some tool calls. The original
proposal included "guarded remediation": propose a fix, dry-run it, then apply it after a human confirms.

Two Phase 2 features need API calls that change nothing but are not `get`/`list`/`watch`:

- **Server-side dry-run** (`PATCH`/`PUT` with `dryRun=All`), for the edit copilot and for
  proposed fixes.
- **SubjectAccessReview create** (the mechanism behind `kubectl auth can-i`), used to verify
  the RBAC explainer's answers.

## Decision

- **k9sai never applies a change.** A proposed fix is output as a patch, the server dry-run
  result and the exact `kubectl` command, which is copied to the clipboard. You run it yourself.
  This applies even if a Tier 2 native pane is built later, unless a new ADR supersedes this one.
- **All Kubernetes access goes through one client guard** that permits only `get`, `list` and
  `watch`, and rejects anything else *before* the request is sent.
- **Two named exceptions**, each with a precondition the guard enforces:
  - `PATCH`/`PUT` is allowed only when `dryRun=All` is set.
  - `create` is allowed only for `subjectaccessreviews` in `authorization.k8s.io`.
- **Exceptions are for our own code only.** They are never registered as model tools, so no
  prompt can reach them.
- **Guard test.** CI fails if any registered tool can reach a verb outside the allowlist, or
  if either exception can be triggered without its precondition.
- **No impersonation.** We don't use `--as` with a `view`-role identity, because impersonation
  rights aren't available on every cluster. The guard enforces read-only in our code.

## Alternatives considered

- **Apply after the user types `yes` in the pager.** A confirm prompt gives a weaker and
  harder-to-audit guarantee than "k9sai issues no real writes". Pasting a command costs you very little.
- **Also impersonate a `view` identity.** It would be stronger (the API server enforces it), but it
  doesn't work on clusters where you lack impersonation rights.
- **No exceptions: shell out to your `kubectl` for dry-runs.** That moves the write path
  outside the guard and its test, which is worse, not better.

## Consequences

- The safety claim is simple and testable: "the only non-read requests k9sai can send are
  dry-runs and access reviews, and the model can't trigger either."
- Prompt injection can at worst cause extra reads (capped at 3 tool calls). Those reads are redacted
  and can only go to allowlisted endpoints ([ADR-0004](0004-endpoint-allowlist.md)).
- Remediation always needs a manual step. We accept this as a deliberate cost.
- Any future write capability requires superseding this ADR, not just adding a tool.
