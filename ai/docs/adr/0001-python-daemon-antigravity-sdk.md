# ADR-0001: Python daemon built on the Antigravity SDK

Status: Accepted, 2026-09-26

## Context

k9s is written in Go. The original proposal ran the model behind a long-lived Python daemon
built on Google's Antigravity SDK (`google-antigravity`, 0.1.x, Apache-2.0).

During design we first chose the opposite approach: a Go-only CLI calling an OpenAI-compatible
endpoint directly. Ollama, vLLM and LM Studio already keep the model warm, so the daemon's
"keep the model loaded" argument didn't hold on its own, and Phase 1 pre-gathers evidence
anyway.

We reversed that choice. From the SDK README and local-models docs, the SDK provides:

- typed custom tools (`tools=[fn]`)
- a policy engine (`deny("*")`, `allow(...)`, `ask_user(...)`)
- token streaming
- two local backends: `LiteRTAgentConfig` (in-process Gemma) and
  `LocalOpenAIAgentConfig` (any OpenAI-compatible server)

Building the tool loop and policy engine ourselves in Go would duplicate that work.

## Decision

- The model side runs as a Python daemon at `ai/daemon/`, using the Antigravity SDK.
- Both SDK backends are supported. `LocalOpenAIAgentConfig` is built and evaluated first.
- The SDK is pinned to an exact version and accessed only through an internal
  `AgentBackend` interface.
- The daemon is started on demand by the client and exits after 30 minutes idle.

## Consequences

- We gain a real agent loop and a declarative policy layer for the read-only tools
  ([ADR-0003](0003-hybrid-evidence.md)).
- A Python runtime, a socket protocol and a second kubeconfig consumer become part of the
  system ([ADR-0002](0002-go-client-http-over-unix-socket.md)).
- The SDK is new and 0.x, so its APIs will move. The adapter interface keeps the blast radius
  to one module, and the stub-model CI tests catch breakage on upgrades.
- The SDK docs don't promise tool calling on local models. We mitigate this by
  pre-gathering evidence instead of depending on tool calls.
- LiteRT with Gemma 26B needs ≥ 24 GB of memory. The idle exit releases it when the tool is unused.

## Addendum (implementation, 2026-09-26): the SDK harness ships builtin tools

The SDK runs a separate native binary (`localharness`, ~118 MB) that executes tools and calls
the model. That harness has **builtin tools**: `run_command`, `view_file`, `create_file`,
`edit_file`, `read_url_content`, `search_web`, subagents and more. `.lightweight()`, which the
SDK docs recommend for local models, **enables `run_command`, `view_file`, `create_file` and
`edit_file` by default**. The original proposal's sketch (`.lightweight()` plus custom
policies) would have given a prompt-injectable model a shell, and access to
`~/.kube/config`, that bypasses our Kubernetes guard entirely.

`k9sai.backend.build_config` is therefore the only place an agent config is built. It:

- passes `CapabilitiesConfig(enabled_tools=[], enable_subagents=False)` and `workspaces=[]`;
- sets `policies=[deny("*"), allow(<our tool>)...]` as a second layer;
- re-checks `enabled_tools == []` *after* `.lightweight()` presets are applied, and raises
  `UnsafeConfigError` otherwise, so an SDK upgrade that changes preset semantics fails loudly.
  A unit test simulates exactly that regression.

We verified this against Ollama in a live test. Asked to call `run_command` and `view_file`, the model
got "unknown tool" from the harness, and no file was written. During the same run, the harness
made loopback connections only (to the Python process and to Ollama on `:11434`).
`k9sai-daemon check-config` runs the lockdown check for every configured backend.
