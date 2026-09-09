"""
Module 2: static MCP schema/resource payloads (no index I/O).

Serves gufi://udfs, gufi://query-flags, gufi://rollup-semantics, and column-note
enrichment for gufi://schemas/{table}. Column notes are sourced verbatim from
refs.md GUFI doc excerpts — no invented facts.
"""

from __future__ import annotations

from typing import Any

from gufi_util import SchemaRegistry

# --- Column notes (refs.md § Table Schema Expected Values) ---

GLOBAL_COLUMN_NOTES: dict[str, str] = {
    "type": "TEXT: d for directory; in entries also used for file or link character",
    "rectype": (
        "INT64: 0 = total for entire dir/tree; "
        "1 = totals by user; 2 = totals by group"
    ),
    "totltk": "INT64: count of files with size <= 1024",
    "totmtk": "INT64: count of files with size > 1024",
    "totltm": "INT64: count of files with size <= 1048576",
    "totmtm": "INT64: count of files with size > 1048576",
    "totmtg": "INT64: count of files with size <= 1073741824",
    "totmtt": "INT64: count of files with size > 1073741824",
}

TABLE_COLUMN_NOTES: dict[str, dict[str, str]] = {
    "vrsummary": {
        "sroll": (
            "Alias of summary.isrolledup — rollup score; "
            "listing: use gufi_vt_vrsummary.rpath column; "
            "aggregate -S: rpath(sname, sroll) OK inside pipeline"
        ),
        "srollsubdirs": "Count of subdirectories under this directory (rollup-aware)",
    },
    "vrpentries": {
        "sroll": (
            "Rollup score from parent vrsummary row; "
            "listing: use (rpath || '/' || name) via gufi_vt_vrpentries columns; "
            "aggregate -E: rpath(sname, sroll, name) OK inside pipeline"
        ),
        "dname": "Directory-relative name prefix derived from vrsummary.name",
        "sname": "Full rolled-up directory path (summary.name)",
    },
    "summary": {
        "isrolledup": "Rollup score for this directory row (0 = not rolled up, 1 = rolled up)",
        "canrollup": "Whether this directory is eligible for rollup per permission rules",
    },
    "treesummary": {
        "rectype": (
            "INT64: 0 = total for entire tree; "
            "1 = totals by user; 2 = totals by group"
        ),
    },
}

QUERY_FLAGS_RESOURCE: dict[str, Any] = {
    "description": "GUFI query pipeline flags mapped to QueryPlan pipeline stages.",
    "pipeline_stages": {
        "init": {
            "flag": "-I",
            "stage": "init",
            "semantics": "SQL init — CREATE intermediate table(s) before scan stages run.",
        },
        "tree_sql": {
            "flag": "-T",
            "stage": "tree_sql",
            "semantics": "SQL against treesummary — prune or aggregate subtrees before -S/-E.",
        },
        "summary_sql": {
            "flag": "-S",
            "stage": "summary_sql",
            "semantics": (
                "SQL against directory summary (prefer vrsummary). Runs before -E; "
                "empty summary output skips entries entirely."
            ),
        },
        "entries_sql": {
            "flag": "-E",
            "stage": "entries_sql",
            "semantics": (
                "SQL against file/symlink rows (prefer vrpentries). "
                "Listing SELECT or INSERT INTO intermediate."
            ),
        },
        "aggregate_create": {
            "flag": "-K",
            "stage": "aggregate_create",
            "semantics": "CREATE final aggregation table — requires -I.",
        },
        "aggregate_insert": {
            "flag": "-J",
            "stage": "aggregate_insert",
            "semantics": (
                "INSERT INTO aggregate table FROM intermediate — requires -I and -K."
            ),
        },
        "final_select": {
            "flag": "-G",
            "stage": "final_select",
            "semantics": "SELECT from aggregate table for final result rows.",
        },
    },
    "ordering": [
        "init (-I) before scan/aggregate stages",
        "tree_sql (-T) optional prune before summary/entries",
        "summary_sql (-S) before entries_sql (-E) when both present",
        "aggregate_create (-K) and aggregate_insert (-J) after init, before final_select (-G)",
    ],
    "scope_flags": {
        "min_level": {"flag": "-y", "field": "scope.min_level", "semantics": "Minimum tree depth to descend."},
        "max_level": {"flag": "-z", "field": "scope.max_level", "semantics": "Maximum tree depth to descend."},
        "threads": {"flag": "-n", "field": "execution.threads", "semantics": "Parallel worker threads (default 1)."},
    },
}

UDFS_RESOURCE: dict[str, Any] = {
    "description": "GUFI/SQLite user-defined functions relevant to path-aware queries.",
    "listing_path_rules": {
        "summary": (
            "execute_query_plan listing plans compile to SELECT … FROM gufi_vt_*(…). "
            "Context UDFs (rpath, path, level, …) are NOT on the outer SQLite connection."
        ),
        "listing_vrpentries_file_path": (
            "SELECT (rpath || '/' || name) AS path, size, … FROM vrpentries … "
            "→ compiles to gufi_vt_vrpentries; rpath/name are VT columns."
        ),
        "listing_vrsummary_dir_path": (
            "SELECT rpath, … FROM vrsummary … → compiles to gufi_vt_vrsummary; "
            "rpath is a VT column (directory path)."
        ),
        "aggregate_pipeline": (
            "When init + aggregate_create + aggregate_insert are set, -E/-S SQL runs "
            "inside gufi_query and rpath(sname, sroll[, name]) works normally."
        ),
        "auto_rewrite": (
            "The compiler rewrites rpath(sname,sroll,name) → (rpath || '/' || name) and "
            "rpath(sname,sroll) → rpath for listing plans; validate warns when rewrite applies."
        ),
    },
    "path_aware": {
        "rpath(sname, sroll [, name])": {
            "availability": "aggregate pipeline (-I/-E/-K/-J) inside gufi_query; NOT listing outer SQL",
            "listing_alternative": "(rpath || '/' || name) or rpath column via gufi_vt_vrpentries / gufi_vt_vrsummary",
            "use_with": "vrpentries, vrsummary (sname and sroll columns)",
            "semantics": (
                "Reconstruct path relative to the query starting directory, accounting "
                "for rollup. Aggregate -E: rpath(sname, sroll, name). "
                "Listing: use VT rpath column instead."
            ),
        },
        "spath(sname, sroll [, name])": {
            "availability": "gufi_query / aggregate pipeline only",
            "use_with": "vrpentries, vrsummary",
            "semantics": "Source path of current entry; requires -p source path prefix.",
        },
        "path()": {
            "availability": "aggregate pipeline; listing use path column from gufi_vt_*",
            "semantics": "Current directory relative to path passed into gufi_query.",
        },
        "fpath()": {
            "availability": "aggregate pipeline; listing use fpath column from gufi_vt_*",
            "semantics": "Full path of current directory.",
        },
        "level()": {
            "availability": "aggregate pipeline; listing use level column from gufi_vt_*",
            "semantics": "Depth of the current directory from the starting directory.",
        },
        "subdirs(srollsubdirs, sroll)": "Subdirectory count; vrsummary only, rollup-aware.",
    },
    "identity": {
        "uidtouser(uid)": "Convert UID to user name.",
        "gidtogroup(gid)": "Convert GID to group name.",
    },
    "formatting": {
        "modetotxt(mode)": "Convert permission bits to text.",
        "human_readable_size(bytes)": "Human-readable byte size string.",
        "bytecat(int)": "Size category using 1024-based buckets (B, K, M, G, ...).",
        "intcat(int)": "Size category using 1000-based buckets (O, K, M, B, T, ...).",
    },
}

ROLLUP_SEMANTICS_RESOURCE: dict[str, Any] = {
    "description": "How GUFI index rollup affects query surfaces and path reconstruction.",
    "overview": (
        "Rollup copies child directory metadata into parent databases to reduce "
        "directory opens during queries. Indexes are not rolled up automatically — "
        "run gufirollup on the index root."
    ),
    "key_columns": {
        "isrolledup": "summary column: rollup score (0 = not rolled up, 1 = rolled up).",
        "sroll": "vrsummary/vrpentries alias of isrolledup for path helpers.",
        "canrollup": "Whether a directory meets permission rules to allow rollup.",
        "srollsubdirs": "vrsummary: count of subdirectories (rollup-aware).",
        "pentries_rollup": "INTERNAL storage table for rolled-up child pentries.",
    },
    "query_guidance": [
        "Prefer vrpentries and vrsummary over entries/summary on rolled-up indexes.",
        "Listing (gufi_vt): file path = (rpath || '/' || name); directory path = rpath column.",
        "Aggregate pipeline: rpath(sname, sroll[, name]) inside -E/-S SQL is OK.",
        "Do not use bare name alone for paths on rolled-up indexes.",
        "Respect sroll/isrolledup when reconstructing paths or aggregating by directory.",
        "Queries starting below the top-most rollup level may not benefit from rollup.",
    ],
    "permission_rules_summary": (
        "A child may roll into its parent only when all children are rolled up and "
        "permissions satisfy one of the documented world-readable, matching uid/gid, "
        "or user-only readable patterns (see GUFI rollup docs)."
    ),
}

STATIC_RESOURCES: dict[str, dict[str, Any]] = {
    "udfs": UDFS_RESOURCE,
    "query-flags": QUERY_FLAGS_RESOURCE,
    "rollup-semantics": ROLLUP_SEMANTICS_RESOURCE,
}


def column_note(table: str, column: str) -> str | None:
    """Return a hand-authored note for a column, if one exists."""
    table_notes = TABLE_COLUMN_NOTES.get(table, {})
    if column in table_notes:
        return table_notes[column]
    return GLOBAL_COLUMN_NOTES.get(column)


def enrich_table_schema(
    table: str,
    columns: SchemaRegistry | list[dict[str, str]],
) -> list[dict[str, str]]:
    """Attach optional notes to registry columns for gufi://schemas/{table}."""
    col_list = columns if isinstance(columns, list) else columns.get(table, [])
    enriched: list[dict[str, str]] = []
    for col in col_list:
        entry: dict[str, str] = {"name": col["name"], "type": col["type"]}
        note = column_note(table, col["name"])
        if note:
            entry["note"] = note
        enriched.append(entry)
    return enriched


def get_static_resource(resource_id: str) -> dict[str, Any] | None:
    """Return a Phase-0 static resource payload by id (udfs, query-flags, ...)."""
    return STATIC_RESOURCES.get(resource_id)
