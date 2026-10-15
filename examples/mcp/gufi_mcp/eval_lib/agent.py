"""Agent driver interface (eval_design.md Part 6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol

from eval_lib.mcp_session import GufiMcpServer, TraceEvent

AGGREGATE_TOOL = "aggregate_sql_query"
StopReason = Literal["final", "max_tool_calls", "timeout", "error"]


@dataclass
class EvalConstraints:
    mode: Literal["single", "general"]
    allowed_tools: list[str]
    max_tool_calls: int = 15
    timeout_s: float = 180.0
    max_aggregate_calls: Optional[int] = None


@dataclass
class TrialContext:
    """What a driver needs besides the prompt: the server, the index, and the reference time."""

    server: GufiMcpServer
    index: str
    fixed_now: int
    fixed_now_iso: str
    ground_truth: Optional[dict[str, Any]] = None


@dataclass
class AggregateCall:
    """One aggregate_sql_query call made by the agent."""

    t_ms: float
    query: Any
    ok: bool
    duration_ms: float
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    error: Optional[str] = None

    @property
    def index(self) -> Optional[str]:
        return self.query.get("index") if isinstance(self.query, dict) else None


@dataclass
class AgentResult:
    final_text: str
    aggregate_calls: list[AggregateCall]
    events: list[TraceEvent]
    started_at: str
    finished_at: str
    duration_ms: float
    stop_reason: StopReason
    error: Optional[str] = None
    usage: dict[str, Any] = field(default_factory=dict)
    transcript: list[dict[str, Any]] = field(default_factory=list)


class AgentDriver(Protocol):
    name: str

    def config(self) -> dict[str, Any]:
        """Settings that identify this agent; hashed into the run manifest. Must not contain secrets."""
        ...

    async def run(self, prompt: str, context: TrialContext, constraints: EvalConstraints) -> AgentResult: ...
