"""Analyze server ablation runs: what each part of the GUFI MCP server is worth, and why.

    uv run python analyze_ablation.py                       # every finished eval_runs/ablation_* run
    uv run python analyze_ablation.py --runs eval_runs/ablation_full_... eval_runs/ablation_rpath_...
    uv run python analyze_ablation.py --output plots/ablation_pilot

Writes report.md (tables), summary.json, and PNG charts to the output directory.

Effects are "value of a part": pass rate with the part minus pass rate without it, averaged over every
pair of configs that differ only in that part (one pair against `full` in the pilot, 24 pairs per
binary factor in the full factorial). 95% intervals come from a bootstrap that resamples scenarios,
since trials of one scenario are not independent. A part is worth keeping when its interval is above 0.
Scenarios declare in advance which parts they stress (`stresses` in scenarios.single.yaml); the report
splits each effect into targeted vs. other scenarios to show why a part matters.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from plot_run import Trial, load_trials, plt, save  # noqa: E402

from gufi_ablation import CHOICES, FULL, AblationConfig  # noqa: E402

BOOTSTRAPS = 4000
SEED = 20261111

# Each "part" compares the level that has it with the level that does not.
PARTS = [
    ("primer", "primer", True, False, "Primer (server instructions)"),
    ("functions", "functions", True, False, "Functions tools + column tags"),
    ("schema_tool", "schema", "naive", "none", "Schema tool (names + types vs none)"),
    ("schema_hints", "schema", "hints", "naive", "Schema hints (hints vs names + types)"),
    ("schema_full", "schema", "hints", "none", "Schema tool with hints (vs none)"),
    ("path_columns", "path", "server", "rpath", "Server path columns (vs agent rpath)"),
    ("pydantic", "submission", "pydantic", "plain", "Structured submission (vs gufi_query command)"),
]
# Which scenario `stresses` tag targets each part.
PART_STRESS = {"primer": "primer", "functions": "functions", "schema_tool": "schema", "schema_hints": "schema",
               "schema_full": "schema", "path_columns": "path", "pydantic": "submission"}

INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
SURFACE = "#fcfcfb"
SERIES = "#2a78d6"
BLUES = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]


@dataclass
class Run:
    path: Path
    row_id: str
    cfg: AblationConfig
    trials: list[Trial]
    pass_by_scenario: dict[str, list[bool]] = field(default_factory=dict)
    matched_alt: int = 0


def find_runs(paths: list[Path]) -> list[Run]:
    """Finished ablation runs; for repeated configs keep the newest."""
    candidates = []
    for path in paths:
        if (path / "manifest.json").exists() and (path / "config.json").exists():
            candidates.append(path)
        else:
            candidates += [p for p in sorted(path.glob("ablation_*")) if (p / "manifest.json").exists()]
    newest: dict[AblationConfig, Path] = {}
    meta: dict[Path, dict[str, Any]] = {}
    for path in sorted(candidates):
        config = json.loads((path / "config.json").read_text())
        if "ablation" not in config:
            continue
        cfg = AblationConfig.from_mapping(config["ablation"]["factors"])
        newest[cfg] = path
        meta[path] = config
    runs = []
    for cfg, path in newest.items():
        trials = [t for t in load_trials(path) if t.mode == "single"]
        run = Run(path, meta[path]["ablation"]["id"], cfg, trials)
        for line in (path / "summary.jsonl").read_text().splitlines():
            row = json.loads(line)
            run.pass_by_scenario.setdefault(row["scenario_id"], []).append(bool(row["pass"]))
            run.matched_alt += bool(row.get("matched_ground_truth"))
        runs.append(run)
    return runs


def check_comparable(runs: list[Run]) -> list[str]:
    """Settings that differ between runs (they should not)."""
    keys = ("mode", "trials", "max_attempts", "max_tool_calls", "timeout_s", "openrouter_model")
    seen: dict[str, set] = defaultdict(set)
    for run in runs:
        config = json.loads((run.path / "config.json").read_text())
        for key in keys:
            seen[key].add(json.dumps(config.get(key)))
        manifest = json.loads((run.path / "manifest.json").read_text())
        seen["registry"].add(manifest.get("registries", {}).get("single", {}).get("sha256"))
    return [f"{key}: {sorted(values)}" for key, values in seen.items() if len(values) > 1]


def wilson(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = passes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (centre - half, centre + half)


class PassMatrix:
    """Pass counts per (config, scenario), for fast scenario-resampling bootstraps."""

    def __init__(self, runs: list[Run], scenarios: list[str]):
        self.scenarios = scenarios
        self.index = {run.cfg: i for i, run in enumerate(runs)}
        self.passes = np.zeros((len(runs), len(scenarios)))
        self.counts = np.zeros((len(runs), len(scenarios)))
        for run in runs:
            for j, sid in enumerate(scenarios):
                values = run.pass_by_scenario.get(sid, [])
                self.passes[self.index[run.cfg], j] = sum(values)
                self.counts[self.index[run.cfg], j] = len(values)

    def rates(self, weights: np.ndarray) -> np.ndarray:
        """Pass rate of every config with scenarios weighted (bootstrap multiplicities)."""
        with np.errstate(invalid="ignore", divide="ignore"):
            return (self.passes * weights).sum(axis=-1) / (self.counts * weights).sum(axis=-1)


def pairs_for(part: tuple, configs: set[AblationConfig]) -> list[tuple[AblationConfig, AblationConfig]]:
    """(with, without) config pairs that differ only in this part and both ran."""
    _, factor, with_level, without_level, _ = part
    pairs = []
    for cfg in configs:
        if getattr(cfg, factor) != with_level:
            continue
        other = AblationConfig.from_mapping({**cfg.as_dict(), factor: without_level})
        if other in configs:
            pairs.append((cfg, other))
    return pairs


def effect(matrix: PassMatrix, pairs: list, scenario_mask: Optional[np.ndarray] = None,
           rng: Optional[np.random.Generator] = None) -> dict[str, Any]:
    """Mean paired difference in pass rate, with a scenario-bootstrap 95% interval."""
    n = len(matrix.scenarios)
    mask = np.ones(n) if scenario_mask is None else scenario_mask.astype(float)
    if not pairs or mask.sum() == 0:
        return {"effect": None, "lo": None, "hi": None, "pairs": len(pairs), "scenarios": int(mask.sum())}
    a = np.array([matrix.index[w] for w, _ in pairs])
    b = np.array([matrix.index[wo] for _, wo in pairs])
    point_rates = matrix.rates(mask)
    point = float(np.nanmean(point_rates[a] - point_rates[b]))
    rng = rng or np.random.default_rng(SEED)
    chosen = np.flatnonzero(mask)
    draws = rng.choice(chosen, size=(BOOTSTRAPS, len(chosen)), replace=True)
    weights = np.zeros((BOOTSTRAPS, n))
    for j in range(len(chosen)):
        np.add.at(weights, (np.arange(BOOTSTRAPS), draws[:, j]), 1)
    rates = matrix.rates(weights[:, None, :])  # (BOOTSTRAPS, configs)
    diffs = np.nanmean(rates[:, a] - rates[:, b], axis=1)
    lo, hi = np.nanpercentile(diffs, [2.5, 97.5])
    return {"effect": point, "lo": float(lo), "hi": float(hi), "pairs": len(pairs), "scenarios": int(mask.sum())}


def interactions(matrix: PassMatrix, configs: set[AblationConfig]) -> list[dict[str, Any]]:
    """Two-way interaction contrasts for pairs of parts, where full 2x2 squares exist.

    contrast = value of part A when part B is present minus value of A when B is absent. Negative:
    substitutes (each matters less when the other is there); positive: complements.
    """
    out = []
    binary = [p for p in PARTS if p[0] != "schema_full"]
    for pa, pb in itertools.combinations(binary, 2):
        if pa[1] == pb[1]:
            continue
        squares = []
        for cfg in configs:
            if getattr(cfg, pa[1]) != pa[2] or getattr(cfg, pb[1]) != pb[2]:
                continue
            base = cfg.as_dict()
            corners = [AblationConfig.from_mapping({**base, pa[1]: x, pb[1]: y})
                       for x, y in ((pa[2], pb[2]), (pa[3], pb[2]), (pa[2], pb[3]), (pa[3], pb[3]))]
            if all(c in configs for c in corners):
                squares.append([matrix.index[c] for c in corners])
        if not squares:
            continue
        idx = np.array(squares)
        rng = np.random.default_rng(SEED)

        def contrast(rates: np.ndarray) -> np.ndarray:
            r = rates[..., idx]
            return np.nanmean((r[..., 0] - r[..., 1]) - (r[..., 2] - r[..., 3]), axis=-1)

        n = len(matrix.scenarios)
        point = float(contrast(matrix.rates(np.ones(n))))
        draws = rng.integers(0, n, size=(BOOTSTRAPS, n))
        weights = np.zeros((BOOTSTRAPS, n))
        for j in range(n):
            np.add.at(weights, (np.arange(BOOTSTRAPS), draws[:, j]), 1)
        boot = contrast(matrix.rates(weights[:, None, :]))
        lo, hi = np.nanpercentile(boot, [2.5, 97.5])
        out.append({"a": pa[0], "b": pb[0], "contrast": point, "lo": float(lo), "hi": float(hi),
                    "squares": len(squares)})
    return out


def verdict(main: dict[str, Any], cost_delta: Optional[float]) -> str:
    if main["effect"] is None:
        return "not measured"
    if main["lo"] > 0:
        return "keep: helps"
    if main["hi"] < 0:
        return "hurts: remove or rework"
    if cost_delta is not None and cost_delta > 0:
        return "no detectable gain; costs more: simplify candidate"
    return "no detectable effect"


#
# Charts (static PNGs; report.md carries the same numbers as tables)
#

def _style(ax: plt.Axes) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=INK_2, labelsize=9)
    ax.title.set_color(INK)


def plot_effects(rows: list[dict[str, Any]], out: Path) -> None:
    rows = [r for r in rows if r["all"]["effect"] is not None]
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(9, 0.55 * len(rows) + 1.6), facecolor=SURFACE)
    _style(ax)
    y = np.arange(len(rows))[::-1]
    for yi, r in zip(y, rows):
        e = r["all"]
        ax.plot([e["lo"] * 100, e["hi"] * 100], [yi, yi], color=SERIES, lw=2, solid_capstyle="round")
        ax.plot(e["effect"] * 100, yi, "o", color=SERIES, ms=8, mec=SURFACE, mew=2)
        ax.text(max(e["hi"] * 100, e["effect"] * 100) + 1.5, yi, f"{e['effect'] * 100:+.1f} pp", va="center",
                fontsize=9, color=INK_2)
    ax.axvline(0, color=MUTED, lw=1)
    ax.set_yticks(y, [r["label"] for r in rows], color=INK)
    ax.set_xlabel("Pass rate with the part minus without it (percentage points, 95% scenario-bootstrap CI)",
                  color=INK_2, fontsize=9)
    ax.set_title("Value of each server part", loc="left", fontsize=11)
    ax.grid(axis="x", color="#e9e8e4", lw=0.8)
    ax.set_axisbelow(True)
    save(fig, out / "01_part_effects.png")


def plot_heatmap(values: np.ndarray, row_labels: list[str], col_labels: list[str], title: str, path: Path,
                 fmt: str = "{:.0%}", vmax: Optional[float] = None) -> None:
    from matplotlib.colors import LinearSegmentedColormap

    cmap = LinearSegmentedColormap.from_list("blues", BLUES)
    fig, ax = plt.subplots(figsize=(1.0 + 0.62 * len(col_labels), 1.2 + 0.42 * len(row_labels)), facecolor=SURFACE)
    _style(ax)
    vmax = vmax if vmax is not None else (np.nanmax(values) if np.isfinite(values).any() else 1)
    im = ax.imshow(values, cmap=cmap, vmin=0, vmax=vmax or 1, aspect="auto")
    for i, j in itertools.product(range(values.shape[0]), range(values.shape[1])):
        v = values[i, j]
        if np.isfinite(v):
            dark = v / (vmax or 1) > 0.55
            ax.text(j, i, fmt.format(v), ha="center", va="center", fontsize=7.5, color="#ffffff" if dark else INK)
    ax.set_xticks(range(len(col_labels)), col_labels, rotation=40, ha="right")
    ax.set_yticks(range(len(row_labels)), row_labels)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title(title, loc="left", fontsize=11)
    cbar = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01)
    cbar.outline.set_visible(False)
    cbar.ax.tick_params(colors=INK_2, labelsize=8)
    save(fig, path)


def plot_config_bars(summary: list[dict[str, Any]], key: str, title: str, xlabel: str, path: Path,
                     ci: bool = False, pct: bool = False) -> None:
    fig, ax = plt.subplots(figsize=(9, 0.42 * len(summary) + 1.5), facecolor=SURFACE)
    _style(ax)
    y = np.arange(len(summary))[::-1]
    scale = 100 if pct else 1
    values = [s[key] * scale for s in summary]
    ax.barh(y, values, height=0.6, color=SERIES, edgecolor=SURFACE, linewidth=2)
    if ci:
        for yi, s in zip(y, summary):
            ax.plot([s["ci_lo"] * scale, s["ci_hi"] * scale], [yi, yi], color=INK_2, lw=1.2)
    for yi, v in zip(y, values):
        ax.text(v + (max(values) or 1) * 0.01, yi + 0.22, f"{v:.0f}%" if pct else f"{v:.3g}", fontsize=8, color=INK_2)
    ax.set_yticks(y, [s["label"] for s in summary])
    ax.set_xlabel(xlabel, color=INK_2, fontsize=9)
    ax.set_title(title, loc="left", fontsize=11)
    ax.grid(axis="x", color="#e9e8e4", lw=0.8)
    ax.set_axisbelow(True)
    save(fig, path)


#
# Report
#

def pct(x: Optional[float]) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{x * 100:+.1f}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", nargs="*", type=Path, default=[HERE / "eval_runs"],
                        help="Run directories, or directories holding ablation_* runs")
    parser.add_argument("--output", type=Path, default=HERE / "plots" / "ablation")
    parser.add_argument("--scenarios", type=Path, default=HERE / "scenarios.single.yaml")
    args = parser.parse_args(argv)

    runs = find_runs(args.runs)
    if not runs:
        print("no finished ablation runs found", file=sys.stderr)
        return 1
    order = {cfg: i for i, cfg in enumerate([FULL])}
    runs.sort(key=lambda r: (order.get(r.cfg, 1), r.row_id))
    args.output.mkdir(parents=True, exist_ok=True)
    mismatches = check_comparable(runs)

    registry = {s["id"]: s for s in yaml.safe_load(args.scenarios.read_text())}
    scenarios = sorted({sid for run in runs for sid in run.pass_by_scenario})
    matrix = PassMatrix(runs, scenarios)
    configs = {run.cfg for run in runs}

    # Per-config summary
    summary = []
    for run in runs:
        flat = [p for values in run.pass_by_scenario.values() for p in values]
        passes, n = sum(flat), len(flat)
        lo, hi = wilson(passes, n)
        ts = run.trials
        discovery = [sum(c for name, c in t.tool_calls.items() if name != "aggregate_sql_query") for t in ts]
        summary.append({
            "label": run.row_id, "config_id": run.cfg.id, "factors": run.cfg.as_dict(), "trials": n,
            "pass_rate": passes / n if n else float("nan"), "ci_lo": lo, "ci_hi": hi,
            "one_shot": float(np.mean([t.passed_on_attempt == 1 and t.aggregate_calls == 1 for t in ts])) if ts else None,
            "mean_tool_calls": float(np.mean([t.n_calls for t in ts])) if ts else None,
            "mean_discovery_calls": float(np.mean(discovery)) if ts else None,
            "mean_aggregate_calls": float(np.mean([t.aggregate_calls for t in ts])) if ts else None,
            "mean_errors": float(np.mean([t.n_errors for t in ts])) if ts else None,
            "mean_cost": float(np.mean([t.cost for t in ts])) if ts else None,
            "mean_tokens": float(np.mean([t.tokens for t in ts])) if ts else None,
            "mean_seconds": float(np.mean([t.duration_s for t in ts])) if ts else None,
            "alternate_matches": run.matched_alt, "run": run.path.name,
        })
    by_cfg = {s["config_id"]: s for s in summary}

    # Effects of each part, overall and on targeted vs. other scenarios
    effects = []
    for part in PARTS:
        pairs = pairs_for(part, configs)
        if not pairs:
            continue
        tag = PART_STRESS[part[0]]
        targeted = np.array([tag in registry.get(sid, {}).get("stresses", []) for sid in scenarios])
        cost_delta = float(np.mean([by_cfg[w.id]["mean_tokens"] - by_cfg[wo.id]["mean_tokens"] for w, wo in pairs]))
        main_effect = effect(matrix, pairs)
        effects.append({
            "part": part[0], "label": part[4], "pairs": len(pairs),
            "all": main_effect,
            "targeted": effect(matrix, pairs, targeted),
            "other": effect(matrix, pairs, ~targeted),
            "token_delta": cost_delta,
            "verdict": verdict(main_effect, cost_delta),
            "per_scenario": {sid: float(np.mean([matrix.passes[matrix.index[w], j] / max(matrix.counts[matrix.index[w], j], 1)
                                                 - matrix.passes[matrix.index[wo], j] / max(matrix.counts[matrix.index[wo], j], 1)
                                                 for w, wo in pairs]))
                             for j, sid in enumerate(scenarios)},
        })
    inter = interactions(matrix, configs)

    # Error categories per config
    error_counts = {run.row_id: Counter(c for t in run.trials for err in t.errors for c in err) for run in runs}
    categories = [c for c, _ in sum(error_counts.values(), Counter()).most_common(12)]

    # Charts
    plot_effects(effects, args.output)
    rates = np.array([[np.mean(run.pass_by_scenario[sid]) if run.pass_by_scenario.get(sid) else np.nan
                       for sid in scenarios] for run in runs])
    plot_heatmap(rates, [r.row_id for r in runs], scenarios, "Pass rate by config and scenario",
                 args.output / "02_config_scenario_pass.png", vmax=1)
    if effects:
        diff = np.array([[e["per_scenario"][sid] for sid in scenarios] for e in effects])
        plot_heatmap(np.abs(diff), [e["label"] for e in effects], scenarios,
                     "Size of each part's effect per scenario (|pp|; sign in report.md)",
                     args.output / "03_part_scenario_effect.png", fmt="{:.0%}", vmax=1)
    if categories:
        errs = np.array([[error_counts[r.row_id][c] / max(len(r.trials), 1) for c in categories] for r in runs])
        plot_heatmap(errs, [r.row_id for r in runs], categories, "Failed tool calls per trial, by error category",
                     args.output / "04_error_categories.png", fmt="{:.2f}")
    plot_config_bars(summary, "pass_rate", "Pass rate by config (95% Wilson CI)", "pass rate (%)",
                     args.output / "05_pass_rate.png", ci=True, pct=True)
    plot_config_bars(summary, "mean_tokens", "Mean tokens per trial by config", "tokens per trial",
                     args.output / "06_tokens.png")
    plot_config_bars(summary, "mean_tool_calls", "Mean tool calls per trial by config", "tool calls per trial",
                     args.output / "07_tool_calls.png")

    # report.md
    lines = ["# GUFI MCP server ablation", "",
             f"{len(runs)} config(s), {len(scenarios)} scenario(s), {sum(s['trials'] for s in summary)} graded trial(s).", ""]
    if mismatches:
        lines += ["**Warning: runs differ in settings, so comparisons are not clean:**", ""]
        lines += [f"- {m}" for m in mismatches] + [""]
    lines += ["## Configs", "",
              "| config | factors | pass | 95% CI | one-shot | tool calls | discovery | aggregate | errors | tokens | cost $ | s |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in summary:
        f = s["factors"]
        factors = f"{f['submission']}, schema={f['schema']}, primer={int(f['primer'])}, fn={int(f['functions'])}, {f['path']}"
        lines.append(f"| {s['label']} | {factors} | {s['pass_rate']:.0%} | {s['ci_lo']:.0%}–{s['ci_hi']:.0%} | "
                     f"{s['one_shot']:.0%} | {s['mean_tool_calls']:.1f} | {s['mean_discovery_calls']:.1f} | "
                     f"{s['mean_aggregate_calls']:.2f} | {s['mean_errors']:.2f} | {s['mean_tokens']:.0f} | "
                     f"{s['mean_cost']:.4f} | {s['mean_seconds']:.1f} |")
    lines += ["", "## Value of each part", "",
              "Pass rate with the part minus without it, in percentage points, averaged over config pairs that "
              "differ only in that part. Intervals: 95% bootstrap over scenarios. *Targeted* = scenarios whose "
              "`stresses` name the part.", "",
              "| part | pairs | effect (pp) | 95% CI | targeted (pp) | other (pp) | Δ tokens/trial | verdict |",
              "|---|---|---|---|---|---|---|---|"]
    for e in effects:
        a, t, o = e["all"], e["targeted"], e["other"]
        lines.append(f"| {e['label']} | {e['pairs']} | {pct(a['effect'])} | {pct(a['lo'])} to {pct(a['hi'])} | "
                     f"{pct(t['effect'])} (n={t['scenarios']}) | {pct(o['effect'])} (n={o['scenarios']}) | "
                     f"{e['token_delta']:+.0f} | {e['verdict']} |")
    if effects:
        lines += ["", "### Per-scenario effect (pp)", "",
                  "| scenario | stresses | " + " | ".join(e["part"] for e in effects) + " |",
                  "|---|---|" + "---|" * len(effects)]
        for sid in scenarios:
            stresses = ", ".join(registry.get(sid, {}).get("stresses", []))
            lines.append(f"| {sid} | {stresses} | " + " | ".join(pct(e["per_scenario"][sid]) for e in effects) + " |")
    if inter:
        lines += ["", "## Interactions", "",
                  "Value of A when B is present minus when B is absent. Negative: substitutes; positive: complements.", "",
                  "| A | B | contrast (pp) | 95% CI | squares |", "|---|---|---|---|---|"]
        for i in inter:
            lines.append(f"| {i['a']} | {i['b']} | {pct(i['contrast'])} | {pct(i['lo'])} to {pct(i['hi'])} | {i['squares']} |")
    lines += ["", "## Pass rate by scenario", "", "| scenario | " + " | ".join(r.row_id for r in runs) + " |",
              "|---|" + "---|" * len(runs)]
    for j, sid in enumerate(scenarios):
        lines.append(f"| {sid} | " + " | ".join("n/a" if not np.isfinite(rates[i, j]) else f"{rates[i, j]:.0%}"
                                             for i in range(len(runs))) + " |")
    if categories:
        lines += ["", "## Failed tool calls per trial, by error category", "",
                  "| config | " + " | ".join(categories) + " |", "|---|" + "---|" * len(categories)]
        for r in runs:
            lines.append(f"| {r.row_id} | " + " | ".join(f"{error_counts[r.row_id][c] / max(len(r.trials), 1):.2f}"
                                                       for c in categories) + " |")
    lines += ["", "## Runs", ""] + [f"- {s['label']}: `{s['run']}`" for s in summary]
    (args.output / "report.md").write_text("\n".join(lines) + "\n")
    (args.output / "summary.json").write_text(json.dumps(
        {"configs": summary, "effects": effects, "interactions": inter, "error_counts": {k: dict(v) for k, v in error_counts.items()},
         "mismatches": mismatches}, indent=2, default=float) + "\n")
    print(f"Report: {args.output / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
