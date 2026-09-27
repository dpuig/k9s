"""The only module that imports the Antigravity SDK (ADR-0001).

build_config() is where the harness lockdown lives. The SDK's harness ships builtin
tools (run_command, view_file, edit_file, read_url_content, ...) and `.lightweight()`
turns run_command/view_file/create_file/edit_file ON by default. We pass
enabled_tools=[] explicitly and re-check it after presets are applied, so an SDK
upgrade that changes preset semantics fails loudly instead of handing the model a shell.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

from k9sai.config import Backend


@dataclass
class Event:
    kind: str  # "token" | "thought" | "tool"
    text: str


class AgentBackend(Protocol):
    def stream(self, system: str, prompt: str, tools: list[Callable[..., str]]) -> AsyncIterator[Event]: ...


class UnsafeConfigError(RuntimeError):
    pass


def build_config(b: Backend, system: str, tools: list[Callable[..., str]]):
    from google.antigravity import CapabilitiesConfig, LiteRTAgentConfig, LocalOpenAIAgentConfig
    from google.antigravity.hooks.policy import allow, deny

    common = dict(
        system_instructions=system,
        tools=tools or None,
        capabilities=CapabilitiesConfig(enabled_tools=[], enable_subagents=False),
        policies=[deny("*"), *(allow(t.__name__) for t in tools)],
        workspaces=[],
    )
    if b.type == "openai":
        cfg = LocalOpenAIAgentConfig(model=b.model, base_url=b.base_url, **common)
    else:
        cfg = LiteRTAgentConfig(model_path=os.path.expanduser(b.model_path), **common)
    cfg = cfg.lightweight()
    caps = cfg.capabilities
    if caps is None or caps.enabled_tools != [] or caps.enable_subagents:
        raise UnsafeConfigError(f"SDK presets re-enabled builtin tools: {caps!r}")
    return cfg


class SDKBackend:
    def __init__(self, backend: Backend):
        self.backend = backend

    async def stream(self, system, prompt, tools):
        from google.antigravity import Agent
        from google.antigravity.types import Text, Thought, ToolCall

        async with Agent(build_config(self.backend, system, tools)) as agent:
            response = await agent.chat(prompt)
            async for chunk in response.chunks:
                if isinstance(chunk, Text):
                    yield Event("token", chunk.text)
                elif isinstance(chunk, Thought):
                    yield Event("thought", chunk.text)
                elif isinstance(chunk, ToolCall):
                    yield Event("tool", getattr(chunk, "name", "") or "tool")


class StubBackend:
    """Scripted backend for tests and fixture replay without a model."""

    def __init__(self, reply: str | Callable[[str], str], tool_calls: list[tuple[str, dict]] = ()):
        self.reply, self.tool_calls = reply, list(tool_calls)
        self.prompts: list[str] = []

    async def stream(self, system, prompt, tools):
        self.prompts.append(prompt)
        by_name = {t.__name__: t for t in tools}
        extra = []
        for name, args in self.tool_calls:
            yield Event("tool", name)
            extra.append(by_name[name](**args))
        text = self.reply(prompt + "\n".join(extra)) if callable(self.reply) else self.reply
        for word in text.split(" "):
            yield Event("token", word + " ")
