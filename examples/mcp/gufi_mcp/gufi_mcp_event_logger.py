"""JSONL event logging for the GUFI MCP server.

The logger records server-observable MCP tool/resource/prompt dispatch. Each
server process gets its own log file by default, while GUFI_MCP_EVENT_LOG can
override the destination for eval runs or disable logging.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext


_LOGGED_METHODS = frozenset({"tools/call", "resources/read", "prompts/get"})
_PROJECT_DIR = Path(__file__).resolve().parent


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _invocation_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_pid{os.getpid()}"


def default_log_path(invocation_id: str) -> Path:
    return _PROJECT_DIR / "logs" / "server" / f"{invocation_id}.jsonl"


def resolve_log_path(raw_path: str | Path | None, invocation_id: str) -> Path | None:
    """Resolve GUFI_MCP_EVENT_LOG into a concrete JSONL path."""
    if raw_path is None:
        return default_log_path(invocation_id)

    raw = str(raw_path).strip().strip("'\"")
    if raw.lower() in {"", "0", "false", "off", "none"}:
        return None

    path = Path(raw)
    if path.suffix:
        return path
    return path / f"{invocation_id}.jsonl"


def _to_plain_data(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _to_plain_data(item) for key, item in value.items()}
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
        structured = result.get("structuredContent")
        if structured is not None:
            return _to_plain_data(structured)
        return _to_plain_data(result)
    if hasattr(result, "model_dump"):
        data = result.model_dump(mode="json", exclude_none=True)
        structured = data.get("structuredContent")
        if structured is not None:
            return _to_plain_data(structured)
        return _to_plain_data(data)
    return _to_plain_data(result)


def _truncate_rows(payload: dict[str, Any], max_rows: int = 20) -> dict[str, Any]:
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) <= max_rows:
        return payload

    output = dict(payload)
    output["rows"] = rows[:max_rows]
    output["rows_truncated_in_log"] = True
    output["rows_total_logged"] = len(rows)
    return output


def _extract_correlation_fields(name: str | None, output: Any) -> dict[str, Any]:
    if not isinstance(output, dict):
        return {}

    fields: dict[str, Any] = {}
    for key in ("row_count", "error", "sql", "compiled_sql", "plan_hash", "warning", "execution_mode", "requested_limit"):
        if key in output and output[key] is not None:
            fields[key] = output[key]
    if name == "execute_query_plan" and "elapsed_ms" in output:
        fields["execute_elapsed_ms"] = output["elapsed_ms"]
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
    """Append-only JSONL sink for MCP invocation events."""

    log_path: Path
    invocation_id: str = field(default_factory=_invocation_id)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_event_monotonic: float | None = None

    @classmethod
    def from_env(cls) -> EventLogger | None:
        invocation_id = _invocation_id()
        path = resolve_log_path(os.getenv("GUFI_MCP_EVENT_LOG"), invocation_id)
        if path is None:
            return None
        return cls(path, invocation_id=invocation_id)

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

        output = _normalize_handler_result(output_data)
        if isinstance(output, dict):
            output = _truncate_rows(output)
            if output.get("error"):
                success = False

        correlation = _extract_correlation_fields(name if event_type == "tool" else None, output)

        event: dict[str, Any] = {
            "timestamp": _utc_now_iso(),
            "invocation_id": self.invocation_id,
            "pid": os.getpid(),
            "event_type": event_type,
            "name": name,
            "duration_ms": round(duration_ms, 2),
            "gap_since_prev_ms": round(gap_ms, 2),
            "success": success,
            "input": _to_plain_data(input_data),
            "output": output,
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


class EventLoggingMiddleware:
    """MCP middleware that records tool/resource/prompt dispatch events."""

    def __init__(self, logger: EventLogger) -> None:
        self._logger = logger

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if ctx.method not in _LOGGED_METHODS:
            return await call_next(ctx)

        event_type, name, input_data = _dispatch_name(ctx.method, dict(ctx.params or {}))
        started = time.perf_counter()
        try:
            result = await call_next(ctx)
            self._logger.record(
                event_type=event_type,
                name=name,
                input_data=input_data,
                output_data=result,
                duration_ms=(time.perf_counter() - started) * 1000,
                success=True,
            )
            return result
        except Exception as exc:
            self._logger.record(
                event_type=event_type,
                name=name,
                input_data=input_data,
                output_data=None,
                duration_ms=(time.perf_counter() - started) * 1000,
                success=False,
                error=str(exc),
            )
            raise


def attach_event_logging(mcp_server: Any, logger: EventLogger | None) -> EventLogger | None:
    """Attach event logging middleware to an MCPServer instance."""
    if logger is None:
        return None
    mcp_server.middleware.append(EventLoggingMiddleware(logger))
    return logger
