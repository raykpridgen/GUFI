"""Remote execution: gufi_vt with remote_cmd/remote_arg must give the same answers as running locally.

The offline tests need nothing installed. The live tests compare a local run with
ssh to GUFI_TEST_SSH_HOST (default 127.0.0.1), which must accept key-based logins
and have gufi_query on its non-interactive PATH.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path, PurePosixPath

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
SSH_HOST = os.getenv("GUFI_TEST_SSH_HOST", "127.0.0.1")


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch: pytest.MonkeyPatch):
    util.clear_caches()
    yield
    util.clear_caches()


# Configuration

def _remote_env(monkeypatch: pytest.MonkeyPatch, command: str, args: str) -> tuple:
    monkeypatch.setenv("GUFI_REMOTE_CMD", command)
    monkeypatch.setenv("GUFI_REMOTE_ARGS", args)
    return util._optional_remote()


def test_no_remote_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _remote_env(monkeypatch, "", "") == (None, [])


def test_ssh_gets_no_stdin_and_no_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    command, args = _remote_env(monkeypatch, "ssh", "-p 2222 'user@host'")
    assert command == "ssh"
    assert args == ["-n", "-o", "BatchMode=yes", "-p", "2222", "user@host"]


def test_ssh_options_already_given_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    _, args = _remote_env(monkeypatch, "/usr/bin/ssh", "-n -o BatchMode=no host")
    assert args == ["-n", "-o", "BatchMode=no", "host"]


def test_other_remote_commands_are_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _remote_env(monkeypatch, "pdsh", "-w node1") == ("pdsh", ["-w", "node1"])


def test_remote_args_need_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="GUFI_REMOTE_CMD is not"):
        _remote_env(monkeypatch, "", "host")


def test_remote_args_cannot_hold_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="quote"):
        _remote_env(monkeypatch, "ssh", '"a b"x\\"')


@pytest.mark.parametrize("value", ["relative/path", "/has space", "/semi;colon", "/dollar$x"])
def test_remote_index_root_must_be_shell_safe(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("GUFI_INDEX_ROOT", value)
    with pytest.raises(RuntimeError, match="GUFI_INDEX_ROOT"):
        util._index_root(remote=True)


def test_remote_index_root_is_not_resolved_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUFI_INDEX_ROOT", "/nonexistent/on/this/host")
    assert util._index_root(remote=True) == PurePosixPath("/nonexistent/on/this/host")


# gufi_vt statements

@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(util, "REMOTE_CMD", "ssh")
    monkeypatch.setattr(util, "REMOTE_ARGS", ["-n", "-o", "BatchMode=yes", "user@host"])


def test_remote_options_follow_the_index(remote) -> None:
    options = util.gufi_vt_options("/idx/a", threads=1, max_level=0)
    assert options == ['index="/idx/a"', "threads=1", "max_level=0", 'remote_cmd="ssh"',
                       'remote_arg="-n"', 'remote_arg="-o"', 'remote_arg="BatchMode=yes"', 'remote_arg="user@host"']


def test_local_options_have_no_remote() -> None:
    assert not any(option.startswith("remote") for option in util.gufi_vt_options("/idx/a"))


def _aggregate_payload(sql: str) -> dict:
    return {
        "index": "testidx", "per_directory_shape": "size INT64", "per_directory_sql": [sql],
        "partial_shape": "total INT64", "partial_sql": "SELECT SUM(size) FROM per_directory",
        "final_sql": "SELECT SUM(total) AS total FROM partial",
    }


@pytest.fixture
def fake_index(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(util, "GUFI_INDEX_ROOT", PurePosixPath("/remote/indexes"))
    monkeypatch.setattr(util, "get_gufi_indexes", lambda: ["testidx"])


def test_aggregates_run_on_the_remote_host(remote, fake_index) -> None:
    query = util.GufiAggregateQuery.model_validate(_aggregate_payload("SELECT size FROM vrpentries WHERE type = 'f'"))
    sql = query.build_query_string()
    assert 'index="/remote/indexes/testidx"' in sql
    assert 'remote_cmd="ssh", remote_arg="-n"' in sql and 'remote_arg="user@host"' in sql


@pytest.mark.parametrize("sql", [
    "SELECT size FROM vrpentries WHERE name = '$HOME'",
    "SELECT size FROM vrpentries WHERE name = '`id`'",
    "SELECT size FROM vrpentries WHERE name LIKE 'a\\_b' ESCAPE '\\'",
])
def test_remote_shell_characters_are_rejected(remote, fake_index, sql: str) -> None:
    with pytest.raises(ValueError, match="cannot be sent to the remote GUFI host"):
        util.GufiAggregateQuery.model_validate(_aggregate_payload(sql))


def test_shell_characters_are_fine_locally(fake_index) -> None:
    util.GufiAggregateQuery.model_validate(_aggregate_payload("SELECT size FROM vrpentries WHERE name = '$HOME'"))


def test_remote_index_names_must_be_shell_safe(remote, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [["/remote/indexes/good"], ["/remote/indexes/has space"], ["/remote/indexes/semi;colon"]]
    monkeypatch.setattr(util, "_introspect", lambda *args: rows)
    assert util._index_names("/remote/indexes") == ("good",)


# Cache

def test_cache_reuses_results_until_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    @util._ttl_cache
    def probe(root: str) -> str:
        calls.append(root)
        return root.upper()

    monkeypatch.setattr(util, "CACHE_TTL", 60)
    assert probe("a") == probe("a") == "A"
    assert probe("b") == "B"
    assert calls == ["a", "b"]
    monkeypatch.setattr(util, "CACHE_TTL", 0)
    probe("a")
    assert calls == ["a", "b", "a"]


def test_cache_does_not_keep_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "CACHE_TTL", 60)
    outcomes = [util.GufiError("ssh failed"), "ok"]

    @util._ttl_cache
    def flaky() -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with pytest.raises(util.GufiError):
        flaky()
    assert flaky() == "ok"


def test_treesummary_checks_are_cached_per_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "CACHE_TTL", 60)
    monkeypatch.setattr(util, "get_gufi_indexes", lambda: ["a", "b"])
    monkeypatch.setattr(util, "GUFI_INDEX_ROOT", PurePosixPath("/idx"))
    seen = []
    monkeypatch.setattr(util, "_introspect", lambda root, *args: seen.append(root) or [[root.endswith("a")]])
    assert [util.has_treesummary(i) for i in ("a", "b", "a", "b")] == [True, False, True, False]
    assert seen == ["/idx/a", "/idx/b"]


# Live: local and ssh to SSH_HOST must agree

def _ssh_works() -> bool:
    if not shutil.which("ssh"):
        return False
    try:
        return subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", SSH_HOST,
                               "command -v gufi_query"], capture_output=True, timeout=15).returncode == 0
    except subprocess.TimeoutExpired:
        return False


live = pytest.mark.skipif(
    not (INDEX_ROOT / INDEX / "db.db").is_file() or not ENV.get("GUFIVTLIB") or not _ssh_works(),
    reason=f"needs GUFIVTLIB in .env, the {INDEX} index, and key-based ssh to {SSH_HOST} with gufi_query on PATH",
)


@pytest.fixture
def real_gufi(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(util, "GUFI_INDEX_ROOT", INDEX_ROOT)
    monkeypatch.setattr(util, "GUFIVTLIB", ENV["GUFIVTLIB"])
    monkeypatch.setattr(util, "GUFI_SOURCE_ROOT", None)

    def use(remote: bool) -> None:
        util.clear_caches()
        monkeypatch.setattr(util, "REMOTE_CMD", "ssh" if remote else None)
        monkeypatch.setattr(util, "REMOTE_ARGS", ["-n", "-o", "BatchMode=yes", SSH_HOST] if remote else [])

    return use


def _snapshot() -> dict:
    indexes = util.get_gufi_indexes()
    query = util.GufiAggregateQuery.model_validate({
        "index": INDEX,
        "per_directory_shape": "uid INT64, size INT64",
        "per_directory_sql": ["SELECT uid, size FROM vrpentries WHERE type = 'f'"],
        "partial_shape": "uid INT64, files INT64, bytes INT64",
        "partial_sql": "SELECT uid, COUNT(*), SUM(size) FROM per_directory GROUP BY uid",
        "final_sql": "SELECT uid, SUM(files) AS files, SUM(bytes) AS bytes FROM partial GROUP BY uid ORDER BY uid",
    })
    return {
        "indexes": {index: util.has_treesummary(index) for index in indexes},
        "objects": util.list_schema_objects(),
        "vrpentries": util.describe_schema_object("vrpentries"),
        "aggregate": util.run_aggregate(query).rows,
    }


@live
def test_remote_matches_local(real_gufi) -> None:
    real_gufi(remote=False)
    local = _snapshot()
    real_gufi(remote=True)
    assert _snapshot() == local
    assert INDEX in local["indexes"] and local["aggregate"]


@live
def test_index_metadata_matches_the_index_files(real_gufi) -> None:
    real_gufi(remote=True)
    on_disk = sorted(entry.name for entry in INDEX_ROOT.iterdir() if (entry / "db.db").is_file())
    assert util.get_gufi_indexes() == on_disk
    for index in on_disk:
        with sqlite3.connect(f"{(INDEX_ROOT / index / 'db.db').as_uri()}?mode=ro", uri=True) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert util.has_treesummary(index) == ("treesummary" in tables), index


@live
def test_gufi_query_tools_reach_the_remote_host(real_gufi) -> None:
    real_gufi(remote=True)
    assert util.gufi_query_path().startswith("/")
    assert util.gufi_query_version()


@live
def test_failed_connection_is_an_error_naming_the_host(real_gufi, monkeypatch: pytest.MonkeyPatch) -> None:
    real_gufi(remote=True)
    monkeypatch.setattr(util, "REMOTE_ARGS", ["-n", "-o", "BatchMode=yes", "-p", "1", "127.0.0.1"])
    with pytest.raises(util.GufiError, match="on the remote host .*-p 1 127.0.0.1"):
        util.get_gufi_indexes()


@live
def test_remote_timeout_kills_ssh(real_gufi, monkeypatch: pytest.MonkeyPatch) -> None:
    real_gufi(remote=True)
    util.get_gufi_indexes()
    monkeypatch.setattr(util, "TOOL_TIMEOUT", 0.05)
    query = util.GufiAggregateQuery.model_validate({
        "index": INDEX, "per_directory_shape": "size INT64",
        "per_directory_sql": ["SELECT size FROM vrpentries"],
        "partial_shape": "total INT64", "partial_sql": "SELECT SUM(size) FROM per_directory",
        "final_sql": "SELECT SUM(total) AS total FROM partial",
    })
    with pytest.raises(util.GufiError, match="did not finish"):
        util.run_aggregate(query)
    time.sleep(0.2)
    assert util._gufi_query_children() == []


@live
def test_cache_avoids_repeated_round_trips(real_gufi, monkeypatch: pytest.MonkeyPatch) -> None:
    real_gufi(remote=True)
    monkeypatch.setattr(util, "CACHE_TTL", 60)
    start = time.monotonic()
    util.list_schema_objects()
    first = time.monotonic() - start
    start = time.monotonic()
    for _ in range(10):
        util.list_schema_objects()
    assert time.monotonic() - start < first
