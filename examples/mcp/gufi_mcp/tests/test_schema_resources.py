"""Module 2: static schema/resource layer tests."""

from __future__ import annotations

import json
import unittest

from gufi_mcp.schema_resources import (
    GLOBAL_COLUMN_NOTES,
    QUERY_FLAGS_RESOURCE,
    ROLLUP_SEMANTICS_RESOURCE,
    STATIC_RESOURCES,
    UDFS_RESOURCE,
    column_note,
    enrich_table_schema,
    get_static_resource,
)
from gufi_util import parse_schema_registry, resolve_view_types

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[5]
SCHEMA_PATH = REPO_ROOT / "gufi/examples/mcp/gufi_mcp/schemas.json"
SCHEMA = resolve_view_types(parse_schema_registry(str(SCHEMA_PATH)))


class TestStaticResources(unittest.TestCase):
    def test_all_static_resources_are_json_serializable(self) -> None:
        for resource_id, payload in STATIC_RESOURCES.items():
            with self.subTest(resource_id=resource_id):
                text = json.dumps(payload)
                self.assertIn("description", json.loads(text))

    def test_get_static_resource_by_id(self) -> None:
        self.assertIs(get_static_resource("udfs"), UDFS_RESOURCE)
        self.assertIs(get_static_resource("query-flags"), QUERY_FLAGS_RESOURCE)
        self.assertIs(get_static_resource("rollup-semantics"), ROLLUP_SEMANTICS_RESOURCE)
        self.assertIsNone(get_static_resource("missing"))

    def test_query_flags_has_pipeline_stages(self) -> None:
        stages = QUERY_FLAGS_RESOURCE["pipeline_stages"]
        for key in ("init", "entries_sql", "aggregate_create", "final_select"):
            self.assertIn(key, stages)
            self.assertIn("flag", stages[key])

    def test_udfs_documents_path_aware_functions(self) -> None:
        path_aware = UDFS_RESOURCE["path_aware"]
        self.assertIn("listing_path_rules", UDFS_RESOURCE)
        self.assertIn("rpath(sname, sroll [, name])", path_aware)
        self.assertIn("listing_alternative", path_aware["rpath(sname, sroll [, name])"])
        self.assertIn("uidtouser(uid)", UDFS_RESOURCE["identity"])

    def test_rollup_semantics_covers_key_columns(self) -> None:
        keys = ROLLUP_SEMANTICS_RESOURCE["key_columns"]
        self.assertIn("isrolledup", keys)
        self.assertIn("sroll", keys)


class TestColumnNotes(unittest.TestCase):
    def test_global_rectype_note(self) -> None:
        note = column_note("summary", "rectype")
        self.assertIsNotNone(note)
        self.assertIn("0", note)
        self.assertIn("user", note)

    def test_totltk_bucket_notes(self) -> None:
        self.assertIn("1024", GLOBAL_COLUMN_NOTES["totltk"])
        self.assertIn("1024", GLOBAL_COLUMN_NOTES["totmtk"])

    def test_enrich_table_schema_adds_notes(self) -> None:
        enriched = enrich_table_schema("summary", SCHEMA["summary"])
        rectype = next(c for c in enriched if c["name"] == "rectype")
        self.assertIn("note", rectype)
        totltk = next(c for c in enriched if c["name"] == "totltk")
        self.assertIn("note", totltk)

    def test_enrich_vrpentries_sroll_note(self) -> None:
        enriched = enrich_table_schema("vrpentries", SCHEMA["vrpentries"])
        sroll = next(c for c in enriched if c["name"] == "sroll")
        self.assertIn("rpath", sroll["note"])


if __name__ == "__main__":
    unittest.main()
