# ADR-0002: Go client, HTTP + SSE over a Unix socket

Status: Accepted, 2026-09-26

## Context

k9s plugins can only exec a command. Given how k9s runs plugins
(`internal/view/actions.go`, `exec.go`):

- `background: true` shows a single flash line, not a pager.
- `background: false` suspends k9s and hands the terminal to the command.

The client runs on every shortcut press. A Python client would pay interpreter startup (and
possibly SDK import cost) each time.

## Decision

- The client is a small Go binary at `cmd/k9sai/` (`package main`, a subdirectory of the
  cobra `cmd` package).
- Plugins run it in the foreground. It streams the response into `less -R`.
- If the daemon socket is missing, the client starts the daemon.
- Client and daemon talk HTTP over a Unix socket (mode `0600`, under
  `$XDG_RUNTIME_DIR/k9sai/`), streaming with Server-Sent Events.
- When the pager exits, the client closes the connection, and the daemon cancels generation.

## Alternatives considered

- **A Python client.** Rejected because of startup latency on every keypress.
- **Newline-delimited JSON over a raw socket.** Rejected: it has no standard tooling, and we would
  have to invent our own cancellation.
- **The daemon in a separate repo.** Rejected: the fork already holds both halves, and
  new top-level directories rarely conflict on upstream rebases.

## Consequences

- The protocol can be debugged with `curl --unix-socket`.
- Go's `net/http` can dial Unix sockets. The same client code can back a future Tier 2 native
  pane inside k9s.
- There are two languages and two test suites in CI.
