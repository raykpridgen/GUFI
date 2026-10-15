#!/usr/bin/env python3
"""
Run agent evaluation trials against the GUFI MCP server (single-query + general usage).

Design: eval_design.md Parts 2-6. Usage is in docs/eval.md.

  gufi_eval.py check                       run every single-query ground truth, no agent
  gufi_eval.py run --agent replay          stub agent that replays ground truth (no API cost)
  gufi_eval.py run --mode both --trials 3  OpenRouter agent (OPENROUTER_API_KEY in .env)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import subprocess
import sys
from collections import defaultdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

from eval_lib.agent import AGGREGATE_TOOL, AgentDriver, EvalConstraints, TrialContext  # noqa: E402
from eval_lib.driver import DEFAULT_MODEL, OpenRouterDriver, ReplayDriver  # noqa: E402
from eval_lib.mcp_session import GufiMcpServer  # noqa: E402
from eval_lib.metrics import general_metrics, score_single  # noqa: E402
from eval_lib.registry import (  # noqa: E402
    IndexManifest, Mode, RegistryError, RenderedScenario, file_sha256, load_manifest, load_registry,
    render_scenario, validate_ground_truth,
)
from eval_lib.reporter import RunWriter, record_general, record_single  # noqa: E402


class EvalError(Exception):
    """A setup failure to report without a traceback."""


#
# Setup
#

def index_root() -> Path:
    root = os.getenv("GUFI_INDEX_ROOT", "").strip()
    if not root:
        raise EvalError("GUFI_INDEX_ROOT is not set (put it in .env)")
    return Path(root)


def load_scenarios(args: argparse.Namespace, modes: list[Mode]) -> tuple[list[RenderedScenario], dict[str, IndexManifest]]:
    """Load the registries for modes, apply --scenario filters, and render against the index manifests."""
    paths = {"single": Path(args.scenarios_single), "general": Path(args.scenarios_general)}
    manifests: dict[str, IndexManifest] = {}
    rendered: list[RenderedScenario] = []
    wanted = set(args.scenario or [])
    found: set[str] = set()
    for mode in modes:
        for scenario in load_registry(paths[mode], mode):
            if wanted and scenario.id not in wanted:
                continue
            found.add(scenario.id)
            if not scenario.enabled:
                continue
            if scenario.index not in manifests:
                path = Path(args.index_manifest) if args.index_manifest else index_root() / f"{scenario.index}.manifest.json"
                manifest = load_manifest(path)
                if manifest.index_name != scenario.index:
                    raise EvalError(f"{path} describes index {manifest.index_name!r}, but scenario {scenario.id!r} "
                                    f"uses {scenario.index!r}")
                manifests[scenario.index] = manifest
            item = render_scenario(scenario, mode, manifests[scenario.index])
            if item.ground_truth is not None:
                validate_ground_truth(item)
            rendered.append(item)
    if wanted - found:
        raise EvalError(f"unknown scenario ids for the selected mode(s): {sorted(wanted - found)}")
    if not rendered:
        raise EvalError("no enabled scenarios selected")
    return rendered, manifests


def make_driver(args: argparse.Namespace) -> AgentDriver:
    if args.agent == "replay":
        return ReplayDriver()
    try:
        return OpenRouterDriver(model=args.model, temperature=args.temperature)
    except RuntimeError as e:
        raise EvalError(str(e)) from e


def git_state() -> dict[str, Any]:
    def git(*cmd: str) -> Optional[str]:
        proc = subprocess.run(["git", *cmd], cwd=HERE, capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None

    status = git("status", "--porcelain", "--", ".")
    return {"rev": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(status) if status is not None else None}


def package_versions() -> dict[str, Optional[str]]:
    versions: dict[str, Optional[str]] = {"python": platform.python_version()}
    for name in ("mcp", "openai", "pydantic", "sqlglot"):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = None
    return versions


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


#
# Trials
#

async def run_ground_truth(server: GufiMcpServer, query: dict[str, Any]) -> tuple[list[list[Any]], float]:
    outcome = await server.call_tool(AGGREGATE_TOOL, {"query": query})
    if not outcome.ok or outcome.structured is None:
        raise EvalError(f"ground truth failed: {outcome.text[:1000]}")
    if outcome.structured.get("truncated"):
        raise EvalError("ground truth result was truncated; raise GUFI_MCP_MAX_ROWS or narrow the query")
    return [list(r) for r in outcome.structured.get("rows", [])], outcome.duration_ms


async def run_trial(item: RenderedScenario, trial: int, driver: AgentDriver, servers: dict[str, GufiMcpServer],
                    manifests: dict[str, IndexManifest], writer: RunWriter, args: argparse.Namespace) -> dict[str, Any]:
    scenario = item.scenario
    manifest = manifests[scenario.index]
    full = servers["general"]
    if item.mode == "single":
        gt_rows, gt_ms = await run_ground_truth(full, item.ground_truth)
        constraints = EvalConstraints(mode="single", allowed_tools=[AGGREGATE_TOOL],
                                      max_tool_calls=args.max_tool_calls, timeout_s=args.timeout)
    else:
        constraints = EvalConstraints(mode="general", allowed_tools=[t.name for t in await full.list_tools()],
                                      max_tool_calls=args.max_tool_calls, timeout_s=args.timeout)
    context = TrialContext(server=servers[item.mode], index=scenario.index, fixed_now=manifest.fixed_now,
                           fixed_now_iso=manifest.fixed_now_iso, ground_truth=item.ground_truth)
    result = await driver.run(item.prompt, context, constraints)
    agent = driver.config()
    if item.mode == "single":
        score = score_single(result, scenario.index, gt_rows, item.ground_truth["final_sql"], gt_ms)
        line = record_single(writer, scenario.id, trial, result, score, item.ground_truth, gt_rows, gt_ms, agent)
        status = "PASS" if score.passed else f"FAIL {score.failure_tags} {score.reason}"
    else:
        line = record_general(writer, scenario.id, trial, result, general_metrics(result), agent)
        status = f"done executions={line['executions']} stop={result.stop_reason}"
    cost = result.usage.get("cost") if result.usage else None
    print(f"  [{item.mode}] {scenario.id} trial {trial}: {status}"
          f"{f' (${cost:.5f})' if cost else ''}", file=sys.stderr, flush=True)
    return line


def summarize(lines: list[dict[str, Any]]) -> dict[str, Any]:
    per_scenario: dict[str, list[bool]] = defaultdict(list)
    cost = 0.0
    tokens = 0
    for line in lines:
        if line["mode"] == "single":
            per_scenario[line["scenario_id"]].append(bool(line["pass"]))
        usage = line.get("usage") or {}
        cost += usage.get("cost") or 0
        tokens += usage.get("total_tokens") or 0
    rates = {sid: sum(v) / len(v) for sid, v in per_scenario.items()}
    single_trials = [p for v in per_scenario.values() for p in v]
    return {
        "single_pass_rate": (sum(single_trials) / len(single_trials)) if single_trials else None,
        "single_pass_rate_by_scenario": rates,
        "trials": len(lines),
        "single_trials": len(single_trials),
        "general_trials": sum(1 for line in lines if line["mode"] == "general"),
        "total_cost_usd": round(cost, 6),
        "total_tokens": tokens,
    }


async def run_command(args: argparse.Namespace) -> int:
    modes: list[Mode] = ["single", "general"] if args.mode == "both" else [args.mode]
    items, manifests = load_scenarios(args, modes)
    driver = make_driver(args)
    writer = RunWriter(Path(args.output))
    agent_config = driver.config()

    writer.write_json("config.json", {
        "run_id": writer.run_id,
        "mode": args.mode,
        "trials": args.trials,
        "agent": agent_config,
        "openrouter_model": agent_config.get("model"),
        "concurrency": args.concurrency,
        "max_tool_calls": args.max_tool_calls,
        "timeout_s": args.timeout,
        "scenario_filter": args.scenario or [],
        "scenario_files": {m: str(Path(getattr(args, f"scenarios_{m}")).resolve()) for m in modes},
        "indexes": {name: {"index_name": m.index_name, "seed": m.seed, "fixed_now": m.fixed_now,
                           "fixed_now_iso": m.fixed_now_iso, "manifest": str(m.path)}
                    for name, m in manifests.items()},
    })
    print(f"Run {writer.run_id}: {len(items)} scenario(s) x {args.trials} trial(s), agent {driver.name}"
          f"{' ' + agent_config['model'] if agent_config.get('model') else ''} -> {writer.root}", file=sys.stderr)

    async with GufiMcpServer("all") as full, GufiMcpServer(AGGREGATE_TOOL) as slim:
        servers = {"general": full, "single": slim}
        gufi_version = (await full.call_tool("gufi_version", {})).text.strip()
        semaphore = asyncio.Semaphore(max(args.concurrency, 1))
        lines: list[dict[str, Any]] = []
        failures: list[str] = []

        async def guarded(item: RenderedScenario, trial: int) -> None:
            async with semaphore:
                try:
                    lines.append(await run_trial(item, trial, driver, servers, manifests, writer, args))
                except EvalError as e:
                    failures.append(f"{item.scenario.id} trial {trial}: {e}")
                    print(f"  [{item.mode}] {item.scenario.id} trial {trial}: ERROR {e}", file=sys.stderr)

        await asyncio.gather(*(guarded(item, t) for item in items for t in range(1, args.trials + 1)))

    results = summarize(lines)
    results["infrastructure_errors"] = failures
    writer.write_json("manifest.json", {
        "run_id": writer.run_id,
        "mcp_git": git_state(),
        "gufi_version": gufi_version,
        "packages": package_versions(),
        "registries": {m: {"path": str(Path(getattr(args, f"scenarios_{m}")).resolve()),
                           "sha256": file_sha256(Path(getattr(args, f"scenarios_{m}")))} for m in modes},
        "index_manifests": {name: m.manifest_id for name, m in manifests.items()},
        "agent_config": agent_config,
        "agent_config_hash": config_hash(agent_config),
        "results": results,
    })

    if results["single_pass_rate"] is not None:
        print(f"\nSingle-query mean pass rate: {results['single_pass_rate']:.1%} over {results['single_trials']} trial(s)")
        for sid, rate in sorted(results["single_pass_rate_by_scenario"].items()):
            print(f"  {sid:24s} {rate:.0%}")
    if results["general_trials"]:
        print(f"General-usage trials recorded: {results['general_trials']}")
    print(f"Tokens: {results['total_tokens']}  cost: ${results['total_cost_usd']:.4f}")
    print(f"Artifacts: {writer.root}")
    if failures:
        print(f"{len(failures)} trial(s) could not run; see manifest.json", file=sys.stderr)
        return 1
    return 0


async def check_command(args: argparse.Namespace) -> int:
    """Run every selected single-query ground truth and show its rows (eval_design.md Part 8 step 2)."""
    items, _ = load_scenarios(args, ["single"])
    problems = 0
    async with GufiMcpServer("all") as full:
        for item in items:
            try:
                rows, ms = await run_ground_truth(full, item.ground_truth)
            except EvalError as e:
                print(f"FAIL  {item.scenario.id}: {e}")
                problems += 1
                continue
            trivial = not rows or all(v in (None, 0, "") for row in rows for v in row)
            flag = "WARN" if trivial else "ok  "
            problems += trivial
            preview = json.dumps(rows[:args.preview], default=str)
            print(f"{flag}  {item.scenario.id:24s} rows={len(rows):<5d} {ms:8.1f} ms  {preview[:200]}")
    print(f"\n{len(items) - problems}/{len(items)} ground truths returned non-trivial rows")
    return 1 if problems else 0


#
# CLI
#

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GUFI MCP agent evaluation")
    sub = parser.add_subparsers(dest="command", required=True)

    def registry_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--scenarios-single", default=str(HERE / "scenarios.single.yaml"), help="Single-query registry")
        p.add_argument("--scenarios-general", default=str(HERE / "scenarios.general.yaml"), help="General-usage registry")
        p.add_argument("--index-manifest", help="Index manifest (default: GUFI_INDEX_ROOT/<index>.manifest.json)")
        p.add_argument("--scenario", action="append", help="Only run this scenario id (repeatable)")

    run = sub.add_parser("run", help="Run agent trials")
    registry_options(run)
    run.add_argument("--mode", choices=("single", "general", "both"), default="single")
    run.add_argument("--trials", type=int, default=1, help="Trials per scenario")
    run.add_argument("--agent", choices=("openrouter", "replay"), default="openrouter")
    run.add_argument("--model", default=DEFAULT_MODEL, help="OpenRouter model id")
    run.add_argument("--temperature", type=float, help="Sampling temperature (default: model default)")
    run.add_argument("--concurrency", type=int, default=4, help="Trials run at once")
    run.add_argument("--max-tool-calls", type=int, default=15, help="Tool calls (incl. resource reads) per trial")
    run.add_argument("--timeout", type=float, default=180.0, help="Seconds per trial")
    run.add_argument("--output", default=str(HERE / "eval_runs"), help="Directory for run artifacts")

    check = sub.add_parser("check", help="Run single-query ground truths without an agent")
    registry_options(check)
    check.add_argument("--preview", type=int, default=3, help="Rows to show per scenario")

    args = parser.parse_args(argv)
    if args.command == "run" and args.trials < 1:
        parser.error("--trials must be at least 1")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "check":
            return asyncio.run(check_command(args))
        return asyncio.run(run_command(args))
    except (EvalError, RegistryError) as e:
        print(f"gufi_eval: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
