"""Per-trial metrics for single-query (Part 3) and general-usage (Part 4) evaluation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from eval_lib.agent import AGGREGATE_TOOL, AgentResult, AggregateCall
from eval_lib.comparator import Comparison, compare_rows, is_ordered

FAILURE_TAGS = (
    "wrong_result", "wrong_index", "tool_error", "timeout",
    "no_gradable_output", "max_tool_calls", "multiple_aggregate",
)


@dataclass
class SingleScore:
    passed: bool
    failure_tags: list[str]
    resources_consumed: int
    aggregate_revision_count: int
    scored: Optional[AggregateCall]
    comparison: Optional[Comparison]
    timing_delta_ms: Optional[float]
    reason: str


def resources_before_first_aggregate(result: AgentResult) -> int:
    """Resource reads before the first aggregate call, repeats included."""
    count = 0
    for event in result.events:
        if event.kind == "tool" and event.name == AGGREGATE_TOOL:
            break
        if event.kind == "resource":
            count += 1
    return count


def score_single(result: AgentResult, index: str, gt_rows: list[list[Any]], gt_final_sql: str,
                 gt_duration_ms: float) -> SingleScore:
    calls = result.aggregate_calls
    tags: list[str] = []
    if len(calls) > 1:
        tags.append("multiple_aggregate")
    if result.stop_reason == "timeout":
        tags.append("timeout")
    if result.stop_reason == "max_tool_calls":
        tags.append("max_tool_calls")
    wrong_index = sorted({c.index for c in calls if c.index != index}, key=str)
    if wrong_index:
        tags.append("wrong_index")

    successful = [c for c in calls if c.ok]
    scored = successful[-1] if successful else None
    comparison = None
    timing = None
    if scored is None:
        tags.append("tool_error" if calls else "no_gradable_output")
        reason = "no successful aggregate call" if calls else "agent made no aggregate call"
    else:
        timing = round(scored.duration_ms - gt_duration_ms, 3)
        if scored.truncated:
            comparison = Comparison(False, is_ordered(gt_final_sql), None, "agent result was truncated")
        else:
            comparison = compare_rows(gt_rows, scored.rows, is_ordered(gt_final_sql))
        reason = comparison.reason
        if not comparison.passed:
            tags.append("wrong_result")
    if wrong_index:
        reason = f"aggregate call on wrong index {wrong_index}; {reason}"

    passed = comparison is not None and comparison.passed and not wrong_index
    return SingleScore(
        passed=passed,
        failure_tags=[t for t in FAILURE_TAGS if t in tags],
        resources_consumed=resources_before_first_aggregate(result),
        aggregate_revision_count=max(len(calls) - 1, 0),
        scored=scored,
        comparison=comparison,
        timing_delta_ms=timing,
        reason=reason,
    )


@dataclass
class GeneralMetrics:
    executions: list[int]
    execution_time_ms: float
    redundancies: list[list[Any]] = field(default_factory=list)


def general_metrics(result: AgentResult) -> GeneralMetrics:
    resource_reads = sum(1 for e in result.events if e.kind == "resource")
    tool_calls = sum(1 for e in result.events if e.kind == "tool")
    counts = Counter(e.name for e in result.events)
    redundancies = [[name, n - 1] for name, n in sorted(counts.items()) if n > 1]
    return GeneralMetrics(
        executions=[resource_reads, tool_calls],
        execution_time_ms=round(result.duration_ms, 3),
        redundancies=redundancies,
    )
