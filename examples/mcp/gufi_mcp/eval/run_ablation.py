"""Run a server ablation sweep: one gufi_eval.py run per config in ablation_matrix.yaml.

    uv run python run_ablation.py --phase pilot                     # 8 configs (leave-one-out + minimal)
    uv run python run_ablation.py --phase factorial                 # all 48 configs
    uv run python run_ablation.py --phase pilot --agent replay --trials 1   # wiring smoke test, no API cost
    uv run python run_ablation.py --only no_primer --only rpath     # selected rows of the phase

Configs run one after another with the matrix's fixed settings. The sweep is safe to interrupt
(Ctrl-C, SIGTERM, a crash, a reboot) and to rerun with the same command:

- Every graded trial is flushed to its run's summary.jsonl as it finishes. A config that was cut off
  resumes in its own run directory and runs only the trials it is missing.
- A config is skipped when eval_runs/ already holds a finished run with the same factors, eval
  settings, agent, model, and scenario registry, so the factorial also reuses pilot runs.
- Trials the harness could not run fairly (model API outage, rate limit, out of credits, a broken MCP
  server) are not graded; the config is retried up to --retries times with a growing pause.
- If an attempt grades no new trials at all, the problem is not the config (no credits, network down),
  so the sweep stops instead of burning through every config; rerun once it is fixed.
- A lock file keeps two sweeps from writing to the same eval_runs/ at once, and
  eval_runs/sweep_<phase>.progress.json records each config's status after every step.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import gufi_eval  # noqa: E402
from eval_lib.driver import DEFAULT_MODEL  # noqa: E402
from eval_lib.registry import file_sha256  # noqa: E402
from gufi_ablation import CHOICES, AblationConfig  # noqa: E402

MATRIX = HERE / "ablation_matrix.yaml"
SETTING_FLAGS = ("mode", "trials", "max_attempts", "max_tool_calls", "timeout", "concurrency")
RETRY_PAUSE_FACTORS = (1, 4, 10)  # pause before the 2nd, 3rd, ... attempt, in units of --retry-pause


class SweepStopped(Exception):
    """The sweep cannot make progress; stop and tell the user how to resume."""


def factorial_rows() -> list[dict[str, Any]]:
    rows = []
    for values in itertools.product(*CHOICES.values()):
        cfg = AblationConfig.from_mapping(dict(zip(CHOICES, values)))
        rows.append({"id": "full" if cfg.is_full else cfg.id, **cfg.as_dict()})
    return rows


def load_phase(path: Path, phase: str) -> tuple[dict[str, Any], list[tuple[str, AblationConfig]]]:
    data = yaml.safe_load(path.read_text())
    if phase not in data["phases"]:
        raise SystemExit(f"{path} has no phase {phase!r}; phases: {list(data['phases'])}")
    rows = data["phases"][phase]
    if rows == "all":
        rows = factorial_rows()
    configs = []
    for row in rows:
        row = dict(row)
        configs.append((row.pop("id"), AblationConfig.from_mapping(row)))
    ids = [i for i, _ in configs]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"{path} phase {phase!r} repeats ids")
    return data.get("settings", {}), configs


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def graded_trials(run: Path) -> int:
    """Complete summary lines on disk (a torn last line does not count)."""
    try:
        return sum(1 for line in (run / "summary.jsonl").read_bytes().splitlines(keepends=True) if line.endswith(b"\n"))
    except OSError:
        return 0


def run_state(output: Path, cfg: AblationConfig, expect: dict[str, Any]) -> tuple[str, Optional[Path]]:
    """("finished", dir), ("resumable", dir), or ("none", None) for cfg under the expected settings.

    finished: manifest written, every trial graded, no infrastructure errors. resumable: the newest run
    with matching factors and settings that is not finished (interrupted, or trials left to rerun).
    """
    resumable: Optional[Path] = None
    for run in sorted(output.glob("ablation_*"), reverse=True):
        config = _read_json(run / "config.json")
        if not config or config.get("ablation", {}).get("factors") != cfg.as_dict():
            continue
        if any(config.get(key) != value for key, value in expect["config"].items()):
            continue
        manifest = _read_json(run / "manifest.json")
        # Runs from before resume support record the registry hash only in their manifest.
        registry = (config.get("registry_sha256") or {}).get("single") or \
            ((manifest or {}).get("registries", {}).get("single", {}).get("sha256"))
        if registry != expect["registry_sha"]:
            continue
        results = (manifest or {}).get("results", {})
        expected = results.get("expected_trials", expect["trials_total"])
        if manifest and not results.get("infrastructure_errors") and graded_trials(run) >= expected:
            return "finished", run
        if resumable is None and "registry_sha256" in config:  # only runs that support resuming
            resumable = run
    return ("resumable", resumable) if resumable else ("none", None)


@contextmanager
def sweep_lock(output: Path) -> Iterator[None]:
    """Refuse to run two sweeps into the same output directory at once (a lock left by a dead process is removed)."""
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".ablation_sweep.lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip() or 0)
                os.kill(pid, 0)
            except (ValueError, OSError):  # unreadable, or no such process: stale
                lock.unlink(missing_ok=True)
                continue
            raise SystemExit(f"another sweep (pid {pid}) is writing to {output}; wait for it or stop it first")
    else:
        raise SystemExit(f"cannot take the sweep lock {lock}")
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def write_progress(path: Path, phase: str, status: dict[str, dict[str, Any]]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"phase": phase, "updated": datetime.now(timezone.utc).isoformat(),
                               "configs": status}, indent=2) + "\n")
    os.replace(tmp, path)


def _raise_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", required=True, help="Phase of the matrix to run (pilot, factorial, ...)")
    parser.add_argument("--matrix", default=str(MATRIX))
    parser.add_argument("--only", action="append", default=[], help="Run only this row id (repeatable)")
    parser.add_argument("--agent", choices=("openrouter", "replay"), default="openrouter")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--trials", type=int, help="Override the matrix's trials (e.g. 1 for a smoke test)")
    parser.add_argument("--scenario", action="append", help="Only this scenario id (repeatable)")
    parser.add_argument("--output", default=str(HERE / "eval_runs"))
    parser.add_argument("--retries", type=int, default=3, help="Attempts per config when trials hit infrastructure "
                                                                 "errors (default 3)")
    parser.add_argument("--retry-pause", type=float, default=30,
                        help="Seconds to wait before retrying a config; later retries wait 4x and 10x (default 30)")
    parser.add_argument("--force", action="store_true", help="Start fresh runs even if finished or partial ones exist")
    args = parser.parse_args(argv)

    settings, configs = load_phase(Path(args.matrix), args.phase)
    if args.trials is not None:
        settings["trials"] = args.trials
    if args.only:
        unknown = set(args.only) - {i for i, _ in configs}
        if unknown:
            raise SystemExit(f"--only names unknown ids for phase {args.phase}: {sorted(unknown)}")
        configs = [(i, c) for i, c in configs if i in args.only]

    output = Path(args.output)
    scenarios = yaml.safe_load((HERE / "scenarios.single.yaml").read_text())
    n_scenarios = len(args.scenario) if args.scenario else sum(1 for s in scenarios if s.get("enabled", True))
    model = args.model if args.agent == "openrouter" else None
    expect = {"config": {"mode": settings["mode"], "trials": settings["trials"],
                         "max_attempts": settings["max_attempts"], "max_tool_calls": settings["max_tool_calls"],
                         "timeout_s": float(settings["timeout"]), "openrouter_model": model,
                         "scenario_filter": args.scenario or []},
              "registry_sha": file_sha256(HERE / "scenarios.single.yaml"),
              "trials_total": n_scenarios * settings["trials"]}

    signal.signal(signal.SIGTERM, _raise_interrupt)
    progress_path = output / f"sweep_{args.phase}.progress.json"
    status: dict[str, dict[str, Any]] = {}
    print(f"Ablation phase {args.phase}: {len(configs)} config(s), agent {args.agent}"
          f"{' ' + model if model else ''}, settings {settings}", file=sys.stderr)
    try:
        with sweep_lock(output):
            for n, (row_id, cfg) in enumerate(configs, 1):
                entry = status.setdefault(row_id, {"config_id": cfg.id, "status": "pending", "run": None,
                                                   "attempts": 0})
                for attempt in range(1, max(args.retries, 1) + 1):
                    state, run = ("none", None) if args.force and attempt == 1 else run_state(output, cfg, expect)
                    if state == "finished":
                        entry.update(status="done", run=run.name, graded=graded_trials(run))
                        if attempt == 1:
                            print(f"[{n}/{len(configs)}] {row_id} ({cfg.id}): already done in {run.name}",
                                  file=sys.stderr)
                        break
                    if attempt > 1:
                        pause = args.retry_pause * RETRY_PAUSE_FACTORS[min(attempt - 2, len(RETRY_PAUSE_FACTORS) - 1)]
                        print(f"  {row_id}: trials left to rerun; retrying in {pause}s "
                              f"(attempt {attempt}/{args.retries})", file=sys.stderr, flush=True)
                        time.sleep(pause)
                    before = graded_trials(run) if run else 0
                    where = f", resuming {run.name} ({before}/{expect['trials_total']} graded)" if run else ""
                    print(f"\n[{n}/{len(configs)}] {row_id} ({cfg.id}){where}", file=sys.stderr, flush=True)
                    run_argv = ["run", "--agent", args.agent, "--model", args.model, "--output", str(output),
                                "--ablation-id", row_id]
                    if run is not None:
                        run_argv += ["--resume-dir", str(run)]
                    for key in SETTING_FLAGS:
                        run_argv += [f"--{key.replace('_', '-')}", str(settings[key])]
                    if args.temperature is not None:
                        run_argv += ["--temperature", str(args.temperature)]
                    for sid in args.scenario or []:
                        run_argv += ["--scenario", sid]
                    for name, value in cfg.as_dict().items():
                        run_argv += ["--ablation", f"{name}={int(value) if isinstance(value, bool) else value}"]
                    code = gufi_eval.main(run_argv)
                    entry["attempts"] += 1
                    _, run = run_state(output, cfg, expect)
                    latest = run or max(output.glob(f"ablation_{row_id}_*"), default=None)
                    after = graded_trials(latest) if latest else 0
                    entry.update(run=latest.name if latest else None, graded=after, last_exit=code)
                    if code == 0:
                        entry["status"] = "done"
                        break
                    entry["status"] = "incomplete"
                    write_progress(progress_path, args.phase, status)
                    if after <= before:
                        raise SweepStopped(f"{row_id}: attempt {attempt} graded no new trials (exit {code}); "
                                           "the cause is probably not this config (API credits, network, "
                                           "GUFI setup). Fix it and rerun the same command to resume.")
                write_progress(progress_path, args.phase, status)
    except KeyboardInterrupt:
        write_progress(progress_path, args.phase, status)
        print("\nInterrupted. Graded trials are saved; rerun the same command to resume.", file=sys.stderr)
        return 130
    except SweepStopped as e:
        write_progress(progress_path, args.phase, status)
        print(f"\nSweep stopped: {e}", file=sys.stderr)
        return 2

    print("\nSweep summary:", file=sys.stderr)
    for row_id, entry in status.items():
        print(f"  {row_id:28s} {entry['status']:10s} {entry.get('graded', 0):>4}/{expect['trials_total']} "
              f"{entry['run'] or ''}", file=sys.stderr)
    incomplete = [r for r, e in status.items() if e["status"] != "done"]
    if incomplete:
        print(f"{len(incomplete)} config(s) still have trials left; rerun the same command to resume them.",
              file=sys.stderr)
    return 0 if not incomplete else 1


if __name__ == "__main__":
    sys.exit(main())
