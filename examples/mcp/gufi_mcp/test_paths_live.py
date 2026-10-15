"""Path columns against a real GUFI install: they must match gufi_find and survive rollup."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("SCHEMAFILE", str(ROOT / "schema_hints.json"))
os.environ.setdefault("GUFIVTLIB", "gufi_vt")
os.environ.setdefault("GUFI_INDEX_ROOT", str(ROOT))

import gufi_mcp_util as util  # noqa: E402

ENV = dotenv_values(ROOT / ".env")
INDEX_ROOT = Path(ENV.get("GUFI_INDEX_ROOT") or "/nonexistent").resolve()
INDEX = "eval_master"
SUBDIR = f"{INDEX}/groups/grp03"

pytestmark = pytest.mark.skipif(
    not (INDEX_ROOT / INDEX / "db.db").is_file() or not shutil.which("gufi_find") or not shutil.which("gufi_rollup"),
    reason="needs GUFI on PATH and the eval_master index",
)


@pytest.fixture(autouse=True)
def _real_gufi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "GUFI_INDEX_ROOT", INDEX_ROOT)
    monkeypatch.setattr(util, "GUFIVTLIB", ENV.get("GUFIVTLIB") or util.GUFIVTLIB)
    monkeypatch.setattr(util, "GUFI_SOURCE_ROOT", None)
    monkeypatch.setattr(util, "MAX_ROWS", 10**7)
    monkeypatch.setattr(util, "_GUFI_VT_COLUMN_MAPS", {})


def _aggregate(table: str, columns: list[str], where: str = "") -> set[tuple]:
    shape = ", ".join(f"c{i} TEXT" for i in range(len(columns)))
    names = ", ".join(f"c{i}" for i in range(len(columns)))
    query = util.GufiAggregateQuery.model_validate({
        "index": INDEX,
        "scatter_sql": [f"SELECT {', '.join(columns)} FROM {table} {where}"],
        "scatter_shape": shape,
        "gather_sql": f"SELECT {names} FROM intermediate",
        "gather_shape": shape,
        "final_sql": f"SELECT {names} FROM aggregate",
    })
    return {tuple(row) for row in util.run_aggregate(query).rows}


def _file_index(table: str, columns: list[str], where: str = "") -> set[tuple]:
    sql, params = util.build_gufi_vt_select(f"SELECT {', '.join(columns)} FROM {table} {where}", INDEX)
    return {tuple(row) for row in util.run_gufi_vt_select(sql, params).rows}


def _gufi_find(*args: str) -> set[tuple]:
    out = subprocess.run(["gufi_find", SUBDIR, *args], capture_output=True, text=True, check=True, cwd="/").stdout
    return {(line,) for line in out.splitlines() if line}


def test_file_paths_match_gufi_find() -> None:
    where = f"WHERE type = 'f' AND path LIKE '{SUBDIR}/%'"
    expected = _gufi_find("-type", "f")
    assert expected
    assert _aggregate("vrpentries", ["path"], where) == expected
    assert _file_index("vrpentries", ["path"], where) == expected


def test_directory_paths_match_gufi_find() -> None:
    where = f"WHERE path = '{SUBDIR}' OR path LIKE '{SUBDIR}/%'"
    expected = _gufi_find("-type", "d")
    assert _aggregate("vrsummary", ["path"], where) == expected
    assert _file_index("vrsummary", ["path"], where) == expected


def test_dirpath_is_the_parent_of_path() -> None:
    rows = _file_index("vrpentries", ["path", "dirpath"], f"WHERE dirpath LIKE '{SUBDIR}%'")
    assert rows and all(path.rsplit("/", 1)[0] == dirpath for path, dirpath in rows)


def test_paths_survive_rollup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def everything() -> dict[str, set[tuple]]:
        return {
            "files": _aggregate("vrpentries", ["path", "dirpath"]),
            "files_vt": _file_index("vrpentries", ["path", "dirpath"]),
            "dirs": _aggregate("vrsummary", ["path"]),
        }

    before = everything()
    shutil.copytree(INDEX_ROOT / INDEX, tmp_path / INDEX, symlinks=True)
    subprocess.run(["gufi_rollup", str(tmp_path / INDEX)], capture_output=True, check=True)
    rolled = subprocess.run(["gufi_query", "-d", "|", "-S", "SELECT COUNT(*) FROM vrsummary WHERE isrolledup = 1",
                             str(tmp_path / INDEX)], capture_output=True, text=True, check=True).stdout
    assert sum(int(n) for n in rolled.split()) > 0, "gufi_rollup rolled nothing up; the test would prove nothing"

    monkeypatch.setattr(util, "GUFI_INDEX_ROOT", tmp_path)
    assert everything() == before
    assert before["files"] == before["files_vt"]


def test_cli_tools_print_index_relative_paths() -> None:
    for cmd in (["gufi_stat", SUBDIR], ["gufi_du", "--summarize", SUBDIR]):
        text = "\n".join("\t".join(row) for row in util.run_gufi_tool(cmd, "\t").rows)
        assert SUBDIR in text and str(INDEX_ROOT) not in text, cmd
