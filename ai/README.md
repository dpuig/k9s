# k9sai

Local AI assistance inside k9s: diagnose failing workloads, summarize logs, and translate
questions into k9s commands. It uses a model endpoint you control and has read-only cluster access.
See [DESIGN.md](DESIGN.md) for the design and [docs/adr/](docs/adr/) for the decisions behind it.

## Setup

Requirements: Go (to build the client), [uv](https://docs.astral.sh/uv/), and an
OpenAI-compatible model server such as Ollama.

```sh
make -C ai install                      # go install k9sai + uv tool install k9sai-daemon
mkdir -p ~/.config/k9sai
cp ai/daemon/config.example.yaml ~/.config/k9sai/config.yaml   # then edit the model
make -C ai check-config                 # validates config and the SDK tool lockdown
```

Then merge [`plugins/k9sai.yaml`](plugins/k9sai.yaml) into your k9s `plugins.yaml`.
To find where that file lives, run `k9s info`.

| Key | Where | What |
|---|---|---|
| `Shift-D` | pods, deployments, statefulsets, daemonsets, replicasets | Diagnose: ranked causes citing `[E#]` evidence lines |
| `Shift-L` | pods, containers | Log summary: drain3 templates plus what is new in the last 10 minutes |
| `Shift-Q` | any view | Ask: prints one k9s command and copies it to the clipboard |

Answers stream into `less`. Press `q` to go back to k9s, which also stops generation. The daemon starts
on first use and exits after 30 idle minutes. Its log is `~/.local/state/k9sai/daemon.log`.

The client also works outside k9s:

```sh
k9sai diagnose --context kind-dev -n shop --resource deployments web
k9sai logs -n shop web-5d8f7-abcde
k9sai ask -n shop "pods restarting since the last deploy"
k9sai capture -n shop web-5d8f7-abcde -o fixture.json    # redacted evidence, for evals
k9sai status
```

## Safety, in short

- **Read-only.** Every API call passes a guard that allows only get, list and watch. The only exceptions are
  server-side dry-runs and access reviews, and the model can't call either. Secrets are never fetched.
- **No builtin tools.** The SDK harness's shell and file tools are disabled, and this is re-checked on every
  config build.
- **Redacted.** Tokens, keys, connection strings and sensitive env values are scrubbed before
  anything reaches a model.
- **Loopback by default.** Any endpoint that isn't loopback must be listed in `allow_endpoints`.
- **Cited.** Claims must cite evidence ids. Invented ids are flagged in a ⚠️ footer.

## Development

```sh
make -C ai test          # Python unit tests + Go client tests (no model, no cluster)
make -C ai lint          # ruff + golangci-lint
make -C ai eval          # 20 kind scenarios × tools on/off against your configured model
make -C ai eval-replay   # grade committed fixtures only; no cluster needed
```

`make eval` only ever touches its own kind cluster (`kind-k9sai-eval`). Reports go to
`eval/reports/` and should be committed, so quality can be tracked over time.
