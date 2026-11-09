"""Export aggregate queries from eval traces and scenarios, with the Python validator's verdict.

The Go server's validator is tested against this file (go/testdata/corpus.json): for each
query it must accept or reject exactly as the Python server does.

    uv run python export_sql_corpus.py [--out ../go/testdata/corpus.json]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("SCHEMAFILE", str(ROOT / "schema_hints.json"))
os.environ.setdefault("GUFIVTLIB", "gufi_vt")
os.environ.setdefault("GUFI_INDEX_ROOT", "/indexes")

import yaml  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import gufi_mcp_util as util  # noqa: E402

FIELDS = ("index", "per_directory_sql", "per_directory_shape", "partial_sql", "partial_shape", "final_sql")
INDEXES = {"eval_master": True, "flat": False}
FIXED_NOW = 1759276800


def render(value, values: dict):
    if isinstance(value, str):
        for key, replacement in values.items():
            value = value.replace("{" + key + "}", str(replacement))
        return value
    if isinstance(value, list):
        return [render(item, values) for item in value]
    if isinstance(value, dict):
        return {key: render(item, values) for key, item in value.items()}
    return value


def queries_in(value):
    if isinstance(value, dict):
        if "per_directory_sql" in value and "final_sql" in value:
            yield value
        for item in value.values():
            yield from queries_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from queries_in(item)


def collect() -> list[dict]:
    found = []
    for path in sorted((ROOT / "eval_runs").rglob("*.trace.json")):
        try:
            found.extend(queries_in(json.loads(path.read_text())))
        except ValueError:
            continue
    for path in sorted(ROOT.glob("scenarios*.yaml")):
        for scenario in yaml.safe_load(path.read_text()) or []:
            truth = scenario.get("ground_truth")
            if isinstance(truth, dict):
                values = {"fixed_now": FIXED_NOW, "index": scenario.get("index", "eval_master"),
                          **{f"parameters.{k}": v for k, v in (scenario.get("parameters") or {}).items()}}
                found.append({**render(truth, values), "index": values["index"]})
    unique, seen = [], set()
    for query in found:
        query = {key: query[key] for key in FIELDS if key in query}
        query.setdefault("index", "eval_master")
        key = json.dumps(query, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(query)
    return unique


def verdict(query: dict) -> dict:
    try:
        model = util.GufiAggregateQuery.model_validate(query)
    except ValidationError as e:
        errors = e.errors()
        return {"ok": False, "error": str(errors[0]["msg"]).removeprefix("Value error, ") if errors else str(e)}
    sql = model.build_query_string()
    return {"ok": True, "phases": {phase: sql for phase, sql in model._phases.items()}, "create": sql}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT.parent / "go" / "testdata" / "corpus.json")
    args = parser.parse_args()

    util.GUFI_SOURCE_ROOT = None
    util.get_gufi_indexes = lambda: sorted(INDEXES)
    util.has_treesummary = lambda index: INDEXES.get(index, False)

    corpus = [{"query": query, **verdict(query)} for query in collect()]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(corpus, indent=1) + "\n")
    accepted = sum(item["ok"] for item in corpus)
    print(f"{len(corpus)} queries ({accepted} accepted, {len(corpus) - accepted} rejected) -> {args.out}")


if __name__ == "__main__":
    main()
