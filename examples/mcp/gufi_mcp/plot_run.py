#!/usr/bin/env python3
"""Plot tool usage, errors, and timing for one eval run.

    uv run --with matplotlib python plot_run.py eval_runs/<run_id> [--output plots/<run_id>]

General-mode trials have no pass/fail, so outcome plots use only the mirrored scenarios
(those also in scenarios.single.yaml). A trial counts as correct when its final answer
contains the ground-truth values for at least GRADE_THRESHOLD of the ground-truth rows;
see answer_matches. Other trials are "ungraded" and appear only in the overall plots.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gufi_mcp_util as util  # noqa: E402
from eval_lib.registry import load_manifest, load_registry, render_scenario  # noqa: E402

GRADE_THRESHOLD = 0.9
OUTCOMES = ("correct", "incorrect")
COLORS = {"correct": "#2e7d32", "incorrect": "#c62828", "ungraded": "#757575",
          "model": "#1565c0", "tools": "#ef6c00", "resources": "#6a1b9a"}

# First matching pattern names the error; order matters.
ERROR_CATEGORIES = [
    ("bare sname/sroll", r"is not a path and is only valid as an argument to rpath|is internal rollup bookkeeping"),
    ("sname/sroll carried in shape", r"cannot be carried between stages"),
    ("rpath outside scatter", r"rpath\(\) only works in scatter_sql"),
    ("path function", r"\w+\(\) is not available\. Use the path columns"),
    ("path column not carried", r"Path columns come from"),
    ("path column on wrong table", r"Path columns exist only in"),
    ("ORDER/LIMIT outside final", r"is only allowed in final_sql"),
    ("sql_file_index path", r"cannot build full paths"),
    ("unknown column", r"do not exist in"),
    ("column count mismatch", r"returns \d+ column\(s\) but"),
    ("SQL parse error", r"could not be parsed|syntax error"),
    ("double quotes", r"double quotes"),
    ("unknown tool argument", r"extra_forbidden|Extra inputs"),
    ("SQLite runtime error", r"SQLite error|gufi_vt rejected"),
    ("timeout", r"did not finish within"),
]


@dataclass
class Trial:
    scenario: str
    trial: int
    duration_s: float
    cost: float
    tokens: int
    stop: str
    outcome: str = "ungraded"
    tool_calls: Counter = field(default_factory=Counter)
    resource_reads: Counter = field(default_factory=Counter)
    errors: list[list[str]] = field(default_factory=list)
    tool_s: float = 0.0
    resource_s: float = 0.0
    first_call_ok: bool | None = None

    @property
    def model_s(self) -> float:
        return max(self.duration_s - self.tool_s - self.resource_s, 0.0)

    @property
    def n_calls(self) -> int:
        return sum(self.tool_calls.values()) + sum(self.resource_reads.values())

    @property
    def n_errors(self) -> int:
        return len(self.errors)


def categorize(message: str) -> list[str]:
    """One error message can hold several pydantic errors; return a category for each."""
    parts = [p for p in re.split(r"\n(?=query\.|query\n|args\.)", message) if p.strip()] or [message]
    if len(parts) > 1 and "validation error" in parts[0] and not any(re.search(p, parts[0]) for _, p in ERROR_CATEGORIES):
        parts = parts[1:]
    found = []
    for part in parts:
        found.append(next((name for name, pattern in ERROR_CATEGORIES if re.search(pattern, part)), "other"))
    return found


def load_trials(run: Path) -> list[Trial]:
    trials = []
    for line in (run / "summary.jsonl").read_text().splitlines():
        row = json.loads(line)
        trace = json.loads((run / row["trace_path"]).read_text())
        usage = row.get("usage") or {}
        t = Trial(row["scenario_id"], row["trial"], row["duration_ms"] / 1000, usage.get("cost") or 0.0,
                  usage.get("total_tokens") or 0, row["stop_reason"])
        tool_events = []
        for e in trace["events"]:
            seconds = (e.get("duration_ms") or 0) / 1000
            if e["kind"] == "tool":
                t.tool_calls[e["name"]] += 1
                t.tool_s += seconds
                tool_events.append(e)
                if e.get("ok") is False:
                    t.errors.append(categorize(e.get("error") or ""))
            elif e["kind"] == "resource":
                t.resource_reads[e["name"]] += 1
                t.resource_s += seconds
        t.first_call_ok = bool(tool_events[0].get("ok")) if tool_events else None
        t.final_text = trace.get("final_text") or ""
        trials.append(t)
    return trials


#
# Grading (mirrored scenarios only)
#

def _number_pattern(n: int | float) -> str:
    if isinstance(n, float) and not n.is_integer():
        return re.escape(f"{n:.2f}".rstrip("0"))
    n = int(n)
    return rf"(?<![\d.,]){(re.escape(str(n)) + '|' + re.escape(f'{n:,}'))}(?![\d]|,\d)"


def _row_patterns(row: list[Any]) -> list[str]:
    numbers, strings = [], []
    for v in row:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            numbers.append(v)
        elif isinstance(v, str) and v:
            text = v.rsplit("/", 1)[-1] if "/" in v else v
            if len(text) >= 2:
                strings.append(re.escape(text))
    # Small counts (< 10) appear in almost any text; use them only when nothing else identifies the row.
    # Epoch timestamps are often rendered as dates, so they are not required when a name identifies the row.
    distinctive = [n for n in numbers if abs(n) >= 10 and not (strings and 1e9 <= n < 1e10)]
    chosen = distinctive if (distinctive or strings) else numbers
    return [_number_pattern(n) for n in chosen] + strings


def answer_matches(text: str, gt_rows: list[list[Any]]) -> bool:
    if not gt_rows:
        return False
    hit = sum(all(re.search(p, text) for p in _row_patterns(r)) for r in gt_rows if _row_patterns(r))
    total = sum(1 for r in gt_rows if _row_patterns(r))
    return total > 0 and hit / total >= GRADE_THRESHOLD


def ground_truth_rows() -> dict[str, list[list[Any]]]:
    rows = {}
    manifests = {}
    for scenario in load_registry(HERE / "scenarios.single.yaml", "single"):
        if scenario.index not in manifests:
            manifests[scenario.index] = load_manifest(util.GUFI_INDEX_ROOT / f"{scenario.index}.manifest.json")
        item = render_scenario(scenario, "single", manifests[scenario.index])
        query = util.GufiAggregateQuery.model_validate({**item.ground_truth, "index": scenario.index})
        rows[scenario.id] = util.run_aggregate(query).rows
    return rows


#
# Plot helpers
#

def save(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(f"wrote {path}")


def mean_counter(trials: list[Trial], attr: str) -> Counter:
    total = Counter()
    for t in trials:
        total.update(getattr(t, attr))
    return Counter({k: v / len(trials) for k, v in total.items()}) if trials else Counter()


def error_counts(trials: list[Trial]) -> Counter:
    return Counter(cat for t in trials for call in t.errors for cat in call)


def barh(ax: plt.Axes, counts: Counter, color: str, title: str, xlabel: str) -> None:
    items = counts.most_common()[::-1]
    if not items:
        ax.text(0.5, 0.5, "none", ha="center", va="center", transform=ax.transAxes)
    else:
        ax.barh([k for k, _ in items], [v for _, v in items], color=color)
        for i, (_, v) in enumerate(items):
            ax.text(v, i, f" {v:.2f}" if isinstance(v, float) and not float(v).is_integer() else f" {int(v)}",
                    va="center", fontsize=8)
    ax.set_title(title)
    ax.set_xlabel(xlabel)


#
# Overall plots (every trial)
#

def plot_overall(trials: list[Trial], out: Path) -> None:
    n = len(trials)

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    barh(axes[0], mean_counter(trials, "tool_calls"), COLORS["tools"], f"Mean tool calls per trial (n={n})", "calls / trial")
    barh(axes[1], mean_counter(trials, "resource_reads"), COLORS["resources"], "Mean resource reads per trial", "reads / trial")
    save(fig, out / "01_tool_usage_per_trial.png")

    fig, ax = plt.subplots(figsize=(9, 5))
    barh(ax, error_counts(trials), COLORS["incorrect"],
         f"Tool error categories ({sum(t.n_errors for t in trials)} failed calls, {n} trials)", "occurrences")
    save(fig, out / "02_error_categories.png")

    cats = [c for c, _ in error_counts(trials).most_common()]
    if cats:
        matrix = np.zeros((len(cats), len(cats)))
        for t in trials:
            for call in t.errors:
                present = sorted(set(call))
                for a in present:
                    for b in present:
                        matrix[cats.index(a), cats.index(b)] += 1
        fig, ax = plt.subplots(figsize=(8, 7))
        im = ax.imshow(matrix, cmap="Reds")
        ax.set_xticks(range(len(cats)), cats, rotation=45, ha="right")
        ax.set_yticks(range(len(cats)), cats)
        for i in range(len(cats)):
            for j in range(len(cats)):
                if matrix[i, j]:
                    ax.text(j, i, int(matrix[i, j]), ha="center", va="center", fontsize=8)
        ax.set_title("Error categories occurring in the same failed call")
        fig.colorbar(im, ax=ax, shrink=0.7)
        save(fig, out / "03_error_cooccurrence.png")

    by_scenario: dict[str, list[Trial]] = defaultdict(list)
    for t in trials:
        by_scenario[t.scenario].append(t)
    scenarios = sorted(by_scenario, key=lambda s: -np.mean([t.n_calls for t in by_scenario[s]]))
    tools = [k for k, _ in mean_counter(trials, "tool_calls").most_common()]
    matrix = np.array([[np.mean([t.tool_calls[tool] for t in by_scenario[s]]) for tool in tools]
                       + [np.mean([t.n_errors for t in by_scenario[s]])] for s in scenarios])
    fig, ax = plt.subplots(figsize=(11, max(6, len(scenarios) * 0.28)))
    im = ax.imshow(matrix, cmap="YlOrBr", aspect="auto")
    ax.set_xticks(range(len(tools) + 1), tools + ["ERRORS"], rotation=40, ha="right")
    ax.set_yticks(range(len(scenarios)), scenarios, fontsize=8)
    for i in range(len(scenarios)):
        for j in range(len(tools) + 1):
            if matrix[i, j] >= 0.05:
                ax.text(j, i, f"{matrix[i, j]:.1f}", ha="center", va="center", fontsize=7)
    ax.set_title("Mean tool calls and failed calls per trial, by scenario")
    fig.colorbar(im, ax=ax, shrink=0.6)
    save(fig, out / "04_scenario_tool_heatmap.png")

    model = [np.mean([t.model_s for t in by_scenario[s]]) for s in scenarios]
    tool = [np.mean([t.tool_s for t in by_scenario[s]]) for s in scenarios]
    res = [np.mean([t.resource_s for t in by_scenario[s]]) for s in scenarios]
    fig, ax = plt.subplots(figsize=(10, max(6, len(scenarios) * 0.28)))
    y = np.arange(len(scenarios))
    ax.barh(y, model, color=COLORS["model"], label="model (LLM round trips)")
    ax.barh(y, tool, left=model, color=COLORS["tools"], label="tool execution")
    ax.barh(y, res, left=np.add(model, tool), color=COLORS["resources"], label="resource reads")
    ax.set_yticks(y, scenarios, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("mean seconds per trial")
    ax.set_title("Where trial time goes, by scenario")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.06), ncol=3)
    save(fig, out / "05_timing_breakdown_by_scenario.png")

    fig, axes = plt.subplots(1, 2, figsize=(13, max(6, len(scenarios) * 0.28)))
    axes[0].barh(y, [np.mean([t.cost for t in by_scenario[s]]) * 1000 for s in scenarios], color=COLORS["model"])
    axes[0].set_xlabel("mean cost per trial (milli-USD)")
    axes[1].barh(y, [np.mean([t.tokens for t in by_scenario[s]]) / 1000 for s in scenarios], color=COLORS["tools"])
    axes[1].set_xlabel("mean tokens per trial (thousands)")
    for ax in axes:
        ax.set_yticks(y, scenarios, fontsize=8)
        ax.invert_yaxis()
    fig.suptitle("Cost and tokens by scenario")
    save(fig, out / "06_cost_tokens_by_scenario.png")

    graded = defaultdict(Counter)
    for t in trials:
        if t.outcome != "ungraded":
            graded[t.scenario][t.outcome] += 1
    if graded:
        names = sorted(graded, key=lambda s: graded[s]["correct"])
        fig, ax = plt.subplots(figsize=(9, 5))
        yy = np.arange(len(names))
        ax.barh(yy, [graded[s]["correct"] for s in names], color=COLORS["correct"], label="correct")
        ax.barh(yy, [graded[s]["incorrect"] for s in names], left=[graded[s]["correct"] for s in names],
                color=COLORS["incorrect"], label="incorrect")
        ax.set_yticks(yy, names)
        ax.set_xlabel("trials")
        ax.set_title(f"Answer correctness, mirrored scenarios (graded against ground truth, threshold {GRADE_THRESHOLD:.0%})")
        ax.legend(loc="lower right")
        save(fig, out / "07_correctness_by_scenario.png")


#
# Per-outcome plots (correct/ and incorrect/) and comparisons
#

def plot_outcome(trials: list[Trial], outcome: str, out: Path) -> None:
    color = COLORS[outcome]
    n = len(trials)
    if not n:
        return
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    usage = mean_counter(trials, "tool_calls") + mean_counter(trials, "resource_reads")
    barh(axes[0], usage, color, f"Mean calls per trial, {outcome} answers (n={n})", "calls / trial")
    barh(axes[1], error_counts(trials), color, f"Error categories, {outcome} answers", "occurrences")
    save(fig, out / outcome / "tool_usage_and_errors.png")

    order = sorted(trials, key=lambda t: t.duration_s, reverse=True)
    labels = [f"{t.scenario} #{t.trial}" for t in order]
    y = np.arange(len(order))
    fig, ax = plt.subplots(figsize=(10, max(5, len(order) * 0.22)))
    model = [t.model_s for t in order]
    tool = [t.tool_s for t in order]
    ax.barh(y, model, color=COLORS["model"], label="model")
    ax.barh(y, tool, left=model, color=COLORS["tools"], label="tools")
    ax.barh(y, [t.resource_s for t in order], left=np.add(model, tool), color=COLORS["resources"], label="resources")
    ax.set_yticks(y, labels, fontsize=7)
    ax.invert_yaxis()
    ax.set_xlabel("seconds")
    ax.set_title(f"Timing per trial, {outcome} answers")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=3)
    save(fig, out / outcome / "timing_per_trial.png")

    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    calls = [t.n_calls for t in trials]
    errors = [t.n_errors for t in trials]
    axes[0].hist(calls, bins=range(0, max(calls) + 2), color=color, edgecolor="white")
    axes[0].set_xlabel("tool calls + resource reads in trial")
    axes[1].hist(errors, bins=range(0, max(errors) + 2), color=color, edgecolor="white")
    axes[1].set_xlabel("failed tool calls in trial")
    for ax in axes:
        ax.set_ylabel("trials")
    fig.suptitle(f"Call and error distribution, {outcome} answers (n={n})")
    save(fig, out / outcome / "calls_and_errors_distribution.png")


def plot_comparison(groups: dict[str, list[Trial]], out: Path) -> None:
    present = [o for o in OUTCOMES if groups.get(o)]
    if len(present) < 2:
        return
    tools = sorted({k for o in present for t in groups[o] for k in (*t.tool_calls, *t.resource_reads)})
    fig, ax = plt.subplots(figsize=(12, 5))
    width = 0.8 / len(present)
    x = np.arange(len(tools))
    for i, o in enumerate(present):
        usage = mean_counter(groups[o], "tool_calls") + mean_counter(groups[o], "resource_reads")
        ax.bar(x + i * width, [usage[k] for k in tools], width, color=COLORS[o], label=f"{o} (n={len(groups[o])})")
    ax.set_xticks(x + width / 2, tools, rotation=35, ha="right")
    ax.set_ylabel("mean calls per trial")
    ax.set_title("Tool usage: correct vs incorrect answers")
    ax.legend()
    save(fig, out / "comparison" / "tool_usage.png")

    cats = sorted({c for o in present for c in error_counts(groups[o])})
    fig, ax = plt.subplots(figsize=(12, 5))
    x = np.arange(len(cats))
    for i, o in enumerate(present):
        counts = error_counts(groups[o])
        ax.bar(x + i * width, [counts[c] / len(groups[o]) for c in cats], width, color=COLORS[o], label=o)
    ax.set_xticks(x + width / 2, cats, rotation=35, ha="right")
    ax.set_ylabel("occurrences per trial")
    ax.set_title("Error categories per trial: correct vs incorrect answers")
    ax.legend()
    save(fig, out / "comparison" / "error_categories.png")

    fig, axes = plt.subplots(1, 4, figsize=(15, 4.5))
    metrics = [("total s", lambda t: t.duration_s), ("model s", lambda t: t.model_s),
               ("tool s", lambda t: t.tool_s), ("calls", lambda t: t.n_calls)]
    for ax, (label, fn) in zip(axes, metrics):
        data = [[fn(t) for t in groups[o]] for o in present]
        box = ax.boxplot(data, patch_artist=True)
        ax.set_xticks(range(1, len(present) + 1), present)
        for patch, o in zip(box["boxes"], present):
            patch.set_facecolor(COLORS[o])
            patch.set_alpha(0.6)
        ax.set_title(label)
    fig.suptitle("Timing and effort: correct vs incorrect answers")
    save(fig, out / "comparison" / "timing_and_effort.png")

    fig, axes = plt.subplots(1, 3, figsize=(13, 4))
    rates = {
        "first tool call succeeded": lambda ts: np.mean([t.first_call_ok for t in ts if t.first_call_ok is not None]),
        "trial had any failed call": lambda ts: np.mean([t.n_errors > 0 for t in ts]),
        "hit max tool calls": lambda ts: np.mean([t.stop == "max_tool_calls" for t in ts]),
    }
    for ax, (label, fn) in zip(axes, rates.items()):
        values = [fn(groups[o]) for o in present]
        ax.bar(present, values, color=[COLORS[o] for o in present])
        for i, v in enumerate(values):
            ax.text(i, v, f"{v:.0%}", ha="center", va="bottom")
        ax.set_ylim(0, 1.1)
        ax.set_title(label)
    fig.suptitle("Rates: correct vs incorrect answers")
    save(fig, out / "comparison" / "rates.png")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path, help="eval_runs/<run_id>")
    parser.add_argument("--output", type=Path, help="Output directory (default: plots/<run_id>)")
    args = parser.parse_args()
    out = args.output or HERE / "plots" / args.run.name

    trials = load_trials(args.run)
    truth = ground_truth_rows()
    for t in trials:
        if t.scenario in truth:
            t.outcome = "correct" if answer_matches(t.final_text, truth[t.scenario]) else "incorrect"

    groups = defaultdict(list)
    for t in trials:
        groups[t.outcome].append(t)

    plot_overall(trials, out / "overall")
    for outcome in OUTCOMES:
        plot_outcome(groups[outcome], outcome, out)
    plot_comparison(groups, out)

    grades = {t.scenario: [] for t in trials if t.outcome != "ungraded"}
    for t in trials:
        if t.outcome != "ungraded":
            grades[t.scenario].append(t.outcome == "correct")
    summary = {
        "run": args.run.name,
        "trials": len(trials),
        "graded_trials": sum(len(v) for v in grades.values()),
        "correct": len(groups["correct"]),
        "incorrect": len(groups["incorrect"]),
        "ungraded": len(groups["ungraded"]),
        "grade_threshold": GRADE_THRESHOLD,
        "correct_by_scenario": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(grades.items())},
        "error_categories": dict(error_counts(trials).most_common()),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
