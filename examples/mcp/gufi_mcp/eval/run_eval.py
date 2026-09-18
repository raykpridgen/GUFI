#!/usr/bin/env python3
"""Run live model evaluations against GUFI MCP implementation arms.

Usage examples:
  python eval/run_eval.py
  python eval/run_eval.py --suite-name smoke --limit 3
  python eval/run_eval.py --resume eval/runs/20260918_041500_gufi_mcp_eval

The runner is intentionally sequential and stateful so it can be launched in
the background and resumed after interruption. It expects a GUFI MCP server to
already be running and reads OPENROUTER_API_KEY from .env or the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp import Client


EVAL_DIR = Path(__file__).resolve().parent
PROJECT_DIR = EVAL_DIR.parent
DEFAULT_RUNS_DIR = EVAL_DIR / "runs"
DEFAULT_MCP_URL = "http://127.0.0.1:8000/mcp"
STATE_FILE = "suite_state.json"

GRADER_MODEL = "openai/gpt-4o-mini-2024-07-18"
PARTICIPANT_MODELS = [
    "inclusionai/ling-3.0-flash-vl:free",
    "nex-agi/nex-n2.5-mini:free",
    "qwen/qwen3.8-27b:free",
    "deepseek/deepseek-v4-flash-0731:free",
]

IMPLEMENTATIONS = {
    "base": {
        "description": "Minimal MCP access: sql_file_index and static naive schema only.",
        "tools": ["sql_file_index", "read_naive_index_scheme"],
        "auto_prompt": None,
    },
    "simple": {
        "description": "SQL plus dynamic schema resource.",
        "tools": ["sql_file_index", "read_gufi_schema"],
        "auto_prompt": None,
    },
    "advanced": {
        "description": "Full MCP suite with command wrappers, resources, aggregate SQL, and briefing.",
        "tools": [
            "gufi_ls",
            "gufi_du",
            "gufi_find",
            "gufi_stat",
            "gufi_stats",
            "sql_file_index",
            "aggregate_sql_query",
            "read_gufi_indexes",
            "read_gufi_schema",
        ],
        "auto_prompt": "gufi_session_briefing",
    },
}

IMPLEMENTATION_ALIASES = {
    "basic": "base",
}

PROMPTS = [
    {
        "id": "basic_listing",
        "kind": "simple",
        "text": (
            "Using the personal_data index, find up to 10 regular files whose "
            "names contain \"organizer\". Return the path/name and size for each result."
        ),
        "expected_tool": "gufi_find or sql_file_index",
    },
    {
        "id": "largest_files",
        "kind": "simple",
        "text": (
            "Using the personal_data index, show the 10 largest regular files. "
            "Return name, path if available, and size, sorted largest first."
        ),
        "expected_tool": "sql_file_index",
    },
    {
        "id": "directory_summary",
        "kind": "simple",
        "text": (
            "For the personal_data index, determine whether summary or treesummary "
            "information is available and use the best available method to summarize "
            "the total size or file count at a directory level."
        ),
        "expected_tool": "gufi://indexes plus gufi_stats, gufi_du, or sql_file_index",
    },
    {
        "id": "aggregate_total_size",
        "kind": "aggregate",
        "text": (
            "Using the personal_data index, calculate the total byte size of all "
            "regular files with the aggregate query tool. Use the intermediate and "
            "aggregate table flow, and return the final total."
        ),
        "expected_tool": "aggregate_sql_query",
    },
    {
        "id": "aggregate_uid_totals",
        "kind": "aggregate",
        "text": (
            "Using the personal_data index, group regular files by uid and calculate "
            "total bytes per uid. Return the top 10 uid totals in descending byte order."
        ),
        "expected_tool": "aggregate_sql_query",
    },
]


@dataclass
class RunSpec:
    implementation: str
    model: str
    prompt_id: str

    @property
    def run_id(self) -> str:
        return f"{self.implementation}__{safe_name(self.model)}__{self.prompt_id}"


class OpenRouterClient:
    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.url = "https://openrouter.ai/api/v1/chat/completions"

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if response_format:
            body["response_format"] = response_format

        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/mar-file-system/GUFI",
                "X-Title": "GUFI MCP Evaluation",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value).strip("_")


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(data, sort_keys=True) + "\n")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def token_estimate(messages: list[dict[str, Any]]) -> int:
    """Cheap, stable estimate good enough for enforcing a suite cutoff."""
    return sum(len(json.dumps(message, sort_keys=True)) for message in messages) // 4


def mcp_text_part(value: Any) -> str:
    """Extract text from one MCP content object or nested content value."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(mcp_text_part(item) for item in value)
    text = getattr(value, "text", None)
    if text is not None:
        return str(text)
    content = getattr(value, "content", None)
    if content is not None:
        return mcp_text_part(content)
    return str(value)


def content_text(result: Any) -> str:
    """Extract text from MCP result objects without depending on exact classes."""
    if hasattr(result, "content") and result.content:
        return mcp_text_part(result.content)
    if hasattr(result, "contents") and result.contents:
        return mcp_text_part(result.contents)
    if hasattr(result, "messages") and result.messages:
        return mcp_text_part([getattr(message, "content", message) for message in result.messages])
    return str(result)


def tool_schema(name: str) -> dict[str, Any]:
    schemas: dict[str, dict[str, Any]] = {
        "sql_file_index": {
            "description": "Run logical SQL against a GUFI index. Use raw table names from the schema, such as pentries or vrpentries; the server maps them to gufi_vt functions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sqlin": {"type": "string", "description": "SELECT and optional FROM portion."},
                    "wherein": {"type": "string", "description": "Optional FROM/WHERE/ORDER/LIMIT portion."},
                    "index": {"type": "string", "description": "Index name, usually personal_data."},
                    "remote": {"type": "boolean", "default": False},
                },
                "required": ["sqlin", "wherein", "index"],
            },
        },
        "aggregate_sql_query": {
            "description": "Run GUFI aggregate SQL phases through the generic gufi_vt virtual table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "string"},
                            "config": {"type": "array", "items": {"type": "string"}},
                            "sql_options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "option": {
                                            "type": "string",
                                            "enum": ["-I", "-T", "-S", "-E", "-K", "-J", "-G", "-F"],
                                        },
                                        "sql": {"type": "string"},
                                    },
                                    "required": ["option", "sql"],
                                },
                            },
                        },
                        "required": ["index", "sql_options"],
                    }
                },
                "required": ["query"],
            },
        },
        "read_naive_index_scheme": {
            "description": "Read the static file-backed GUFI schema notes for the Base implementation.",
            "parameters": {"type": "object", "properties": {}},
        },
        "read_gufi_indexes": {
            "description": "Read available GUFI indexes and whether treesummary is present.",
            "parameters": {"type": "object", "properties": {}},
        },
        "read_gufi_schema": {
            "description": "Read all GUFI schemas or one specific schema.",
            "parameters": {
                "type": "object",
                "properties": {
                    "schema": {
                        "type": "string",
                        "description": "Schema name such as all, entries, pentries, vrpentries, summary, or vrsummary.",
                        "default": "all",
                    }
                },
            },
        },
        "gufi_ls": {
            "description": "Run gufi_ls for quick directory-style listing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "gufi_du": {
            "description": "Run gufi_du for disk-usage style summaries.",
            "parameters": {
                "type": "object",
                "properties": {"options": {"type": "array", "items": {"type": "string"}}},
            },
        },
        "gufi_find": {
            "description": "Run gufi_find for find-style metadata searches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "gufi_stat": {
            "description": "Run gufi_stat for a single file/path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["file"],
            },
        },
        "gufi_stats": {
            "description": "Run gufi_stats for higher-level GUFI statistics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "stat": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["stat"],
            },
        },
    }
    spec = schemas[name]
    return {"type": "function", "function": {"name": name, **spec}}


async def execute_agent_tool(client: Client, name: str, arguments: dict[str, Any]) -> str:
    if name == "read_naive_index_scheme":
        return content_text(await client.read_resource("gufi://naive_indexes"))
    if name == "read_gufi_indexes":
        return content_text(await client.read_resource("gufi://indexes"))
    if name == "read_gufi_schema":
        schema = arguments.get("schema") or "all"
        return content_text(await client.read_resource(f"gufi://schemas/{schema}"))

    return content_text(await client.call_tool(name, arguments))


def system_message(implementation: str) -> str:
    impl = IMPLEMENTATIONS[implementation]
    return (
        "You are an evaluation participant using a GUFI MCP server. "
        "Answer the user's task by using only the exposed tools when data is needed. "
        "Prefer the simplest tool that can answer correctly, keep output bounded, "
        "and give a concise final answer. "
        f"Implementation arm: {implementation}. {impl['description']}"
    )


def run_status_path(run_dir: Path) -> Path:
    return run_dir / "run.json"


def load_run_status(run_dir: Path) -> dict[str, Any] | None:
    path = run_status_path(run_dir)
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def truncate_text(value: Any, max_chars: int) -> Any:
    if not isinstance(value, str) or len(value) <= max_chars:
        return value
    return value[:max_chars] + f"\n... [truncated {len(value) - max_chars} chars]"


def compact_messages_for_grader(messages: list[dict[str, Any]], max_content_chars: int = 4000) -> list[dict[str, Any]]:
    compacted = []
    for message in messages:
        item = dict(message)
        item["content"] = truncate_text(item.get("content"), max_content_chars)
        compacted.append(item)
    return compacted


def structured_result_error(result_text: str) -> str | None:
    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and parsed.get("error"):
        return str(parsed["error"])
    return None


def structured_result_warning(result_text: str) -> str | None:
    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and parsed.get("warning"):
        return str(parsed["warning"])
    return None


async def run_participant(
    *,
    spec: RunSpec,
    prompt: dict[str, Any],
    suite_dir: Path,
    mcp_url: str,
    openrouter: OpenRouterClient,
    timeout_seconds: int,
    tool_timeout_seconds: int,
    max_tool_calls_per_run: int,
    token_cutoff: int,
    max_response_tokens: int,
) -> dict[str, Any]:
    run_dir = suite_dir / spec.implementation / safe_name(spec.model) / spec.prompt_id
    messages_path = run_dir / "messages.json"
    calls_path = run_dir / "calls.jsonl"

    started = time.monotonic()
    started_at = now_iso()
    status = {
        "run_id": spec.run_id,
        "implementation": spec.implementation,
        "model": spec.model,
        "prompt_id": spec.prompt_id,
        "prompt": prompt["text"],
        "expected_tool": prompt["expected_tool"],
        "status": "in_progress",
        "started_at": started_at,
        "ended_at": None,
        "elapsed_ms": None,
        "final_answer": "",
        "error": None,
        "tool_calls": 0,
        "resource_calls": 0,
        "prompt_calls": 0,
        "approx_tokens": 0,
    }
    write_json(run_status_path(run_dir), status)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_message(spec.implementation)}
    ]
    impl = IMPLEMENTATIONS[spec.implementation]
    tools = [tool_schema(name) for name in impl["tools"]]

    async with Client(mcp_url) as mcp_client:
        if impl["auto_prompt"]:
            prompt_name = impl["auto_prompt"]
            prompt_start = time.monotonic()
            briefing = content_text(await mcp_client.get_prompt(prompt_name))
            elapsed_ms = int((time.monotonic() - prompt_start) * 1000)
            messages.append({"role": "system", "content": briefing})
            append_jsonl(
                calls_path,
                {
                    "kind": "prompt",
                    "name": prompt_name,
                    "arguments": {},
                    "elapsed_ms": elapsed_ms,
                    "success": True,
                    "timestamp": now_iso(),
                },
            )
            status["prompt_calls"] += 1

        messages.append({"role": "user", "content": prompt["text"]})
        write_json(messages_path, messages)

        try:
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= timeout_seconds:
                    status["status"] = "timed_out"
                    status["error"] = f"Exceeded timeout of {timeout_seconds} seconds"
                    break

                status["approx_tokens"] = token_estimate(messages)
                if status["approx_tokens"] >= token_cutoff:
                    status["status"] = "token_limited"
                    status["error"] = f"Exceeded approximate token cutoff of {token_cutoff}"
                    break
                if status["tool_calls"] >= max_tool_calls_per_run:
                    status["status"] = "tool_limited"
                    status["error"] = f"Exceeded tool-call cutoff of {max_tool_calls_per_run}"
                    break

                remaining_tokens = max(256, min(max_response_tokens, token_cutoff - status["approx_tokens"]))
                remaining_time = max(1.0, timeout_seconds - elapsed)
                response = openrouter.chat(
                    model=spec.model,
                    messages=messages,
                    tools=tools,
                    max_tokens=remaining_tokens,
                    timeout=min(openrouter.timeout, remaining_time),
                )

                choice = response["choices"][0]
                message = choice["message"]
                messages.append(message)
                write_json(messages_path, messages)

                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    status["status"] = "completed"
                    status["final_answer"] = message.get("content") or ""
                    break

                for tool_call in tool_calls:
                    function = tool_call["function"]
                    tool_name = function["name"]
                    raw_args = function.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = {}

                    call_started = time.monotonic()
                    success = True
                    error_type = None
                    try:
                        remaining_run_time = max(1.0, timeout_seconds - (time.monotonic() - started))
                        result_text = await asyncio.wait_for(
                            execute_agent_tool(mcp_client, tool_name, args),
                            timeout=min(tool_timeout_seconds, remaining_run_time),
                        )
                        if result_text.startswith("Error executing tool"):
                            success = False
                            error_type = "mcp_tool_error"
                        elif result_text.startswith("Error reading resource"):
                            success = False
                            error_type = "mcp_resource_error"
                        elif structured_result_error(result_text):
                            success = False
                            error_type = "tool_result_error"
                    except TimeoutError:
                        success = False
                        error_type = "tool_timeout"
                        result_text = f"Tool call timed out after {tool_timeout_seconds} seconds"
                    except Exception as exc:
                        success = False
                        error_type = type(exc).__name__
                        result_text = f"{type(exc).__name__}: {exc}"
                    call_elapsed_ms = int((time.monotonic() - call_started) * 1000)

                    kind = "resource" if tool_name.startswith("read_") else "tool"
                    status["resource_calls" if kind == "resource" else "tool_calls"] += 1
                    append_jsonl(
                        calls_path,
                        {
                            "kind": kind,
                            "name": tool_name,
                            "arguments": args,
                            "elapsed_ms": call_elapsed_ms,
                            "success": success,
                            "error_type": error_type,
                            "result_preview": result_text[:2000],
                            "timestamp": now_iso(),
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": tool_name,
                            "content": result_text,
                        }
                    )
                    write_json(messages_path, messages)
                    write_json(run_status_path(run_dir), status)

        except Exception as exc:
            status["status"] = "errored"
            status["error"] = f"{type(exc).__name__}: {exc}"
            status["traceback"] = traceback.format_exc()

    status["ended_at"] = now_iso()
    status["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    status["approx_tokens"] = token_estimate(messages)
    write_json(messages_path, messages)
    write_json(run_status_path(run_dir), status)
    return status


def grade_run(
    *,
    openrouter: OpenRouterClient,
    run_status: dict[str, Any],
    run_dir: Path,
    implementation_description: str,
    max_tokens: int,
) -> dict[str, Any]:
    messages_path = run_dir / "messages.json"
    if messages_path.is_file():
        messages = json.loads(messages_path.read_text())
    else:
        messages = [
            {"role": "system", "content": system_message(run_status["implementation"])},
            {"role": "user", "content": run_status["prompt"]},
        ]
    calls = []
    calls_path = run_dir / "calls.jsonl"
    if calls_path.is_file():
        calls = [json.loads(line) for line in calls_path.read_text().splitlines() if line.strip()]

    grader_prompt = {
        "task": run_status["prompt"],
        "expected_tool": run_status["expected_tool"],
        "implementation": run_status["implementation"],
        "implementation_description": implementation_description,
        "completion_status": run_status["status"],
        "elapsed_ms": run_status["elapsed_ms"],
        "final_answer": run_status["final_answer"],
        "messages": compact_messages_for_grader(messages),
        "calls": calls,
        "rubric": {
            "correctness": 45,
            "tool_use": 25,
            "gufi_understanding": 15,
            "efficiency": 10,
            "clarity": 5,
        },
    }
    grader_messages = [
        {
            "role": "system",
            "content": (
                "You are grading GUFI MCP agent evaluation runs. Return only JSON "
                "with keys: score, correctness, tool_use, gufi_understanding, "
                "efficiency, clarity, completed, used_expected_tool, "
                "major_failure_mode, reasoning. Treat failed, redundant, or timed-out "
                "tool calls as evidence for tool_use and efficiency scores. Penalize "
                "accepting shard-local sql_file_index results when warning is present "
                "for global top-N, ORDER BY, GROUP BY, or aggregate questions."
            ),
        },
        {"role": "user", "content": json.dumps(grader_prompt, indent=2)},
    ]
    response = openrouter.chat(
        model=GRADER_MODEL,
        messages=grader_messages,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    content = response["choices"][0]["message"].get("content") or "{}"
    try:
        grade = json.loads(content)
    except json.JSONDecodeError:
        grade = {
            "score": 0,
            "correctness": 0,
            "tool_use": 0,
            "gufi_understanding": 0,
            "efficiency": 0,
            "clarity": 0,
            "completed": run_status["status"] == "completed",
            "used_expected_tool": False,
            "major_failure_mode": "grader_json_parse_failed",
            "reasoning": content,
        }

    write_json(run_dir / "grade.json", grade)
    return grade


def build_specs(args: argparse.Namespace) -> list[RunSpec]:
    implementations = [
        IMPLEMENTATION_ALIASES.get(implementation, implementation)
        for implementation in (args.implementation or list(IMPLEMENTATIONS))
    ]
    models = args.model or PARTICIPANT_MODELS
    requested_prompt_ids = set(args.prompt) if args.prompt else None
    prompt_ids = [
        prompt["id"]
        for prompt in PROMPTS
        if requested_prompt_ids is None or prompt["id"] in requested_prompt_ids
    ]
    prompts_by_id = {prompt["id"]: prompt for prompt in PROMPTS}

    specs = []
    for implementation in implementations:
        if implementation not in IMPLEMENTATIONS:
            raise ValueError(f"Unknown implementation: {implementation}")
        for model in models:
            for prompt_id in prompt_ids:
                if prompt_id not in prompts_by_id:
                    raise ValueError(f"Unknown prompt id: {prompt_id}")
                specs.append(RunSpec(implementation, model, prompt_id))
    return specs


def prompt_by_id(prompt_id: str) -> dict[str, Any]:
    for prompt in PROMPTS:
        if prompt["id"] == prompt_id:
            return prompt
    raise KeyError(prompt_id)


def create_or_resume_suite(args: argparse.Namespace, specs: list[RunSpec]) -> Path:
    if args.resume:
        suite_dir = Path(args.resume).resolve()
        if not (suite_dir / STATE_FILE).is_file():
            raise FileNotFoundError(f"Resume state not found: {suite_dir / STATE_FILE}")
        return suite_dir

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suite_name = safe_name(args.suite_name)
    suite_dir = (args.runs_dir / f"{stamp}_{suite_name}").resolve()
    suite_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        suite_dir / STATE_FILE,
        {
            "suite_name": args.suite_name,
            "created_at": now_iso(),
            "mcp_url": args.mcp_url,
            "timeout_seconds": args.timeout_seconds,
            "token_cutoff": args.token_cutoff,
            "grader_model": GRADER_MODEL,
            "specs": [asdict(spec) for spec in specs],
        },
    )
    return suite_dir


def summarize_suite(suite_dir: Path) -> None:
    grades = []
    statuses = []
    for run_json in suite_dir.glob("*/*/*/run.json"):
        statuses.append(json.loads(run_json.read_text()))
        grade_path = run_json.parent / "grade.json"
        if grade_path.is_file():
            grades.append(json.loads(grade_path.read_text()))

    summary = {
        "updated_at": now_iso(),
        "run_count": len(statuses),
        "completed_count": sum(1 for status in statuses if status.get("status") == "completed"),
        "graded_count": len(grades),
        "average_score": (
            sum(float(grade.get("score", 0)) for grade in grades) / len(grades)
            if grades
            else None
        ),
        "status_counts": {},
    }
    for status in statuses:
        key = status.get("status", "unknown")
        summary["status_counts"][key] = summary["status_counts"].get(key, 0) + 1
    write_json(suite_dir / "summary.json", summary)


async def check_mcp_server(mcp_url: str) -> str | None:
    try:
        async with Client(mcp_url) as client:
            await client.list_tools()
        return None
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        return f"{type(exc).__name__}: {exc}"


async def run_suite(args: argparse.Namespace) -> int:
    load_dotenv(PROJECT_DIR / ".env")
    specs = build_specs(args)
    if args.limit:
        specs = specs[: args.limit]

    if args.dry_run:
        print(f"MCP server: {args.mcp_url}")
        print(f"Runs selected: {len(specs)}")
        for spec in specs:
            print(f"DRY RUN {spec.run_id}")
        return 0

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is not set in the environment or .env", file=sys.stderr)
        return 2

    mcp_error = await check_mcp_server(args.mcp_url)
    if mcp_error:
        print(f"MCP server is not reachable at {args.mcp_url}", file=sys.stderr)
        print(f"Connection error: {mcp_error}", file=sys.stderr)
        print("Start the GUFI MCP server first, then rerun this command.", file=sys.stderr)
        return 3

    suite_dir = create_or_resume_suite(args, specs)
    print(f"Suite directory: {suite_dir}")
    print(f"MCP server: {args.mcp_url}")
    print(f"Runs requested this invocation: {len(specs)}")

    openrouter = OpenRouterClient(api_key, timeout=args.request_timeout)
    completed_this_invocation = 0

    for spec in specs:
        run_dir = suite_dir / spec.implementation / safe_name(spec.model) / spec.prompt_id
        existing = load_run_status(run_dir)
        if existing and existing.get("status") == "completed" and (run_dir / "grade.json").is_file():
            print(f"SKIP completed: {spec.run_id}")
            continue
        if existing and existing.get("status") == "in_progress":
            print(f"RESUME retrying in-progress run: {spec.run_id}")

        print(f"RUN {spec.run_id}")
        try:
            run_status = await run_participant(
                spec=spec,
                prompt=prompt_by_id(spec.prompt_id),
                suite_dir=suite_dir,
                mcp_url=args.mcp_url,
                openrouter=openrouter,
                timeout_seconds=args.timeout_seconds,
                tool_timeout_seconds=args.tool_timeout_seconds,
                max_tool_calls_per_run=args.max_tool_calls_per_run,
                token_cutoff=args.token_cutoff,
                max_response_tokens=args.max_response_tokens,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            prompt = prompt_by_id(spec.prompt_id)
            run_dir = suite_dir / spec.implementation / safe_name(spec.model) / spec.prompt_id
            existing_status = load_run_status(run_dir) or {}
            run_status = {
                **existing_status,
                "run_id": spec.run_id,
                "implementation": spec.implementation,
                "model": spec.model,
                "prompt_id": spec.prompt_id,
                "prompt": prompt["text"],
                "expected_tool": prompt["expected_tool"],
                "status": "errored",
                "ended_at": now_iso(),
                "elapsed_ms": existing_status.get("elapsed_ms"),
                "final_answer": existing_status.get("final_answer", ""),
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "tool_calls": existing_status.get("tool_calls", 0),
                "resource_calls": existing_status.get("resource_calls", 0),
                "prompt_calls": existing_status.get("prompt_calls", 0),
                "approx_tokens": existing_status.get("approx_tokens", 0),
            }
            if not run_status.get("started_at"):
                run_status["started_at"] = now_iso()
            write_json(run_status_path(run_dir), run_status)
        print(f"  status={run_status['status']} elapsed_ms={run_status['elapsed_ms']}")

        try:
            grade = grade_run(
                openrouter=openrouter,
                run_status=run_status,
                run_dir=run_dir,
                implementation_description=IMPLEMENTATIONS[spec.implementation]["description"],
                max_tokens=args.grader_max_tokens,
            )
            print(f"  grade={grade.get('score')} reason={grade.get('reasoning', '')[:120]}")
        except Exception as exc:
            print(f"  grader_error={type(exc).__name__}: {exc}", file=sys.stderr)
            append_jsonl(
                suite_dir / "grader_errors.jsonl",
                {
                    "run_id": spec.run_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "timestamp": now_iso(),
                },
            )

        summarize_suite(suite_dir)
        completed_this_invocation += 1

    print(f"Finished invocation. Runs attempted: {completed_this_invocation}")
    print(f"Summary: {suite_dir / 'summary.json'}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-name", default="gufi_mcp_eval")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--resume", type=Path, help="Existing suite directory to resume")
    parser.add_argument("--mcp-url", default=os.environ.get("MCP_SERVER", DEFAULT_MCP_URL))
    parser.add_argument(
        "--implementation",
        action="append",
        choices=sorted([*IMPLEMENTATIONS, *IMPLEMENTATION_ALIASES]),
        help="Implementation arm to run. Repeatable. Defaults to all. 'basic' is an alias for 'base'.",
    )
    parser.add_argument("--model", action="append", help="Participant model to run. Repeatable.")
    parser.add_argument(
        "--prompt",
        action="append",
        choices=[prompt["id"] for prompt in PROMPTS],
        help="Prompt id to run. Repeatable. Defaults to all.",
    )
    parser.add_argument("--limit", type=int, help="Limit number of run specs for smoke testing")
    parser.add_argument("--dry-run", action="store_true", help="Print selected runs without calling MCP/OpenRouter")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--tool-timeout-seconds", type=int, default=60)
    parser.add_argument("--max-tool-calls-per-run", type=int, default=30)
    parser.add_argument("--token-cutoff", type=int, default=50_000)
    parser.add_argument("--max-response-tokens", type=int, default=4096)
    parser.add_argument("--grader-max-tokens", type=int, default=1200)
    parser.add_argument("--request-timeout", type=float, default=60.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    args = parse_args(argv or sys.argv[1:])
    return asyncio.run(run_suite(args))


if __name__ == "__main__":
    raise SystemExit(main())
