"""Run artifacts under eval_runs/<run_id>/ (eval_design.md Part 5)."""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from eval_lib.agent import AgentResult, AggregateCall
from eval_lib.metrics import GeneralMetrics, SingleScore

# Every summary.jsonl line has exactly these keys, in both modes.
SUMMARY_KEYS = (
    "scenario_id", "trial", "mode", "pass",
    "resources_consumed", "timing_delta_ms", "similarity_path",
    "executions", "execution_time_ms", "redundancies", "final_response_path",
    "aggregate_revision_count", "failure_tags",
    "agent", "model", "stop_reason", "duration_ms", "usage", "trace_path", "error",
)
MAX_ROWS_LOGGED = 1000


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o)))


def _call_summary(call: AggregateCall) -> dict[str, Any]:
    return {
        "t_ms": call.t_ms, "query": call.query, "ok": call.ok, "duration_ms": call.duration_ms,
        "columns": call.columns, "row_count": call.row_count, "truncated": call.truncated, "error": call.error,
    }


class RunWriter:
    def __init__(self, output: Path, run_id: Optional[str] = None):
        self.run_id = run_id or new_run_id()
        self.root = output / self.run_id
        self.root.mkdir(parents=True, exist_ok=False)
        self.summary_path = self.root / "summary.jsonl"
        self.summary_path.touch()

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def write_json(self, rel: str, data: Any) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_jsonable(data), indent=2) + "\n")
        return path

    def write_text(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def append_summary(self, line: dict[str, Any]) -> None:
        missing = set(SUMMARY_KEYS) ^ set(line)
        if missing:
            raise ValueError(f"summary line keys differ from SUMMARY_KEYS: {sorted(missing)}")
        with self.summary_path.open("a") as f:
            f.write(json.dumps(_jsonable({k: line[k] for k in SUMMARY_KEYS})) + "\n")

    def trace(self, mode: str, scenario_id: str, trial: int, result: AgentResult) -> Path:
        return self.write_json(f"{mode}/{scenario_id}/trial_{trial}.trace.json", {
            "scenario": scenario_id, "trial": trial, "stop_reason": result.stop_reason, "error": result.error,
            "started_at": result.started_at, "finished_at": result.finished_at, "duration_ms": result.duration_ms,
            "usage": result.usage,
            "events": [e.model_dump() for e in result.events],
            "aggregate_calls": [_call_summary(c) for c in result.aggregate_calls],
            "final_text": result.final_text,
            "transcript": result.transcript,
        })


def _base_line(scenario_id: str, trial: int, mode: str, result: AgentResult, agent: dict[str, Any],
               trace_path: str) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id, "trial": trial, "mode": mode, "pass": None,
        "resources_consumed": None, "timing_delta_ms": None, "similarity_path": "",
        "executions": None, "execution_time_ms": None, "redundancies": [], "final_response_path": "",
        "aggregate_revision_count": None, "failure_tags": [],
        "agent": agent.get("agent"), "model": agent.get("model"), "stop_reason": result.stop_reason,
        "duration_ms": result.duration_ms, "usage": result.usage, "trace_path": trace_path, "error": result.error,
    }


def record_single(writer: RunWriter, scenario_id: str, trial: int, result: AgentResult, score: SingleScore,
                  ground_truth: dict[str, Any], gt_rows: list[list[Any]], gt_duration_ms: float,
                  agent: dict[str, Any]) -> dict[str, Any]:
    trace = writer.trace("single", scenario_id, trial, result)
    scored = score.scored
    last = result.aggregate_calls[-1] if result.aggregate_calls else None
    similarity = writer.write_json(f"single/{scenario_id}/trial_{trial}.similarity.json", {
        "scenario": scenario_id,
        "trial": trial,
        "pass": score.passed,
        "aggregate_revision_count": score.aggregate_revision_count,
        "ground_truth": ground_truth,
        "agent": (scored or last).query if (scored or last) else None,
        "agent_attempts": [c.query for c in result.aggregate_calls],
        "gt_row_count": len(gt_rows),
        "agent_row_count": scored.row_count if scored else 0,
        "ordered_compare": score.comparison.ordered if score.comparison else None,
        "column_permutation": score.comparison.column_permutation if score.comparison else None,
        "reason": score.reason,
    })
    comparison = score.comparison
    compared = comparison is not None and (comparison.expected or comparison.actual)
    writer.write_json(f"single/{scenario_id}/trial_{trial}.rows.json", {
        "ordered_compare": comparison.ordered if comparison else None,
        "row_order": "compared order (both sides sorted)" if compared and not comparison.ordered
        else "compared order (as returned)" if compared else "raw",
        "expected": [list(r) for r in comparison.expected[:MAX_ROWS_LOGGED]] if compared else gt_rows[:MAX_ROWS_LOGGED],
        "actual": [list(r) for r in comparison.actual[:MAX_ROWS_LOGGED]] if compared
        else scored.rows[:MAX_ROWS_LOGGED] if scored else [],
        "missing_from_actual": [list(r) for r in comparison.missing[:MAX_ROWS_LOGGED]] if comparison else [],
        "unexpected_in_actual": [list(r) for r in comparison.extra[:MAX_ROWS_LOGGED]] if comparison else [],
        "expected_row_count": len(gt_rows),
        "actual_row_count": scored.row_count if scored else 0,
        "gt_duration_ms": gt_duration_ms,
        "agent_duration_ms": scored.duration_ms if scored else None,
    })
    line = _base_line(scenario_id, trial, "single", result, agent, writer.rel(trace))
    line.update({
        "pass": score.passed,
        "resources_consumed": score.resources_consumed,
        "timing_delta_ms": score.timing_delta_ms,
        "similarity_path": writer.rel(similarity),
        "aggregate_revision_count": score.aggregate_revision_count,
        "failure_tags": score.failure_tags,
    })
    writer.append_summary(line)
    return line


def record_general(writer: RunWriter, scenario_id: str, trial: int, result: AgentResult, metrics: GeneralMetrics,
                   agent: dict[str, Any]) -> dict[str, Any]:
    trace = writer.trace("general", scenario_id, trial, result)
    response = writer.write_text(f"general/{scenario_id}/trial_{trial}.response.txt", result.final_text + "\n")
    line = _base_line(scenario_id, trial, "general", result, agent, writer.rel(trace))
    line.update({
        **asdict(metrics),
        "final_response_path": writer.rel(response),
    })
    writer.append_summary(line)
    return line
