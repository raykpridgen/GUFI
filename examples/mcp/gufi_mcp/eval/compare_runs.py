#!/usr/bin/env python3
"""Compare single-mode eval runs side by side, one bar or column per run.

    uv run --with matplotlib python compare_runs.py eval_runs/<run_a> eval_runs/<run_b> ... \
        [--label <run_id>=<label> ...] [--output plots/<name>]

Runs are shown in the order given. Use --label to name what changed in each run.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import numpy as np

from plot_run import COLORS, FAILURE_COLORS, GUFI_FUNCTIONS, HERE, Trial, load_trials, plt, save

RESOURCE_KINDS = [
    ("gufi://indexes", re.compile(r"^gufi://indexes$")),
    ("gufi://schemas/all", re.compile(r"^gufi://schemas/all$")),
    ("gufi://schemas/<table>", re.compile(r"^gufi://schemas/(?!all$)")),
    ("gufi://functions", re.compile(r"^gufi://functions$")),
    ("gufi://functions/<name>", re.compile(r"^gufi://functions/.")),
]


def resource_kind(uri: str) -> str:
    return next((name for name, pattern in RESOURCE_KINDS if pattern.search(uri)), "other")


def run_labels(runs: list[Path], given: list[str]) -> list[str]:
    names = dict(item.split("=", 1) for item in given)
    return [f"{names[run.name]}\n{run.name[-6:]}" if run.name in names else run.name for run in runs]


def plot_pass_rates(data: list[list[Trial]], labels: list[str], out: Path) -> None:
    x = np.arange(len(labels))
    width = 0.38
    passed = [np.mean([t.outcome == "correct" for t in ts]) for ts in data]
    one_shot = [np.mean([t.passed_on_attempt == 1 and t.aggregate_calls == 1 for t in ts]) for ts in data]
    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 2.4), 5))
    for offset, values, color, name in ((-width / 2, passed, COLORS["correct"], "pass (any attempt)"),
                                        (width / 2, one_shot, COLORS["model"], "pass with one aggregate call")):
        bars = ax.bar(x + offset, values, width, color=color, label=name)
        for bar, v in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, v, f"{v:.0%}", ha="center", va="bottom", fontsize=9)
    ax.set_xticks(x, labels, fontsize=8)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("share of trials")
    ax.set_title("Single-mode pass rate by run")
    ax.legend(loc="upper left")
    save(fig, out / "01_pass_rates.png")


def plot_scenario_heatmap(data: list[list[Trial]], labels: list[str], out: Path) -> None:
    scenarios = sorted({t.scenario for ts in data for t in ts})
    matrix = np.array([[np.mean([t.outcome == "correct" for t in ts if t.scenario == s] or [np.nan]) for ts in data]
                       for s in scenarios])
    scenarios_order = np.argsort(np.nanmean(matrix, axis=1))
    matrix = matrix[scenarios_order]
    scenarios = [scenarios[i] for i in scenarios_order]
    fig, ax = plt.subplots(figsize=(max(7, len(labels) * 2.0 + 3), max(5, len(scenarios) * 0.45)))
    im = ax.imshow(matrix, cmap="RdYlGn", vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(labels)), labels, fontsize=8)
    ax.set_yticks(range(len(scenarios)), scenarios, fontsize=8)
    for i in range(len(scenarios)):
        for j in range(len(labels)):
            if not np.isnan(matrix[i, j]):
                ax.text(j, i, f"{matrix[i, j]:.0%}", ha="center", va="center", fontsize=8)
    ax.set_title("Pass rate by scenario and run")
    fig.colorbar(im, ax=ax, shrink=0.7)
    save(fig, out / "02_scenario_pass_heatmap.png")


def plot_failure_reasons(data: list[list[Trial]], labels: list[str], out: Path) -> None:
    reasons = [r for r, _ in Counter(t.failure_reason for ts in data for t in ts if t.failure_reason).most_common()]
    x = np.arange(len(labels))
    bottom = np.zeros(len(labels))
    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 2.4), 5))
    passed = np.array([sum(t.outcome == "correct" for t in ts) for ts in data], dtype=float)
    ax.bar(x, passed, color=COLORS["correct"], label="pass")
    bottom += passed
    for reason in reasons:
        values = np.array([sum(t.failure_reason == reason for t in ts) for ts in data], dtype=float)
        ax.bar(x, values, bottom=bottom, color=FAILURE_COLORS.get(reason, FAILURE_COLORS["other"]), label=reason)
        for xi, b, v in zip(x, bottom, values):
            if v:
                ax.text(xi, b + v / 2, int(v), ha="center", va="center", fontsize=8, color="white")
        bottom += values
    ax.set_xticks(x, labels, fontsize=8)
    ax.set_ylabel("trials")
    ax.set_title("Trial outcomes by run: pass, or why the graded call failed")
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8)
    save(fig, out / "03_failure_reasons.png")


def plot_usage(data: list[list[Trial]], labels: list[str], out: Path) -> None:
    kinds = [name for name, _ in RESOURCE_KINDS] + ["aggregate_sql_query"]
    means = []
    for ts in data:
        totals = Counter()
        for t in ts:
            for uri, n in t.resource_reads.items():
                totals[resource_kind(uri)] += n
            totals["aggregate_sql_query"] += t.tool_calls.get("aggregate_sql_query", 0)
        means.append([totals[k] / len(ts) for k in kinds])
    x = np.arange(len(kinds))
    width = 0.8 / len(labels)
    fig, ax = plt.subplots(figsize=(13, 5))
    palette = plt.get_cmap("viridis")
    for i, (label, values) in enumerate(zip(labels, means)):
        bars = ax.bar(x + i * width, values, width, color=palette(i / max(len(labels) - 1, 1)),
                      label=label.replace("\n", " "))
        for bar, v in zip(bars, values):
            if v:
                ax.text(bar.get_x() + bar.get_width() / 2, v, f"{v:.2f}", ha="center", va="bottom", fontsize=7)
    ax.set_xticks(x + width * (len(labels) - 1) / 2, kinds, rotation=20, ha="right")
    ax.set_ylabel("mean per trial")
    ax.set_title("Resource reads and aggregate calls per trial, by run")
    ax.legend(fontsize=8)
    save(fig, out / "04_usage_per_trial.png")


def plot_function_use(data: list[list[Trial]], labels: list[str], out: Path) -> None:
    pairs = sorted({(t.scenario, f) for ts in data for t in ts for f in t.functions_used},
                   key=lambda p: (GUFI_FUNCTIONS.index(p[1]), p[0]))
    if not pairs:
        return
    matrix = np.array([[np.mean([f in t.functions_used for t in ts if t.scenario == s] or [0]) for ts in data]
                       for s, f in pairs])
    fig, ax = plt.subplots(figsize=(max(7, len(labels) * 2.0 + 4), max(4, len(pairs) * 0.45)))
    im = ax.imshow(matrix, cmap="Blues", vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(labels)), labels, fontsize=8)
    ax.set_yticks(range(len(pairs)), [f"{f}() in {s}" for s, f in pairs], fontsize=8)
    for i in range(len(pairs)):
        for j in range(len(labels)):
            ax.text(j, i, f"{matrix[i, j]:.0%}", ha="center", va="center", fontsize=8)
    ax.set_title("Share of trials whose aggregate SQL used a GUFI function")
    fig.colorbar(im, ax=ax, shrink=0.7)
    save(fig, out / "05_gufi_function_use.png")


def plot_cost_time(data: list[list[Trial]], labels: list[str], out: Path) -> None:
    metrics = [("mean cost per trial (milli-USD)", lambda t: t.cost * 1000, COLORS["model"]),
               ("mean tokens per trial (thousands)", lambda t: t.tokens / 1000, COLORS["tools"]),
               ("mean seconds per trial", lambda t: t.duration_s, COLORS["resources"])]
    fig, axes = plt.subplots(1, len(metrics), figsize=(max(13, len(labels) * 4), 4.5))
    x = np.arange(len(labels))
    for ax, (title, fn, color) in zip(axes, metrics):
        values = [np.mean([fn(t) for t in ts]) for ts in data]
        bars = ax.bar(x, values, color=color)
        for bar, v in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, v, f"{v:.1f}", ha="center", va="bottom", fontsize=8)
        ax.set_xticks(x, labels, fontsize=7)
        ax.set_title(title, fontsize=10)
    fig.suptitle("Cost, tokens, and time by run")
    save(fig, out / "06_cost_tokens_time.png")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", type=Path, nargs="+", help="eval_runs/<run_id>, in display order")
    parser.add_argument("--label", action="append", default=[], help="<run_id>=<label> naming what changed")
    parser.add_argument("--output", type=Path, help="Output directory (default: plots/comparison_<first>_<last>)")
    args = parser.parse_args()
    out = args.output or HERE / "plots" / f"comparison_{args.runs[0].name}_{args.runs[-1].name}"

    data = [[t for t in load_trials(run) if t.mode == "single"] for run in args.runs]
    for run, ts in zip(args.runs, data):
        if not ts:
            parser.error(f"{run} has no single-mode trials")
    labels = run_labels(args.runs, args.label)

    plot_pass_rates(data, labels, out)
    plot_scenario_heatmap(data, labels, out)
    plot_failure_reasons(data, labels, out)
    plot_usage(data, labels, out)
    plot_function_use(data, labels, out)
    plot_cost_time(data, labels, out)

    summary = {
        run.name: {
            "label": dict(item.split("=", 1) for item in args.label).get(run.name),
            "trials": len(ts),
            "pass_rate": round(float(np.mean([t.outcome == "correct" for t in ts])), 3),
            "one_shot_rate": round(float(np.mean([t.passed_on_attempt == 1 and t.aggregate_calls == 1 for t in ts])), 3),
            "failure_reasons": dict(Counter(t.failure_reason for t in ts if t.failure_reason).most_common()),
            "function_use": dict(Counter(f for t in ts for f in t.functions_used).most_common()),
        }
        for run, ts in zip(args.runs, data)
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
