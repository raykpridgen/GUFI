"""Offline tests for gen_index.py plan building and configuration (no Docker required)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import gen_index as gen

FIXED_NOW = 1790812800  # 2026-10-01T00:00:00Z


def _config(**overrides: object) -> gen.GenConfig:
    base = {
        "groups": 3, "users": 7,
        "files_per_user_min": 30, "files_per_user_max": 60,
        "dirs_per_user_min": 4, "dirs_per_user_max": 9,
        "dir_depth_min": 2, "dir_depth_max": 4,
        "symlink_fraction": 0.1,
    }
    base.update(overrides)
    return gen.GenConfig.model_validate(base)


def _plan(seed: int = 5, **overrides: object) -> dict:
    return gen.build_plan(_config(**overrides), seed, FIXED_NOW)


def _of(plan: dict, kind: str) -> list[dict]:
    return [e for e in plan["entries"] if e["kind"] == kind]


# Determinism

def test_same_seed_same_plan() -> None:
    assert _plan(5) == _plan(5)


def test_different_seed_different_plan() -> None:
    assert _plan(5)["plan_id"] != _plan(6)["plan_id"]


# Layout and identities

def test_parents_precede_children_and_paths_unique() -> None:
    plan = _plan()
    seen_dirs = set()
    paths = [e["path"] for e in plan["entries"]]
    assert len(paths) == len(set(paths))
    for entry in plan["entries"]:
        if entry["path"]:
            parent = entry["path"].rsplit("/", 1)[0] if "/" in entry["path"] else ""
            assert parent in seen_dirs, entry["path"]
        if entry["kind"] == "d":
            seen_dirs.add(entry["path"])


def test_users_spread_over_groups() -> None:
    groups, users = gen.build_identities(_config(), gen.random.Random(1))
    assert [g["gid"] for g in groups] == [2001, 2002, 2003]
    assert sorted(u["uid"] for u in users) == list(range(3001, 3008))
    sizes = Counter(u["gid"] for u in users)
    assert max(sizes.values()) - min(sizes.values()) <= 1
    assert sum(len(g["members"]) for g in groups) == 7


def test_explicit_names_are_used() -> None:
    plan = _plan(groups=["physics", "chem"], users=["ada", "bob", "cy"])
    assert {g["name"] for g in plan["groups"]} == {"physics", "chem"}
    assert {u["name"] for u in plan["users"]} == {"ada", "bob", "cy"}


def test_user_entries_live_in_their_group() -> None:
    plan = _plan()
    users = {u["uid"]: u for u in plan["users"]}
    for entry in plan["entries"]:
        if entry["uid"] in users:
            user = users[entry["uid"]]
            assert entry["gid"] == user["gid"]
            assert entry["path"].startswith(f"groups/{user['group']}/")


def test_group_and_home_permissions() -> None:
    plan = _plan()
    by_path = {e["path"]: e for e in plan["entries"]}
    for group in plan["groups"]:
        assert by_path[f"groups/{group['name']}"]["mode"] == 0o2770
        assert by_path[f"groups/{group['name']}"]["gid"] == group["gid"]
    for user in plan["users"]:
        home = by_path[f"groups/{user['group']}/{user['name']}"]
        assert (home["uid"], home["gid"], home["mode"]) == (user["uid"], user["gid"], 0o750)


# Density and bounds

def test_counts_within_bounds() -> None:
    cfg = _config()
    plan = gen.build_plan(cfg, 5, FIXED_NOW)
    files = Counter(e["uid"] for e in _of(plan, "f"))
    for user in plan["users"]:
        assert cfg.files_per_user_min <= files[user["uid"]] <= cfg.files_per_user_max
        home = f"groups/{user['group']}/{user['name']}"
        dirs = [e for e in _of(plan, "d") if e["path"].startswith(home + "/")]
        assert cfg.dirs_per_user_min <= len(dirs) <= cfg.dirs_per_user_max
        depths = [d["path"][len(home) + 1:].count("/") + 1 for d in dirs]
        assert cfg.dir_depth_min <= max(depths) <= cfg.dir_depth_max


def test_sizes_and_extensions() -> None:
    cfg = _config(file_size_min=10, file_size_max=10_000, empty_file_fraction=0.2, large_files=0)
    plan = gen.build_plan(cfg, 5, FIXED_NOW)
    for entry in _of(plan, "f"):
        assert entry["size"] == 0 or 10 <= entry["size"] <= 10_000
        assert entry["ext"] is None or entry["ext"] in cfg.extension_vocab
        if entry["ext"]:
            assert entry["path"].endswith("." + entry["ext"])
    assert any(e["size"] == 0 for e in _of(plan, "f"))


@pytest.mark.parametrize("text, expected", [
    ("4096", 4096), ("10g", 10 * 1024**3), ("10GiB", 10 * 1024**3), ("512MB", 512 * 1024**2),
    ("1.5G", 1536 * 1024**2), ("2 k", 2048), (77, 77),
])
def test_parse_size(text: object, expected: int) -> None:
    assert gen.parse_size(text) == expected


def test_bad_size_rejected() -> None:
    with pytest.raises(gen.ValidationError, match="not a size"):
        _config(disk_limit="lots")


def test_large_files_drawn_from_large_range() -> None:
    cfg = _config(large_files=3, large_file_size_min="1MiB", large_file_size_max="2MiB", file_size_max=1000)
    plan = gen.build_plan(cfg, 5, FIXED_NOW)
    large = [e for e in _of(plan, "f") if e["size"] > 1000]
    assert len(large) == 3
    assert all(1024**2 <= e["size"] <= 2 * 1024**2 for e in large)
    assert plan["size_scale"] == 1.0


def test_disk_limit_scales_sizes_to_fit() -> None:
    unlimited = _plan(large_files=4, large_file_size_min="64MiB", large_file_size_max="128MiB")
    limit = 32 * 1024**2
    plan = _plan(large_files=4, large_file_size_min="64MiB", large_file_size_max="128MiB", disk_limit=limit)
    assert plan["allocated_bytes"] == gen.tree_allocation(plan["entries"]) <= limit
    assert 0 < plan["size_scale"] < 1
    assert [e["path"] for e in _of(plan, "f")] == [e["path"] for e in _of(unlimited, "f")]
    biggest = max(_of(unlimited, "f"), key=lambda e: e["size"])["path"]
    assert max(_of(plan, "f"), key=lambda e: e["size"])["path"] == biggest


def test_disk_limit_too_small_for_directories() -> None:
    with pytest.raises(gen.GenError, match="too small"):
        _plan(disk_limit=4096)


def test_default_plan_fits_default_limit() -> None:
    cfg = gen.GenConfig()
    plan = gen.build_plan(cfg, 1, FIXED_NOW)
    assert plan["allocated_bytes"] <= cfg.disk_limit == 10 * 1024**3


def test_apply_allocates_real_blocks(tmp_path: Path) -> None:
    import gen_index_apply as apply
    path = tmp_path / "f"
    with open(path, "xb") as f:
        apply.allocate(f, 1_000_000)
    st = path.stat()
    assert st.st_size == 1_000_000
    assert st.st_blocks * 512 >= 1_000_000
    assert apply.disk_used(tmp_path) >= 1_000_000


def test_apply_rejects_plan_over_limit(tmp_path: Path) -> None:
    import gen_index_apply as apply
    with pytest.raises(apply.DiskLimitError, match="disk_limit"):
        apply.check_free_space(tmp_path, 2 * 1024**3, 1024**3)
    with pytest.raises(apply.DiskLimitError, match="free"):
        apply.check_free_space(tmp_path, 1024**5, 1024**6)


def test_times_relative_to_fixed_now() -> None:
    cfg = _config(mtime_range_days=(10, 100), atime_range_days=(0, 50))
    plan = gen.build_plan(cfg, 5, FIXED_NOW)
    for entry in plan["entries"]:
        assert FIXED_NOW - 100 * 86400 <= entry["mtime"] <= FIXED_NOW - 10 * 86400
        assert entry["mtime"] <= entry["atime"] <= FIXED_NOW


def test_symlinks_point_inside_or_dangle() -> None:
    plan = _plan(symlink_fraction=0.2, dangling_symlink_fraction=0.5)
    links = _of(plan, "l")
    files = {e["path"] for e in _of(plan, "f")}
    assert links
    dangling = 0
    for link in links:
        assert link["size"] == len(link["target"].encode())
        if link["target"].startswith("missing_"):
            dangling += 1
        else:
            parent = link["path"].rsplit("/", 1)[0]
            resolved = gen.os.path.normpath(f"{parent}/{link['target']}")
            assert resolved in files
    assert 0 < dangling < len(links)


def test_expected_counts_match_plan_totals() -> None:
    plan = _plan()
    rows = gen.expected_counts(plan)
    assert sum(r[3] for r in rows) == len(plan["entries"])
    assert sum(r[4] for r in rows if r[0] == "f") == gen.plan_stats(plan)["total_file_size"]


# Configuration validation

@pytest.mark.parametrize("overrides, message", [
    ({"files_per_user_min": 10, "files_per_user_max": 5}, "files_per_user_min"),
    ({"dir_depth_min": 4, "dirs_per_user_min": 2, "dirs_per_user_max": 9}, "dirs_per_user_min must be at least"),
    ({"index_name": "../escape"}, "index_name"),
    ({"index_name": "a/b"}, "index_name"),
    ({"groups": 0}, "groups must be at least 1"),
    ({"users": ["ok", "Bad Name"]}, "invalid users"),
    ({"users": ["a", "a"]}, "unique"),
    ({"extension_vocab": {".txt": 1}}, "letters and digits"),
    ({"extension_vocab": {"txt": 0}}, "positive weight"),
    ({"mtime_range_days": (5, 1)}, "mtime_range_days"),
    ({"index_ownership": "root"}, "index_ownership"),
    ({"unknown": 1}, "Extra inputs"),
])
def test_bad_config_rejected(overrides: dict, message: str) -> None:
    with pytest.raises(gen.ValidationError, match=message):
        _config(**overrides)


def test_load_config_applies_overrides(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("index_name: from_file\nseed: 1\ngroups: [a, b]\n")
    cfg = gen.load_config(path, {"seed": 9, "index_name": None, "fixed_now": "2026-10-01"})
    assert (cfg.index_name, cfg.seed, cfg.groups) == ("from_file", 9, ["a", "b"])
    assert cfg.resolved_fixed_now() == FIXED_NOW


def test_load_config_reports_errors(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("- not a mapping\n")
    with pytest.raises(gen.GenError, match="mapping"):
        gen.load_config(path, {})


# Container invocation

def test_docker_command_mounts(tmp_path: Path) -> None:
    cmd = gen.docker_command(_config(), Path("/opt/gufi"), Path("/idx"), tmp_path)
    assert cmd[:4] == ["docker", "run", "--rm", "--network"]
    assert "/opt/gufi:/opt/gufi:ro" in cmd
    assert "/idx:/out" in cmd
    assert f"{tmp_path}:/gen:ro" in cmd
    assert cmd[cmd.index("--gufi-bin") + 1] == "/opt/gufi/bin"


def test_dry_run_needs_no_docker(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen, "check_docker", lambda: pytest.fail("dry run must not touch docker"))
    assert gen.main(["--seed", "3", "--fixed-now", "2026-10-01", "--dry-run"]) == 0
    assert '"seed": 3' in capsys.readouterr().out
