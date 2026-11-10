#!/usr/bin/env python3
"""Cross-iteration plots for GUFI MCP eval runs (single + general, excl. user-facing tools).

    .venv/bin/python analyze_iterations.py

Writes plots/iterations_all/ covering every comparable run in this directory and old/.
Trials that called user-facing tools (gufi_find, gufi_ls, gufi_stats, gufi_du, gufi_stat)
are excluded from graded metrics, per plan.md.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

RUNS_DIR = Path(__file__).resolve().parent
EVAL_DIR = RUNS_DIR.parent
PKG_DIR = EVAL_DIR.parent
OUT = RUNS_DIR / "plots" / "iterations_all"

sys.path[:0] = [str(PKG_DIR), str(EVAL_DIR)]

from plot_run import (  # noqa: E402
    COLORS,
    FAILURE_COLORS,
    GUFI_FUNCTIONS,
    Trial,
    answer_matches,
    load_trials,
    save,
)
import gufi_mcp_util as util  # noqa: E402
from eval_lib.registry import load_manifest, load_registry, render_scenario  # noqa: E402

USER_FACING = {"gufi_find", "gufi_ls", "gufi_stats", "gufi_du", "gufi_stat"}

# Chronological design iterations. Smoke test 535d0f (12 trials) omitted.
# General runs use soft final-text grading; single runs use exact row match.
ITERATIONS: list[tuple[str, str, Path]] = [
    ("G1. full tools / sname paths", "general", RUNS_DIR / "old" / "20261014T173900Z-c0641c"),
    ("G2. path columns", "general", RUNS_DIR / "old" / "20261015T043612Z-ceed58"),
    ("S0. first single suite", "single", RUNS_DIR / "old" / "20261015T050030Z-90fa7d"),
    ("G3. primer / path fixes", "general", RUNS_DIR / "old" / "20261019T054117Z-8c9c78"),
    ("G4. cleaner aggregate path", "general", RUNS_DIR / "old" / "20261019T060635Z-d4ac4e"),
    ("S1. baseline (new grading)", "single", RUNS_DIR / "20261026T133502Z-28ac34"),
    ("S2. histogram template", "single", RUNS_DIR / "20261026T134502Z-c3e789"),
    ("S3. UDF column hints", "single", RUNS_DIR / "20261026T140520Z-f2769d"),
    ("S4. UDF primer lines", "single", RUNS_DIR / "20261026T141318Z-886bc0"),
    ("S5. expanded column tags", "single", RUNS_DIR / "20261026T144644Z-39600c"),
]


def used_user_facing(t: Trial) -> bool:
    return bool(set(t.tool_calls) & USER_FACING)


def ground_truth_rows() -> dict[str, list[list]]:
    rows: dict[str, list[list]] = {}
    manifests: dict = {}
    for scenario in load_registry(EVAL_DIR / "scenarios.single.yaml", "single"):
        if scenario.index not in manifests:
            manifests[scenario.index] = load_manifest(
                util.GUFI_INDEX_ROOT / f"{scenario.index}.manifest.json"
            )
        item = render_scenario(scenario, "single", manifests[scenario.index])
        query = util.GufiAggregateQuery.model_validate({**item.ground_truth, "index": scenario.index})
        rows[scenario.id] = util.run_aggregate(query).rows
    return rows


def grade_trials(trials: list[Trial], mode: str, gt: dict[str, list[list]]) -> list[Trial]:
    """Return graded trials, excluding user-facing tool use. Mutates outcome for general."""
    kept = []
    for t in trials:
        if t.mode != mode:
            continue
        if used_user_facing(t):
            continue
        if mode == "single":
            if t.outcome in ("correct", "incorrect"):
                kept.append(t)
            continue
        # general: soft-grade mirrored scenarios only
        rows = gt.get(t.scenario)
        if not rows:
            continue
        t.outcome = "correct" if answer_matches(getattr(t, "final_text", "") or "", rows) else "incorrect"
        kept.append(t)
    return kept


def short_label(label: str, run: Path) -> str:
    return f"{label}\n{run.name[-6:]}"


def plot_pass_rates(series: list[tuple[str, list[Trial]]], out: Path) -> None:
    labels = [s[0] for s in series]
    data = [s[1] for s in series]
    x = np.arange(len(labels))
    width = 0.38
    passed = [np.mean([t.outcome == "correct" for t in ts]) if ts else 0 for ts in data]
    one_shot = [
        np.mean([t.passed_on_attempt == 1 and t.aggregate_calls == 1 for t in ts]) if ts else 0
        for ts in data
    ]
    # one-shot only meaningful for single; still plot for visibility
    fig, ax = plt.subplots(figsize=(max(10, len(labels) * 1.35), 5.5))
    bars = ax.bar(x - width / 2, passed, width, color=COLORS["correct"], label="pass (graded)")
    bars2 = ax.bar(x + width / 2, one_shot, width, color=COLORS["model"], label="one-shot pass (single only)")
    for bar, v in zip(bars, passed):
        ax.text(bar.get_x() + bar.get_width() / 2, v, f"{v:.0%}", ha="center", va="bottom", fontsize=8)
    for bar, v, ts in zip(bars2, one_shot, data):
        if ts and any(t.mode == "single" for t in ts):
            ax.text(bar.get_x() + bar.get_width() / 2, v, f"{v:.0%}", ha="center", va="bottom", fontsize=8)
    ax.set_xticks(x, labels, fontsize=7)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("share of graded trials")
    ax.set_title("Pass rate by design iteration (user-facing tool trials excluded)")
    ax.legend(loc="upper left")
    save(fig, out / "01_pass_rates.png")


def plot_scenario_heatmap(series: list[tuple[str, list[Trial]]], out: Path) -> None:
    labels = [s[0] for s in series]
    data = [s[1] for s in series]
    scenarios = sorted({t.scenario for ts in data for t in ts})
    matrix = np.array(
        [
            [np.mean([t.outcome == "correct" for t in ts if t.scenario == s] or [np.nan]) for ts in data]
            for s in scenarios
        ]
    )
    order = np.argsort(np.nanmean(matrix, axis=1))
    matrix = matrix[order]
    scenarios = [scenarios[i] for i in order]
    fig, ax = plt.subplots(figsize=(max(10, len(labels) * 1.2 + 3), max(5, len(scenarios) * 0.45)))
    im = ax.imshow(matrix, cmap="RdYlGn", vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(labels)), labels, fontsize=7)
    ax.set_yticks(range(len(scenarios)), scenarios, fontsize=8)
    for i in range(len(scenarios)):
        for j in range(len(labels)):
            if not np.isnan(matrix[i, j]):
                ax.text(j, i, f"{matrix[i, j]:.0%}", ha="center", va="center", fontsize=7)
    ax.set_title("Pass rate by scenario and iteration (hardest scenarios at top)")
    fig.colorbar(im, ax=ax, shrink=0.7)
    save(fig, out / "02_scenario_pass_heatmap.png")


def plot_hard_scenarios(series: list[tuple[str, list[Trial]]], out: Path) -> None:
    """Mean pass rate across iterations per scenario — which prompts stay hard."""
    by_sc: dict[str, list[float]] = defaultdict(list)
    for _, ts in series:
        for s in {t.scenario for t in ts}:
            rates = [t.outcome == "correct" for t in ts if t.scenario == s]
            if rates:
                by_sc[s].append(float(np.mean(rates)))
    scenarios = sorted(by_sc, key=lambda s: np.mean(by_sc[s]))
    means = [np.mean(by_sc[s]) for s in scenarios]
    mins = [np.min(by_sc[s]) for s in scenarios]
    maxs = [np.max(by_sc[s]) for s in scenarios]
    y = np.arange(len(scenarios))
    fig, ax = plt.subplots(figsize=(9, max(5, len(scenarios) * 0.4)))
    ax.barh(y, means, color="#455a64", label="mean across iterations")
    ax.hlines(y, mins, maxs, color="#90a4ae", lw=2, label="min–max across iterations")
    ax.set_yticks(y, scenarios, fontsize=8)
    ax.set_xlim(0, 1.05)
    ax.set_xlabel("pass rate")
    ax.set_title("Which prompts stay hard? Mean pass rate across iterations")
    ax.legend(loc="lower right")
    save(fig, out / "03_hard_scenarios.png")


def plot_failure_reasons(series: list[tuple[str, list[Trial]]], out: Path) -> None:
    single_series = [(lab, [t for t in ts if t.mode == "single"]) for lab, ts in series]
    single_series = [(lab, ts) for lab, ts in single_series if ts]
    if not single_series:
        return
    labels = [s[0] for s in single_series]
    data = [s[1] for s in single_series]
    reasons = [r for r, _ in Counter(t.failure_reason for ts in data for t in ts if t.failure_reason).most_common()]
    x = np.arange(len(labels))
    bottom = np.zeros(len(labels))
    fig, ax = plt.subplots(figsize=(max(9, len(labels) * 1.5), 5.5))
    passed = np.array([sum(t.outcome == "correct" for t in ts) for ts in data], dtype=float)
    ax.bar(x, passed, color=COLORS["correct"], label="pass")
    bottom += passed
    for reason in reasons:
        values = np.array([sum(t.failure_reason == reason for t in ts) for ts in data], dtype=float)
        ax.bar(x, values, bottom=bottom, color=FAILURE_COLORS.get(reason, FAILURE_COLORS["other"]), label=reason)
        for xi, b, v in zip(x, bottom, values):
            if v:
                ax.text(xi, b + v / 2, int(v), ha="center", va="center", fontsize=7, color="white")
        bottom += values
    ax.set_xticks(x, labels, fontsize=7)
    ax.set_ylabel("trials")
    ax.set_title("Single-mode outcomes by iteration")
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8)
    save(fig, out / "04_failure_reasons_single.png")


def plot_tool_mix(series: list[tuple[str, Path, list[Trial]]], out: Path) -> None:
    """Share of all trials (before UF filter) that used user-facing tools, plus aggregate-only share."""
    labels, uf_share, agg_only = [], [], []
    for label, run, _graded in series:
        all_trials = load_trials(run)
        n = len(all_trials) or 1
        uf = sum(used_user_facing(t) for t in all_trials)
        labels.append(label)
        uf_share.append(uf / n)
        agg_only.append(
            sum((not t.tool_calls) or (set(t.tool_calls) <= {"aggregate_sql_query"}) for t in all_trials) / n
        )
    x = np.arange(len(labels))
    width = 0.38
    fig, ax = plt.subplots(figsize=(max(10, len(labels) * 1.35), 5))
    ax.bar(x - width / 2, uf_share, width, color="#c62828", label="used gufi_find/ls/stats/du/stat")
    ax.bar(x + width / 2, agg_only, width, color=COLORS["tools"], label="aggregate_sql_query only (or no tools)")
    ax.set_xticks(x, labels, fontsize=7)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("share of all trials")
    ax.set_title("User-facing tool use by iteration (these trials are discounted from pass metrics)")
    ax.legend()
    save(fig, out / "05_user_facing_tool_share.png")


def plot_function_use(series: list[tuple[str, list[Trial]]], out: Path) -> None:
    single = [(lab, [t for t in ts if t.mode == "single"]) for lab, ts in series]
    single = [(lab, ts) for lab, ts in single if ts]
    if not single:
        return
    labels = [s[0] for s in single]
    data = [s[1] for s in single]
    pairs = sorted(
        {(t.scenario, f) for ts in data for t in ts for f in t.functions_used},
        key=lambda p: (GUFI_FUNCTIONS.index(p[1]) if p[1] in GUFI_FUNCTIONS else 99, p[0]),
    )
    if not pairs:
        return
    matrix = np.array(
        [[np.mean([f in t.functions_used for t in ts if t.scenario == s] or [0]) for ts in data] for s, f in pairs]
    )
    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 1.3 + 4), max(4, len(pairs) * 0.4)))
    im = ax.imshow(matrix, cmap="Blues", vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(labels)), labels, fontsize=7)
    ax.set_yticks(range(len(pairs)), [f"{f}() in {s}" for s, f in pairs], fontsize=8)
    for i in range(len(pairs)):
        for j in range(len(labels)):
            if matrix[i, j]:
                ax.text(j, i, f"{matrix[i, j]:.0%}", ha="center", va="center", fontsize=7)
    ax.set_title("GUFI function use in aggregate SQL (single-mode)")
    fig.colorbar(im, ax=ax, shrink=0.7)
    save(fig, out / "06_gufi_function_use.png")


def plot_cost_time(series: list[tuple[str, list[Trial]]], out: Path) -> None:
    labels = [s[0] for s in series]
    data = [s[1] for s in series]
    metrics = [
        ("mean cost (milli-USD)", lambda t: t.cost * 1000, COLORS["model"]),
        ("mean tokens (thousands)", lambda t: t.tokens / 1000, COLORS["tools"]),
        ("mean seconds", lambda t: t.duration_s, COLORS["resources"]),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(max(14, len(labels) * 1.2), 4.5))
    x = np.arange(len(labels))
    for ax, (title, fn, color) in zip(axes, metrics):
        values = [np.mean([fn(t) for t in ts]) if ts else 0 for ts in data]
        bars = ax.bar(x, values, color=color)
        for bar, v in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, v, f"{v:.1f}", ha="center", va="bottom", fontsize=7)
        ax.set_xticks(x, labels, fontsize=6)
        ax.set_title(title, fontsize=10)
    fig.suptitle("Cost, tokens, and time by iteration (graded, UF-excluded trials)")
    save(fig, out / "07_cost_tokens_time.png")


def plot_single_only_pass(series: list[tuple[str, list[Trial]]], out: Path) -> None:
    """Dedicated single-mode timeline — apples-to-apples exact grading."""
    single = [(lab, ts) for lab, ts in series if ts and all(t.mode == "single" for t in ts)]
    if len(single) < 2:
        return
    labels = [s[0] for s in single]
    data = [s[1] for s in single]
    x = np.arange(len(labels))
    passed = [np.mean([t.outcome == "correct" for t in ts]) for ts in data]
    one = [np.mean([t.passed_on_attempt == 1 and t.aggregate_calls == 1 for t in ts]) for ts in data]
    fig, ax = plt.subplots(figsize=(max(9, len(labels) * 1.6), 5))
    ax.plot(x, passed, "o-", color=COLORS["correct"], label="pass (exact row match)", lw=2)
    ax.plot(x, one, "s--", color=COLORS["model"], label="one-shot pass", lw=1.5)
    for i, (p, o) in enumerate(zip(passed, one)):
        ax.annotate(f"{p:.0%}", (i, p), textcoords="offset points", xytext=(0, 8), ha="center", fontsize=9)
        ax.annotate(f"{o:.0%}", (i, o), textcoords="offset points", xytext=(0, -14), ha="center", fontsize=8, color=COLORS["model"])
    ax.set_xticks(x, labels, fontsize=8)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("share of trials")
    ax.set_title("Single-mode exact pass rate across design iterations")
    ax.legend(loc="lower right")
    ax.grid(axis="y", alpha=0.3)
    save(fig, out / "08_single_mode_trend.png")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("loading ground truth for general-mode soft grading...")
    gt = ground_truth_rows()

    graded_series: list[tuple[str, list[Trial]]] = []
    meta_series: list[tuple[str, Path, list[Trial]]] = []
    summary: dict = {}

    for label, mode, run in ITERATIONS:
        if not run.exists():
            print(f"skip missing {run}")
            continue
        print(f"loading {run.name} ({mode})...")
        trials = load_trials(run)
        graded = grade_trials(trials, mode, gt)
        short = short_label(label, run)
        graded_series.append((short, graded))
        meta_series.append((short, run, graded))
        uf_n = sum(used_user_facing(t) for t in trials)
        summary[run.name] = {
            "label": label,
            "mode": mode,
            "all_trials": len(trials),
            "user_facing_trials_excluded": uf_n,
            "graded_trials": len(graded),
            "pass_rate": round(float(np.mean([t.outcome == "correct" for t in graded])), 3) if graded else None,
            "one_shot_rate": (
                round(float(np.mean([t.passed_on_attempt == 1 and t.aggregate_calls == 1 for t in graded])), 3)
                if graded and mode == "single"
                else None
            ),
            "pass_by_scenario": {
                s: round(float(np.mean([t.outcome == "correct" for t in graded if t.scenario == s])), 3)
                for s in sorted({t.scenario for t in graded})
            },
            "failure_reasons": dict(Counter(t.failure_reason for t in graded if t.failure_reason).most_common())
            if mode == "single"
            else {},
            "function_use": dict(Counter(f for t in graded for f in t.functions_used).most_common())
            if mode == "single"
            else {},
        }
        print(
            f"  graded {len(graded)}/{len(trials)} (excl {uf_n} UF) "
            f"pass={summary[run.name]['pass_rate']}"
        )

    plot_pass_rates(graded_series, OUT)
    plot_scenario_heatmap(graded_series, OUT)
    plot_hard_scenarios(graded_series, OUT)
    plot_failure_reasons(graded_series, OUT)
    plot_tool_mix(meta_series, OUT)
    plot_function_use(graded_series, OUT)
    plot_cost_time(graded_series, OUT)
    plot_single_only_pass(graded_series, OUT)

    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: {**v, "pass_by_scenario": "..."} for k, v in summary.items()}, indent=2))
    print(f"wrote plots to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
