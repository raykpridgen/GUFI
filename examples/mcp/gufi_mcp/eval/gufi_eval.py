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
SERVER_DIR = HERE.parent  # gufi_mcp_server.py, gufi_mcp_util.py, gufi_ablation.py, and the shared .env
sys.path.insert(0, str(SERVER_DIR))
load_dotenv(HERE / ".env")
load_dotenv(SERVER_DIR / ".env")  # does not override values already set

from gufi_ablation import AblationConfig  # noqa: E402

from eval_lib.agent import AGGREGATE_TOOL, AgentDriver, EvalConstraints, TrialContext  # noqa: E402
from eval_lib.driver import DEFAULT_MODEL, OpenRouterDriver, ReplayDriver  # noqa: E402
from eval_lib.mcp_session import GufiMcpServer  # noqa: E402
from eval_lib.metrics import general_metrics, score_single  # noqa: E402
from eval_lib.registry import (  # noqa: E402
    IndexManifest, Mode, RegistryError, RenderedScenario, file_sha256, load_manifest, load_registry,
    render_scenario, validate_ground_truth,
)
from eval_lib.reporter import RunWriter, new_run_id, record_general, record_single  # noqa: E402


class EvalError(Exception):
    """A setup failure to report without a traceback."""


class InfrastructureError(EvalError):
    """A trial the harness could not run fairly (model API outage, rate limit, broken MCP server).

    It is not graded and gets no summary line, so resuming the run reruns it.
    """


# Exit codes of `run`: 0 every trial graded; 1 some trials hit infrastructure errors (resume to rerun
# them); 3 the run stopped early because an MCP server connection broke (resume to continue).
EXIT_INCOMPLETE = 1
EXIT_ABORTED = 3
# Config keys that must match for a run directory to be resumed with the current arguments.
RESUME_KEYS = ("ablation", "mode", "trials", "openrouter_model", "max_tool_calls", "max_attempts", "timeout_s",
               "scenario_filter", "registry_sha256")


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


def gufi_version() -> Optional[str]:
    """gufi_query --version of the GUFI the server runs, or None if it cannot be read."""
    try:
        import gufi_mcp_util

        return gufi_mcp_util.gufi_query_version()
    except Exception:  # version is informational; never fail a run over it
        return None


def ablation_record(args: argparse.Namespace) -> dict[str, Any]:
    cfg: AblationConfig = args.ablation_config
    return {"id": args.ablation_id or ("full" if cfg.is_full else cfg.id), "config_id": cfg.id,
            "factors": cfg.as_dict(), "env": cfg.env()}


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
    reference, agent_server = servers["reference"], servers["agent"]
    # Every tool the (possibly ablated) server registers is offered in both modes; single mode grades
    # only aggregate calls, but discovery tools are part of what the ablation measures.
    tool_names = [t.name for t in await agent_server.list_tools()]
    if item.mode == "single":
        gt_rows, gt_ms = await run_ground_truth(reference, item.ground_truth)
        alternates = [((await run_ground_truth(reference, alt))[0], alt["final_sql"]) for alt in item.alternates]
        constraints = EvalConstraints(mode="single", allowed_tools=tool_names,
                                      max_tool_calls=args.max_tool_calls, timeout_s=args.timeout,
                                      max_aggregate_calls=args.max_attempts)
    else:
        constraints = EvalConstraints(mode="general", allowed_tools=tool_names,
                                      max_tool_calls=args.max_tool_calls, timeout_s=args.timeout)
    context = TrialContext(server=agent_server, index=scenario.index, fixed_now=manifest.fixed_now,
                           fixed_now_iso=manifest.fixed_now_iso, ground_truth=item.ground_truth,
                           ablation=args.ablation_config.as_dict())
    transport_before = reference.transport_errors + agent_server.transport_errors
    result = await driver.run(item.prompt, context, constraints)
    broken = reference.transport_errors + agent_server.transport_errors > transport_before
    if result.infrastructure_error or broken:
        why = result.error if result.infrastructure_error else "MCP server connection failed during the trial"
        writer.write_json(f"infrastructure/{item.mode}/{scenario.id}/trial_{trial}.{new_run_id()}.json", {
            "scenario": scenario.id, "trial": trial, "reason": why, "stop_reason": result.stop_reason,
            "events": [e.model_dump() for e in result.events], "usage": result.usage,
        })
        raise InfrastructureError(f"not graded, will rerun on resume: {why}")
    agent = driver.config()
    if item.mode == "single":
        score = score_single(result, scenario.index, gt_rows, item.ground_truth["final_sql"], gt_ms,
                             max_attempts=args.max_attempts, alternates=alternates, path_root=str(index_root()))
        line = record_single(writer, scenario.id, trial, result, score, item.ground_truth, gt_rows, gt_ms, agent)
        calls = len(result.aggregate_calls)
        status = (f"PASS on attempt {score.passed_on_attempt} ({calls} aggregate call(s))" if score.passed
                  else f"FAIL {score.failure_tags} {score.reason}")
    else:
        line = record_general(writer, scenario.id, trial, result, general_metrics(result), agent)
        status = f"done executions={line['executions']} stop={result.stop_reason}"
    cost = result.usage.get("cost") if result.usage else None
    print(f"  [{item.mode}] {scenario.id} trial {trial}: {status}"
          f"{f' (${cost:.5f})' if cost else ''}", file=sys.stderr, flush=True)
    return line


def _rate(values: list[bool]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def summarize(lines: list[dict[str, Any]], max_attempts: Optional[int] = None) -> dict[str, Any]:
    single = [line for line in lines if line["mode"] == "single"]
    per_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in single:
        per_scenario[line["scenario_id"]].append(line)
    cost = 0.0
    tokens = 0
    for line in lines:
        usage = line.get("usage") or {}
        cost += usage.get("cost") or 0
        tokens += usage.get("total_tokens") or 0
    # Cumulative: share of trials that had matched by attempt n.
    deepest = max([line["passed_on_attempt"] or 0 for line in single] + [max_attempts or 0])
    by_attempt = {n: _rate([bool(line["passed_on_attempt"]) and line["passed_on_attempt"] <= n for line in single])
                  for n in range(1, deepest + 1)}
    tool_calls = [line["executions"][1] for line in single if line.get("executions")]
    return {
        "single_pass_rate": _rate([bool(line["pass"]) for line in single]),
        "single_one_shot_pass_rate": _rate([bool(line["one_shot_pass"]) for line in single]),
        "single_pass_rate_by_attempt": by_attempt,
        "single_mean_tool_calls": round(sum(tool_calls) / len(tool_calls), 3) if tool_calls else None,
        "single_pass_rate_by_scenario": {sid: _rate([bool(x["pass"]) for x in v]) for sid, v in per_scenario.items()},
        "single_one_shot_rate_by_scenario": {sid: _rate([bool(x["one_shot_pass"]) for x in v])
                                             for sid, v in per_scenario.items()},
        "trials": len(lines),
        "single_trials": len(single),
        "general_trials": sum(1 for line in lines if line["mode"] == "general"),
        "total_cost_usd": round(cost, 6),
        "total_tokens": tokens,
    }


async def run_command(args: argparse.Namespace) -> int:
    modes: list[Mode] = ["single", "general"] if args.mode == "both" else [args.mode]
    items, manifests = load_scenarios(args, modes)
    driver = make_driver(args)
    ablation = ablation_record(args)
    agent_config = driver.config()
    registry_sha = {m: file_sha256(Path(getattr(args, f"scenarios_{m}"))) for m in modes}
    config = {
        "ablation": ablation,
        "mode": args.mode,
        "trials": args.trials,
        "agent": agent_config,
        "openrouter_model": agent_config.get("model"),
        "concurrency": args.concurrency,
        "max_tool_calls": args.max_tool_calls,
        "max_attempts": args.max_attempts,
        "timeout_s": args.timeout,
        "scenario_filter": args.scenario or [],
        "scenario_files": {m: str(Path(getattr(args, f"scenarios_{m}")).resolve()) for m in modes},
        "indexes": {name: {"index_name": m.index_name, "seed": m.seed, "fixed_now": m.fixed_now,
                           "fixed_now_iso": m.fixed_now_iso, "manifest": str(m.path)}
                    for name, m in manifests.items()},
        "registry_sha256": registry_sha,
    }
    if args.resume_dir:
        writer = RunWriter.reopen(Path(args.resume_dir))
        previous = json.loads((writer.root / "config.json").read_text())
        differ = [key for key in RESUME_KEYS if previous.get(key) != config.get(key)]
        if differ:
            raise EvalError(f"cannot resume {writer.root}: its config differs in {differ}")
        (writer.root / "manifest.json").unlink(missing_ok=True)  # the run is open again until it finishes
    else:
        run_id = (f"ablation_{ablation['id']}_{new_run_id()}" if args.ablation_id or not args.ablation_config.is_full
                  else None)
        writer = RunWriter(Path(args.output), run_id)
        writer.write_json("config.json", {"run_id": writer.run_id, **config})
    lines: list[dict[str, Any]] = writer.load_summary()
    done = {(line["mode"], line["scenario_id"], line["trial"]) for line in lines}
    todo = [(item, t) for item in items for t in range(1, args.trials + 1)
            if (item.mode, item.scenario.id, t) not in done]
    if done:
        print(f"Resuming {writer.run_id}: {len(done)} trial(s) already graded, {len(todo)} to run", file=sys.stderr)
    print(f"Run {writer.run_id}: {len(items)} scenario(s) x {args.trials} trial(s), agent {driver.name}"
          f"{' ' + agent_config['model'] if agent_config.get('model') else ''}, server {ablation['config_id']}"
          f" -> {writer.root}", file=sys.stderr)

    async with GufiMcpServer("all") as reference, \
            GufiMcpServer("all", ablation_env=args.ablation_config.env()) as agent_server:
        servers = {"reference": reference, "agent": agent_server}
        await check_server_surface(agent_server, args.ablation_config)
        semaphore = asyncio.Semaphore(max(args.concurrency, 1))
        failures: list[str] = []
        state = {"aborted": False, "skipped": 0}

        async def guarded(item: RenderedScenario, trial: int) -> None:
            async with semaphore:
                if state["aborted"]:
                    state["skipped"] += 1
                    return
                try:
                    lines.append(await run_trial(item, trial, driver, servers, manifests, writer, args))
                except Exception as e:  # one trial's failure must not lose the others
                    if not isinstance(e, EvalError):
                        e = InfrastructureError(f"{type(e).__name__}: {e}")
                    failures.append(f"{item.scenario.id} trial {trial}: {e}")
                    print(f"  [{item.mode}] {item.scenario.id} trial {trial}: ERROR {e}", file=sys.stderr)
                if reference.transport_errors or agent_server.transport_errors:
                    if not state["aborted"]:
                        print("MCP server connection broke; stopping this run (resume to continue)", file=sys.stderr)
                    state["aborted"] = True

        await asyncio.gather(*(guarded(item, t) for item, t in todo))

    results = summarize(lines, args.max_attempts)
    results["infrastructure_errors"] = failures
    results["expected_trials"] = len(items) * args.trials
    results["complete"] = len(lines) == results["expected_trials"]
    results["aborted"] = state["aborted"]
    writer.write_json("manifest.json", {
        "run_id": writer.run_id,
        "mcp_git": git_state(),
        "gufi_version": gufi_version(),
        "ablation": ablation,
        "packages": package_versions(),
        "registries": {m: {"path": str(Path(getattr(args, f"scenarios_{m}")).resolve()),
                           "sha256": file_sha256(Path(getattr(args, f"scenarios_{m}")))} for m in modes},
        "index_manifests": {name: m.manifest_id for name, m in manifests.items()},
        "agent_config": agent_config,
        "agent_config_hash": config_hash(agent_config),
        "results": results,
    })

    if results["single_pass_rate"] is not None:
        print(f"\nSingle-query pass rate: {results['single_pass_rate']:.1%} within {args.max_attempts} attempt(s), "
              f"{results['single_one_shot_pass_rate']:.1%} on the first attempt, over {results['single_trials']} "
              f"trial(s); mean tool calls {results['single_mean_tool_calls']}")
        print("  by attempt: " + ", ".join(f"<= {n}: {rate:.0%}" for n, rate in results["single_pass_rate_by_attempt"].items()))
        one_shot = results["single_one_shot_rate_by_scenario"]
        print(f"  {'scenario':24s} pass  one-shot")
        for sid, rate in sorted(results["single_pass_rate_by_scenario"].items()):
            print(f"  {sid:24s} {rate:4.0%}  {one_shot[sid]:4.0%}")
    if results["general_trials"]:
        print(f"General-usage trials recorded: {results['general_trials']}")
    print(f"Tokens: {results['total_tokens']}  cost: ${results['total_cost_usd']:.4f}")
    print(f"Artifacts: {writer.root}")
    if state["aborted"]:
        print(f"Run stopped early; {results['expected_trials'] - len(lines)} trial(s) left. Resume with "
              f"--resume-dir {writer.root}", file=sys.stderr)
        return EXIT_ABORTED
    if failures:
        print(f"{len(failures)} trial(s) could not run and were not graded; see manifest.json. Resume with "
              f"--resume-dir {writer.root} to rerun them", file=sys.stderr)
        return EXIT_INCOMPLETE
    return 0


async def check_server_surface(server: GufiMcpServer, cfg: AblationConfig) -> None:
    """Abort unless the server exposes exactly the tools and instructions cfg calls for."""
    names = [t.name for t in await server.list_tools()]
    if sorted(names) != sorted(cfg.expected_tools()):
        raise EvalError(f"server for ablation {cfg.id} lists tools {names}, expected {cfg.expected_tools()}")
    if bool(server.instructions.strip()) != cfg.primer:
        raise EvalError(f"server for ablation {cfg.id} has primer={'on' if server.instructions.strip() else 'off'}, "
                        f"expected {'on' if cfg.primer else 'off'}")


async def check_command(args: argparse.Namespace) -> int:
    """Run every selected single-query ground truth and show its rows (eval_design.md Part 8 step 2)."""
    items, _ = load_scenarios(args, ["single"])
    problems = 0
    async with GufiMcpServer("all") as full:
        for item in items:
            for n, query in enumerate([item.ground_truth, *item.alternates]):
                label = item.scenario.id if n == 0 else f"{item.scenario.id}~alt{n}"
                try:
                    rows, ms = await run_ground_truth(full, query)
                except EvalError as e:
                    print(f"FAIL  {label}: {e}")
                    problems += 1
                    continue
                trivial = not rows or all(v in (None, 0, "") for row in rows for v in row)
                flag = "WARN" if trivial else "ok  "
                problems += trivial
                preview = json.dumps(rows[:args.preview], default=str)
                print(f"{flag}  {label:28s} rows={len(rows):<5d} {ms:8.1f} ms  {preview[:200]}")
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
    run.add_argument("--max-attempts", type=int, default=3,
                     help="Single-query: aggregate calls graded per trial; any matching one passes (1 = one-shot)")
    run.add_argument("--timeout", type=float, default=180.0, help="Seconds per trial")
    run.add_argument("--output", default=str(HERE / "eval_runs"), help="Directory for run artifacts")
    run.add_argument("--ablation", action="append", default=[], metavar="FACTOR=VALUE",
                     help="Server ablation factor for the agent's server (repeatable), e.g. --ablation schema=naive. "
                          "Factors: submission, schema, primer, functions, path; unset factors are the full server")
    run.add_argument("--ablation-id", help="Name for this ablation config in run ids and reports (e.g. no_primer)")
    run.add_argument("--resume-dir", help="Continue an interrupted run in this directory: trials already graded "
                                          "are kept, the rest run. The run's settings must match these arguments")

    check = sub.add_parser("check", help="Run single-query ground truths without an agent")
    registry_options(check)
    check.add_argument("--preview", type=int, default=3, help="Rows to show per scenario")

    args = parser.parse_args(argv)
    if args.command == "run":
        try:
            pairs = dict(item.split("=", 1) for item in args.ablation)
            args.ablation_config = AblationConfig.from_mapping(pairs)
        except ValueError as e:  # AblationError or a malformed FACTOR=VALUE
            parser.error(f"--ablation: {e}")
    if args.command == "run" and args.trials < 1:
        parser.error("--trials must be at least 1")
    if args.command == "run" and args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1")
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
