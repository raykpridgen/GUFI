"""Row comparison between ground truth and agent results (eval_design.md Part 3)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from itertools import permutations
from typing import Any, Optional

import sqlglot
from sqlglot import exp

FLOAT_DIGITS = 6
MAX_PERMUTED_COLUMNS = 6


def normalize_value(value: Any) -> Any:
    """Make equal values compare equal regardless of how SQLite or JSON typed them."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            value = int(text)
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return round(value, FLOAT_DIGITS)
    return value


def normalize_rows(rows: list[list[Any]]) -> list[tuple]:
    return [tuple(normalize_value(v) for v in row) for row in rows]


def _sort_key(row: tuple) -> tuple:
    # Mixed types (None, numbers, text) must still sort deterministically.
    return tuple((v is None, isinstance(v, str), str(v) if isinstance(v, str) else v if v is not None else 0) for v in row)


def is_ordered(final_sql: str) -> bool:
    """True when the outermost SELECT of final_sql has an ORDER BY."""
    try:
        tree = sqlglot.parse_one(final_sql, read="sqlite")
    except sqlglot.errors.ParseError:
        return False
    return isinstance(tree, exp.Select) and tree.args.get("order") is not None


@dataclass
class Comparison:
    passed: bool
    ordered: bool
    column_permutation: Optional[list[int]]
    reason: str
    expected: list[tuple] = field(default_factory=list)
    actual: list[tuple] = field(default_factory=list)
    missing: list[tuple] = field(default_factory=list)
    extra: list[tuple] = field(default_factory=list)

    @property
    def column_permuted(self) -> bool:
        return self.column_permutation is not None and self.column_permutation != sorted(self.column_permutation)


def _diff(want: list[tuple], got: list[tuple]) -> tuple[list[tuple], list[tuple]]:
    want_count, got_count = Counter(want), Counter(got)
    missing = sorted((want_count - got_count).elements(), key=_sort_key)
    extra = sorted((got_count - want_count).elements(), key=_sort_key)
    return missing, extra


def compare_rows(expected: list[list[Any]], actual: list[list[Any]], ordered: bool) -> Comparison:
    """Rows must match after normalization; column names are ignored, column order may differ.

    Unordered compares sort both sides first. The returned expected/actual are in compared order (actual with
    the best column permutation applied) so reports can show them side by side with missing/extra rows.
    """
    want = normalize_rows(expected)
    got = normalize_rows(actual)
    if not ordered:
        want = sorted(want, key=_sort_key)
    widths = {len(r) for r in want} | {len(r) for r in got}
    if len(widths) > 1:
        width = len(want[0]) if want else 0
        return Comparison(False, ordered, None, f"column counts differ: expected {width}, got {len(got[0])}",
                          want, got)
    if not want and not got:
        return Comparison(True, ordered, None, "both empty")
    width = widths.pop()

    identity = list(range(width))
    candidates = permutations(identity) if width <= MAX_PERMUTED_COLUMNS else [tuple(identity)]
    best: Optional[tuple[int, list[int], list[tuple]]] = None
    for perm in candidates:
        reordered = [tuple(row[i] for i in perm) for row in got]
        if not ordered:
            reordered = sorted(reordered, key=_sort_key)
        if reordered == want:
            return Comparison(True, ordered, list(perm), "match", want, reordered)
        missing, _ = _diff(want, reordered)
        if best is None or len(missing) < best[0]:
            best = (len(missing), list(perm), reordered)

    _, perm, reordered = best
    missing, extra = _diff(want, reordered)
    if len(want) != len(reordered):
        reason = f"row count {len(reordered)} != expected {len(want)}"
    elif not missing:
        reason = "same rows, different order"
    else:
        reason = f"values differ: {len(missing)} expected row(s) missing, {len(extra)} unexpected"
    return Comparison(False, ordered, None, reason, want, reordered, missing, extra)
