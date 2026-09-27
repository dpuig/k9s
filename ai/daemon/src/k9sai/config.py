"""Config loading and endpoint trust (ADR-0004).

A non-loopback base_url is refused at load time unless it is listed in
allow_endpoints. Hostnames other than "localhost" are never resolved: a name
that happens to point at 127.0.0.1 still has to be allowlisted.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

TASKS = ("ask", "logs", "diagnose")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Backend:
    name: str
    type: str  # "openai" | "litert"
    model: str = ""
    base_url: str = ""
    model_path: str = ""


@dataclass
class Config:
    default: str
    backends: dict[str, Backend]
    tasks: dict[str, str] = field(default_factory=dict)
    allow_endpoints: list[str] = field(default_factory=list)
    budget_tokens: int = 6000
    max_tool_calls: int = 3
    idle_minutes: int = 30

    def backend_for(self, task: str) -> Backend:
        return self.backends[self.tasks.get(task, self.default)]


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "k9sai" / "config.yaml"


def is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalize(url: str) -> str:
    return url.rstrip("/")


def parse(data: dict) -> Config:
    try:
        backends = {name: Backend(name=name, **spec) for name, spec in (data.get("backends") or {}).items()}
    except TypeError as e:
        raise ConfigError(f"invalid backend spec: {e}") from e
    cfg = Config(
        default=data.get("default", ""),
        backends=backends,
        tasks=data.get("tasks") or {},
        allow_endpoints=[_normalize(u) for u in data.get("allow_endpoints") or []],
        budget_tokens=int(data.get("budget_tokens", 6000)),
        max_tool_calls=int(data.get("max_tool_calls", 3)),
        idle_minutes=int(data.get("idle_minutes", 30)),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.default not in cfg.backends:
        raise ConfigError(f"default backend {cfg.default!r} is not defined")
    for task, name in cfg.tasks.items():
        if task not in TASKS:
            raise ConfigError(f"unknown task {task!r}; expected one of {TASKS}")
        if name not in cfg.backends:
            raise ConfigError(f"task {task!r} routes to undefined backend {name!r}")
    for b in cfg.backends.values():
        if b.type == "openai":
            if not (b.base_url and b.model):
                raise ConfigError(f"backend {b.name!r}: openai needs base_url and model")
            url = _normalize(b.base_url)
            if not is_loopback(url) and url not in cfg.allow_endpoints:
                raise ConfigError(
                    f"backend {b.name!r}: {b.base_url} is not loopback and not in "
                    "allow_endpoints; refusing to send cluster data there"
                )
        elif b.type == "litert":
            if not b.model_path:
                raise ConfigError(f"backend {b.name!r}: litert needs model_path")
        else:
            raise ConfigError(f"backend {b.name!r}: unknown type {b.type!r}")


def load(path: Path | None = None) -> Config:
    path = path or default_path()
    if not path.exists():
        raise ConfigError(f"no config at {path}; copy ai/daemon/config.example.yaml there")
    return parse(yaml.safe_load(path.read_text()) or {})
