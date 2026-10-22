"""Agent drivers: OpenRouter (OpenAI-compatible tool calling) and a ground-truth replay stub."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from eval_lib.agent import (
    AGGREGATE_TOOL, AgentResult, AggregateCall, EvalConstraints, StopReason, TrialContext,
)
from eval_lib.mcp_session import CallOutcome, Telemetry

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-opus-5.5"
READ_RESOURCE_TOOL = "read_resource"
TOOL_RESULT_CHARS = 20_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace local "$ref": "#/$defs/Name" pointers with the definitions they name and drop $defs."""
    defs = schema.get("$defs", {})

    def resolve(node: Any, seen: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [resolve(item, seen) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            if name in seen:
                raise ValueError(f"recursive schema reference {name!r} cannot be inlined")
            if name not in defs:
                raise ValueError(f"schema reference {ref!r} has no definition")
            target = resolve(copy.deepcopy(defs[name]), seen + (name,))
            siblings = {k: resolve(v, seen) for k, v in node.items() if k != "$ref"}
            return {**target, **siblings}
        return {k: resolve(v, seen) for k, v in node.items() if k != "$defs"}

    return resolve(schema, ())


def openai_tools(mcp_tools: list[Any], resources: list[str], templates: list[str]) -> list[dict[str, Any]]:
    """MCP tool listings as OpenAI function tools, plus a read_resource function for MCP resources."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": inline_refs(tool.input_schema),
            },
        }
        for tool in mcp_tools
    ]
    listing = ", ".join(resources + templates)
    tools.append({
        "type": "function",
        "function": {
            "name": READ_RESOURCE_TOOL,
            "description": (
                f"Read a GUFI MCP resource by URI. Available resources: {listing}. "
                "Template parameters in braces are filled in, e.g. gufi://schemas/all or gufi://schemas/vrpentries."
            ),
            "parameters": {
                "type": "object",
                "properties": {"uri": {"type": "string", "description": "Resource URI, starting with gufi://"}},
                "required": ["uri"],
                "additionalProperties": False,
            },
        },
    })
    return tools


def system_prompt(context: TrialContext, constraints: EvalConstraints, instructions: str = "") -> str:
    """Host-style system prompt: role, the server's MCP instructions, then task constraints."""
    lines = [
        "You are a file system metadata assistant. You answer questions using a GUFI MCP server, "
        "which indexes file system metadata into SQLite databases. Its tools are available as functions; "
        f"its resources are read with the {READ_RESOURCE_TOOL} function.",
    ]
    if instructions.strip():
        lines += ["", "Instructions from the GUFI MCP server:", instructions.strip(), ""]
    lines += [
        f"The index to use is {context.index!r}.",
        f"The current date and time is {context.fixed_now_iso} (Unix time {context.fixed_now}). "
        "Use this value as 'now' and do date arithmetic inside SQL from it.",
    ]
    if constraints.mode == "single":
        lines.append(
            f"Answer with {AGGREGATE_TOOL}: the rows of your last successful {AGGREGATE_TOOL} call are taken as "
            "your answer, so that call's result must contain exactly the requested answer and nothing else. "
            "When it does, reply with a short final message and stop calling tools."
        )
    else:
        lines.append("When you have the answer, reply with your final answer as a message and stop calling tools.")
    return "\n".join(lines)


def _aggregate_call(arguments: dict[str, Any], outcome: CallOutcome, t_ms: float) -> AggregateCall:
    call = AggregateCall(t_ms=t_ms, query=arguments.get("query"), ok=outcome.ok,
                         duration_ms=round(outcome.duration_ms, 3), error=outcome.error)
    data = outcome.structured or {}
    if outcome.ok:
        call.columns = list(data.get("columns", []))
        call.rows = [list(r) for r in data.get("rows", [])]
        call.row_count = int(data.get("row_count", len(call.rows)))
        call.truncated = bool(data.get("truncated", False))
    return call


class ReplayDriver:
    """Stub agent that submits the scenario's ground truth as its only aggregate call."""

    name = "replay"

    def config(self) -> dict[str, Any]:
        return {"agent": self.name}

    async def run(self, prompt: str, context: TrialContext, constraints: EvalConstraints) -> AgentResult:
        telemetry = Telemetry()
        started = _now_iso()
        calls: list[AggregateCall] = []
        if context.ground_truth is not None and AGGREGATE_TOOL in constraints.allowed_tools:
            arguments = {"query": context.ground_truth}
            t_ms = telemetry.elapsed_ms()
            outcome = await context.server.call_tool(AGGREGATE_TOOL, arguments, telemetry)
            calls.append(_aggregate_call(arguments, outcome, t_ms))
            final = outcome.text
        else:
            final = "The replay agent has no ground truth to replay for this scenario."
        return AgentResult(
            final_text=final, aggregate_calls=calls, events=telemetry.events, started_at=started,
            finished_at=_now_iso(), duration_ms=round(telemetry.elapsed_ms(), 3), stop_reason="final",
        )


class OpenRouterDriver:
    """Tool-calling loop against OpenRouter's OpenAI-compatible chat completions API."""

    name = "openrouter"

    def __init__(self, model: str = DEFAULT_MODEL, temperature: Optional[float] = None,
                 max_completion_tokens: Optional[int] = 4096, api_key: Optional[str] = None):
        from openai import AsyncOpenAI

        key = api_key or os.getenv("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set (put it in .env)")
        self.model = model
        self.temperature = temperature
        self.max_completion_tokens = max_completion_tokens
        self._client = AsyncOpenAI(
            api_key=key, base_url=OPENROUTER_BASE_URL, max_retries=3,
            default_headers={"X-Title": "gufi-mcp-eval"},
        )

    def config(self) -> dict[str, Any]:
        return {
            "agent": self.name, "model": self.model, "temperature": self.temperature,
            "max_completion_tokens": self.max_completion_tokens, "base_url": OPENROUTER_BASE_URL,
            "tool_result_chars": TOOL_RESULT_CHARS,
        }

    async def run(self, prompt: str, context: TrialContext, constraints: EvalConstraints) -> AgentResult:
        server = context.server
        telemetry = Telemetry()
        started = _now_iso()
        mcp_tools = [t for t in await server.list_tools() if t.name in constraints.allowed_tools]
        resources, templates = await server.list_resources()
        tools = openai_tools(mcp_tools, resources, templates)
        allowed = {t.name for t in mcp_tools}

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(context, constraints, server.instructions)},
            {"role": "user", "content": prompt},
        ]
        calls: list[AggregateCall] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0, "requests": 0}
        state: dict[str, Any] = {"final": "", "stop": "final", "error": None, "tool_calls": 0}

        async def loop() -> None:
            while True:
                kwargs: dict[str, Any] = {
                    "model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto",
                    "extra_body": {"usage": {"include": True}},
                }
                if self.temperature is not None:
                    kwargs["temperature"] = self.temperature
                if self.max_completion_tokens:
                    kwargs["max_completion_tokens"] = self.max_completion_tokens
                response = await self._client.chat.completions.create(**kwargs)
                _add_usage(usage, response)
                if not response.choices:
                    raise RuntimeError(f"model returned no choices: {response}")
                message = response.choices[0].message
                tool_calls = list(message.tool_calls or [])
                entry: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
                if tool_calls:
                    entry["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                        for tc in tool_calls
                    ]
                messages.append(entry)
                if message.content:
                    state["final"] = message.content
                if not tool_calls:
                    return
                for tc in tool_calls:
                    if state["tool_calls"] >= constraints.max_tool_calls:
                        state["stop"] = "max_tool_calls"
                        return
                    state["tool_calls"] += 1
                    content = await self._dispatch(tc.function.name, tc.function.arguments, context,
                                                   allowed, telemetry, calls)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        try:
            async with asyncio.timeout(constraints.timeout_s):
                await loop()
        except TimeoutError:
            state["stop"] = "timeout"
        except Exception as e:  # API failures end the trial; they are reported, not raised
            state["stop"] = "error"
            state["error"] = f"{type(e).__name__}: {e}"

        stop: StopReason = state["stop"]
        return AgentResult(
            final_text=state["final"], aggregate_calls=calls, events=telemetry.events, started_at=started,
            finished_at=_now_iso(), duration_ms=round(telemetry.elapsed_ms(), 3), stop_reason=stop,
            error=state["error"], usage=usage, transcript=messages,
        )

    async def _dispatch(self, name: str, raw_arguments: Optional[str], context: TrialContext, allowed: set[str],
                        telemetry: Telemetry, calls: list[AggregateCall]) -> str:
        """Run one model tool call and return the text to send back to the model."""
        try:
            arguments = json.loads(raw_arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            outcome = CallOutcome(ok=False, text=f"Invalid tool arguments: {e}", structured=None, duration_ms=0,
                                  error=str(e))
            telemetry.record("tool", name, raw_arguments or "", outcome, time.monotonic())
            return outcome.text

        if name == READ_RESOURCE_TOOL:
            uri = arguments.get("uri")
            if not isinstance(uri, str):
                outcome = CallOutcome(ok=False, text="read_resource needs a string uri", structured=None,
                                      duration_ms=0, error="missing uri")
                telemetry.record("resource", str(uri), arguments, outcome, time.monotonic())
            else:
                outcome = await context.server.read_resource(uri, telemetry)
        elif name in allowed:
            t_ms = telemetry.elapsed_ms()
            outcome = await context.server.call_tool(name, arguments, telemetry)
            if name == AGGREGATE_TOOL:
                calls.append(_aggregate_call(arguments, outcome, t_ms))
        else:
            outcome = CallOutcome(ok=False, text=f"Unknown tool: {name}", structured=None, duration_ms=0,
                                  error="unknown tool")
            telemetry.record("tool", name, arguments, outcome, time.monotonic())

        text = outcome.text
        if len(text) > TOOL_RESULT_CHARS:
            text = text[:TOOL_RESULT_CHARS] + f"\n... [output cut to {TOOL_RESULT_CHARS} of {len(outcome.text)} characters]"
            telemetry.events[-1].note = f"result cut to {TOOL_RESULT_CHARS} of {len(outcome.text)} characters for the model"
        return text


def _add_usage(usage: dict[str, Any], response: Any) -> None:
    usage["requests"] += 1
    u = getattr(response, "usage", None)
    if u is None:
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        usage[key] += getattr(u, key, 0) or 0
    cost = getattr(u, "cost", None)
    if cost is None and getattr(u, "model_extra", None):
        cost = u.model_extra.get("cost")
    if isinstance(cost, (int, float)):
        usage["cost"] = round(usage["cost"] + cost, 8)
