"""Long-lived GUFI MCP server sessions over stdio, with per-trial telemetry."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent.parent
EVAL_SERVER = HERE / "gufi_eval_server.py"
ARGS_SUMMARY_CHARS = 500


class TraceEvent(BaseModel):
    """One tool call or resource read, as listed in eval_design.md Part 5."""

    t_ms: float
    kind: Literal["resource", "tool"]
    name: str
    args_summary: str
    ok: bool
    duration_ms: float
    error: Optional[str] = None
    note: Optional[str] = None


@dataclass
class CallOutcome:
    """What a tool call or resource read returned."""

    ok: bool
    text: str
    structured: Optional[dict[str, Any]]
    duration_ms: float
    error: Optional[str] = None


@dataclass
class Telemetry:
    """Ordered events for one trial; t_ms is relative to the trial start."""

    started: float = field(default_factory=time.monotonic)
    events: list[TraceEvent] = field(default_factory=list)

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000

    def record(self, kind: str, name: str, args: Any, outcome: CallOutcome, t_start: float) -> TraceEvent:
        summary = args if isinstance(args, str) else json.dumps(args, sort_keys=True, default=str)
        if len(summary) > ARGS_SUMMARY_CHARS:
            summary = summary[:ARGS_SUMMARY_CHARS] + "..."
        event = TraceEvent(
            t_ms=round((t_start - self.started) * 1000, 3), kind=kind, name=name, args_summary=summary,
            ok=outcome.ok, duration_ms=round(outcome.duration_ms, 3), error=outcome.error,
        )
        self.events.append(event)
        return event


def _content_text(content: list[Any]) -> str:
    return "\n".join(getattr(item, "text", "") or "" for item in content)


class GufiMcpServer:
    """One gufi_eval_server.py subprocess and its client session, shared by every trial in a run.

    tools is "all" or a comma-separated allowlist passed to the server as GUFI_EVAL_TOOLS.
    """

    def __init__(self, tools: str = "all", read_timeout_seconds: float = 600):
        self.tools = tools
        self.read_timeout_seconds = read_timeout_seconds
        self._stack: Optional[AsyncExitStack] = None
        self._client: Optional[Client] = None

    async def __aenter__(self) -> "GufiMcpServer":
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(EVAL_SERVER)],
            env={**os.environ, "GUFI_EVAL_TOOLS": self.tools},
            cwd=str(HERE),
        )
        self._stack = AsyncExitStack()
        self._client = await self._stack.enter_async_context(
            Client(params, read_timeout_seconds=self.read_timeout_seconds)
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = self._client = None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("GufiMcpServer is not started; use it as an async context manager")
        return self._client

    @property
    def instructions(self) -> str:
        """The server's MCP instructions (its primer), sent at initialize."""
        return self.client.instructions or ""

    async def list_tools(self) -> list[Any]:
        return list((await self.client.list_tools()).tools)

    async def list_resources(self) -> tuple[list[str], list[str]]:
        """Static resource URIs and resource templates."""
        resources = [str(r.uri) for r in (await self.client.list_resources()).resources]
        templates = [t.uri_template for t in (await self.client.list_resource_templates()).resource_templates]
        return resources, templates

    async def call_tool(self, name: str, arguments: dict[str, Any], telemetry: Optional[Telemetry] = None) -> CallOutcome:
        start = time.monotonic()
        try:
            result = await self.client.call_tool(name, arguments)
            text = _content_text(result.content)
            outcome = CallOutcome(
                ok=not result.is_error, text=text, structured=result.structured_content,
                duration_ms=(time.monotonic() - start) * 1000, error=text[:2000] if result.is_error else None,
            )
        except Exception as e:  # transport or protocol failure: report it to the agent like a tool error
            message = f"{type(e).__name__}: {e}"
            outcome = CallOutcome(ok=False, text=message, structured=None,
                                  duration_ms=(time.monotonic() - start) * 1000, error=message)
        if telemetry is not None:
            telemetry.record("tool", name, arguments, outcome, start)
        return outcome

    async def read_resource(self, uri: str, telemetry: Optional[Telemetry] = None) -> CallOutcome:
        start = time.monotonic()
        try:
            result = await self.client.read_resource(uri, cache_mode="bypass")
            text = _content_text(result.contents)
            outcome = CallOutcome(ok=True, text=text, structured=None, duration_ms=(time.monotonic() - start) * 1000)
        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            outcome = CallOutcome(ok=False, text=message, structured=None,
                                  duration_ms=(time.monotonic() - start) * 1000, error=message)
        if telemetry is not None:
            telemetry.record("resource", uri, uri, outcome, start)
        return outcome
