from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


class AgentSandbox(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


@dataclass(frozen=True)
class AgentTurnRequest:
    cwd: Path
    developer_instructions: str
    instruction: str
    model: str | None
    sandbox: AgentSandbox
    thread_id: str | None = None


@dataclass(frozen=True)
class AgentTurnResult:
    final_response: str


class AgentBackend(Protocol):
    async def run_turn(self, request: AgentTurnRequest) -> AgentTurnResult:
        """Run one AI-worker turn and return the final response text."""


class CodexAgentBackend:
    """Default AgentBackend implementation backed by the Codex Python SDK."""

    async def run_turn(self, request: AgentTurnRequest) -> AgentTurnResult:
        try:
            from openai_codex import AsyncCodex, Sandbox
        except ImportError as exc:
            raise RuntimeError(
                "openai-codex is not installed. Install dependencies with uv or pip before running live Codex tasks."
            ) from exc

        sdk_sandbox = _to_codex_sandbox(request.sandbox, Sandbox)
        async with AsyncCodex() as codex:
            if request.thread_id:
                thread = await codex.thread_resume(
                    request.thread_id,
                    cwd=request.cwd,
                    developer_instructions=request.developer_instructions,
                    model=request.model,
                    sandbox=sdk_sandbox,
                )
            else:
                thread = await codex.thread_start(
                    cwd=request.cwd,
                    developer_instructions=request.developer_instructions,
                    model=request.model,
                    sandbox=sdk_sandbox,
                )

            result = await thread.run(
                request.instruction,
                sandbox=sdk_sandbox,
                model=request.model,
            )

        return AgentTurnResult(final_response=getattr(result, "final_response", "") or "")


def _to_codex_sandbox(sandbox: AgentSandbox, sdk_sandbox_type):
    if sandbox == AgentSandbox.READ_ONLY:
        return sdk_sandbox_type.read_only
    if sandbox == AgentSandbox.WORKSPACE_WRITE:
        return sdk_sandbox_type.workspace_write
    raise ValueError(f"Unsupported agent sandbox: {sandbox}")
