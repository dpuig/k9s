# ADR-0004: Always redact; refuse non-loopback endpoints unless allowlisted

Status: Accepted, 2026-09-26

## Context

k9sai routes each task to a configurable backend, which can be any OpenAI-compatible
endpoint. The same binary might talk to localhost, a GPU box on the LAN, or a hosted API.
The original promise that "nothing leaves the machine" therefore depends on configuration.

Logs are the most sensitive input: they routinely contain tokens and connection strings.

## Decision

- **Always redact the hard secrets**, whatever the endpoint:
  - Secret `data`/`stringData` is never read.
  - Tokens, private keys, connection strings and auth headers are scrubbed.
- Redaction is applied in our code at the single point every piece of evidence passes through:
  the context builder and every tool result. It is not done in SDK hooks.
- Email addresses are not scrubbed, which keeps diagnoses readable for a personal tool.
- **Loopback endpoints are always allowed.** Any other `base_url` is refused at startup
  unless it is listed in `allow_endpoints` in `config.yaml`.

## Alternatives considered

- **Redact only for non-loopback endpoints.** This saves little, and redaction bugs would stay
  hidden until the day an endpoint changes.
- **No allowlist.** A single config typo could send production logs to a hosted API.

## Consequences

- Sending data off-machine is always an explicit, reviewable config change.
- The redactor is a critical component, covered by unit tests in CI with realistic secret
  shapes.
- Redaction can remove text that would have helped a diagnosis. We accept that loss.
