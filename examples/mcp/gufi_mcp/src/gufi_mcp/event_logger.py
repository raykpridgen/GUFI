"""
Module 7: JSONL event logger for MCP tool/resource/prompt dispatch.

Records server-observable events with duration_ms, gap_since_prev_ms, plan_hash,
and compiled_sql (for pipeline tools) per design §10.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext

_LOGGED_METHODS = frozenset({"tools/call", "resources/read", "prompts/get"})

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_plain_data(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_plain_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain_data(item) for item in value]
    if hasattr(value, "model_dump"):
        return _to_plain_data(value.model_dump(mode="json", exclude_none=True))
    if hasattr(value, "dict"):
        try:
            return _to_plain_data(value.dict())
        except TypeError:
            pass
    return repr(value)


def _normalize_handler_result(result: HandlerResult) -> Any:
    if result is None:
        return None
    if isinstance(result, dict):
        return result
    if hasattr(result, "model_dump"):
        data = result.model_dump(mode="json", exclude_none=True)
        structured = data.get("structuredContent")
        if structured is not None:
            return structured
        return data
    return _to_plain_data(result)


def _truncate_rows(payload: dict[str, Any], max_rows: int = 20) -> dict[str, Any]:
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) <= max_rows:
        return payload
    copy = dict(payload)
    copy["rows"] = rows[:max_rows]
    copy["rows_truncated_in_log"] = True
    copy["rows_total_logged"] = len(rows)
    return copy


def _extract_correlation_fields(tool_name: str | None, output: Any) -> dict[str, Any]:
    if not isinstance(output, dict):
        return {}

    fields: dict[str, Any] = {}
    if "plan_hash" in output:
        fields["plan_hash"] = output["plan_hash"]
    if "compiled_sql" in output:
        fields["compiled_sql"] = output["compiled_sql"]
    if tool_name == "execute_query_plan":
        if "elapsed_ms" in output:
            fields["execute_elapsed_ms"] = output["elapsed_ms"]
        if "row_count" in output:
            fields["row_count"] = output["row_count"]
    return fields


def _dispatch_name(method: str, params: dict[str, Any] | None) -> tuple[str, str, dict[str, Any]]:
    params = params or {}
    if method == "tools/call":
        name = str(params.get("name", "unknown_tool"))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {"value": _to_plain_data(arguments)}
        return "tool", name, arguments
    if method == "resources/read":
        uri = str(params.get("uri", "unknown_resource"))
        return "resource", uri, {"uri": uri}
    if method == "prompts/get":
        name = str(params.get("name", "unknown_prompt"))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {"value": _to_plain_data(arguments)}
        return "prompt", name, arguments
    return "request", method, dict(params)


@dataclass
class EventLogger:
    """Append-only JSONL sink for MCP dispatch events."""

    log_path: Path
    _lock: threading.Lock = threading.Lock()
    _last_event_monotonic: float | None = None

    @classmethod
    def from_path(cls, path: Path | str | None) -> EventLogger | None:
        if path is None:
            return None
        raw = str(path).strip()
        if not raw or raw.lower() in {"0", "false", "off", "none"}:
            return None
        return cls(Path(raw))

    def record(
        self,
        *,
        event_type: str,
        name: str,
        input_data: Any,
        output_data: Any,
        duration_ms: float,
        success: bool = True,
        error: str | None = None,
    ) -> dict[str, Any]:
        now = time.monotonic()
        gap_ms = 0.0 if self._last_event_monotonic is None else (now - self._last_event_monotonic) * 1000
        self._last_event_monotonic = now

        output_plain = _normalize_handler_result(output_data)
        if isinstance(output_plain, dict):
            output_plain = _truncate_rows(output_plain)

        tool_name = name if event_type == "tool" else None
        correlation = _extract_correlation_fields(tool_name, output_plain if isinstance(output_plain, dict) else {})

        event: dict[str, Any] = {
            "timestamp": _utc_now_iso(),
            "event_type": event_type,
            "name": name,
            "duration_ms": round(duration_ms, 2),
            "gap_since_prev_ms": round(gap_ms, 2),
            "success": success,
            "input": _to_plain_data(input_data),
            "output": output_plain,
        }
        if error:
            event["error"] = error
        event.update(correlation)

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, separators=(",", ":"), default=str)
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return event

    def read_events(self) -> list[dict[str, Any]]:
        if not self.log_path.is_file():
            return []
        events: list[dict[str, Any]] = []
        with self.log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def events_for_plan_hash(self, plan_hash_value: str) -> list[dict[str, Any]]:
        return [
            event
            for event in self.read_events()
            if event.get("plan_hash") == plan_hash_value
        ]


class EventLoggingMiddleware:
    """MCP ServerMiddleware that writes JSONL events for tools/resources/prompts."""

    def __init__(self, logger: EventLogger) -> None:
        self._logger = logger

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if ctx.method not in _LOGGED_METHODS:
            return await call_next(ctx)

        params = dict(ctx.params or {})
        event_type, name, input_data = _dispatch_name(ctx.method, params)
        started = time.perf_counter()
        try:
            result = await call_next(ctx)
            duration_ms = (time.perf_counter() - started) * 1000
            self._logger.record(
                event_type=event_type,
                name=name,
                input_data=input_data,
                output_data=result,
                duration_ms=duration_ms,
                success=True,
            )
            return result
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            self._logger.record(
                event_type=event_type,
                name=name,
                input_data=input_data,
                output_data=None,
                duration_ms=duration_ms,
                success=False,
                error=str(exc),
            )
            raise


def attach_event_logging(mcp_server: Any, logger: EventLogger | None) -> EventLogger | None:
    """Register JSONL logging middleware on an MCPServer instance."""
    if logger is None:
        return None
    mcp_server.middleware.append(EventLoggingMiddleware(logger))
    return logger


def log_pipeline_step(
    logger: EventLogger,
    tool_name: str,
    arguments: dict[str, Any],
    handler: Any,
) -> Any:
    """Sync helper for tests/gates: invoke a pipeline handler and log one event."""
    started = time.perf_counter()
    try:
        result = handler(**arguments)
        duration_ms = (time.perf_counter() - started) * 1000
        logger.record(
            event_type="tool",
            name=tool_name,
            input_data=arguments,
            output_data=result,
            duration_ms=duration_ms,
            success=True,
        )
        return result
    except Exception as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.record(
            event_type="tool",
            name=tool_name,
            input_data=arguments,
            output_data=None,
            duration_ms=duration_ms,
            success=False,
            error=str(exc),
        )
        raise
