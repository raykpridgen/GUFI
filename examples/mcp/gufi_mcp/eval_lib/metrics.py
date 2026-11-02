"""Per-trial metrics for single-query (Part 3) and general-usage (Part 4) evaluation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from eval_lib.agent import AGGREGATE_TOOL, AgentResult, AggregateCall
from eval_lib.comparator import Comparison, compare_rows, is_ordered

FAILURE_TAGS = (
    "wrong_result", "wrong_index", "tool_error", "timeout",
    "no_gradable_output", "max_tool_calls", "max_attempts", "replaced_match", "multiple_aggregate",
)


@dataclass
class Attempt:
    """One graded aggregate call: attempt is 1-based, comparison is None when the call failed."""

    attempt: int
    call: AggregateCall
    comparison: Optional[Comparison]
    passed: bool
    reason: str


@dataclass
class SingleScore:
    passed: bool
    passed_on_attempt: Optional[int]
    one_shot_pass: bool
    failure_tags: list[str]
    resources_consumed: int
    aggregate_revision_count: int
    attempts: list[Attempt]
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


def grade_call(n: int, call: AggregateCall, index: str, gt_rows: list[list[Any]], ordered: bool) -> Attempt:
    """Compare one aggregate call with the ground truth rows."""
    if not call.ok:
        return Attempt(n, call, None, False, f"call failed: {call.error or 'error'}")
    if call.truncated:
        comparison = Comparison(False, ordered, None, "agent result was truncated")
    else:
        comparison = compare_rows(gt_rows, call.rows, ordered)
    if call.index != index:
        return Attempt(n, call, comparison, False, f"wrong index {call.index!r}; {comparison.reason}")
    return Attempt(n, call, comparison, comparison.passed, comparison.reason)


def score_single(result: AgentResult, index: str, gt_rows: list[list[Any]], gt_final_sql: str,
                 gt_duration_ms: float, max_attempts: Optional[int] = None) -> SingleScore:
    """Grade the last successful call among the first max_attempts aggregate calls.

    Every aggregate call counts as an attempt, failed ones included. A failed call does not replace an
    earlier answer, but a later successful call does, even when an earlier one matched (tagged
    replaced_match).
    """
    calls = result.aggregate_calls
    graded = calls[:max_attempts] if max_attempts else calls
    ordered = is_ordered(gt_final_sql)
    attempts = [grade_call(n, call, index, gt_rows, ordered) for n, call in enumerate(graded, 1)]

    tags: list[str] = []
    if len(calls) > 1:
        tags.append("multiple_aggregate")
    if result.stop_reason in ("timeout", "max_tool_calls", "max_attempts"):
        tags.append(result.stop_reason)

    gradable = [a for a in attempts if a.comparison is not None]
    shown = gradable[-1] if gradable else None
    passed = shown is not None and shown.passed
    if shown is None:
        tags.append("tool_error" if graded else "no_gradable_output")
        reason = "no successful aggregate call" if graded else "agent made no aggregate call"
    else:
        if shown.call.index != index:
            tags.append("wrong_index")
        elif not passed:
            tags.append("wrong_result")
        if not passed and any(a.passed for a in gradable):
            tags.append("replaced_match")
        reason = f"{shown.reason} (attempt {shown.attempt} of {len(graded)})"

    return SingleScore(
        passed=passed,
        passed_on_attempt=shown.attempt if passed else None,
        one_shot_pass=passed and len(calls) == 1,
        failure_tags=[t for t in FAILURE_TAGS if t in tags],
        resources_consumed=resources_before_first_aggregate(result),
        aggregate_revision_count=max(len(calls) - 1, 0),
        attempts=attempts,
        scored=shown.call if shown else None,
        comparison=shown.comparison if shown else None,
        timing_delta_ms=round(shown.call.duration_ms - gt_duration_ms, 3) if shown else None,
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
