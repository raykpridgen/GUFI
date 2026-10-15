#!/usr/bin/env python3
"""
Build a seeded synthetic HPC-like filesystem tree and index it with GUFI.

Design: eval_design.md Part 1. Every random choice is made here, on the host,
in a deterministic plan (same seed + config + fixed_now -> same plan). A
throwaway Docker container then materializes the plan as root, indexes it with
the bind-mounted GUFI install directly into the export root, and is removed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator, model_validator

from gen_index_apply import verify_counts

HERE = Path(__file__).resolve().parent
APPLY_SCRIPT = HERE / "gen_index_apply.py"

CONTAINER_GEN = "/gen"
CONTAINER_TREE = "/tree"
CONTAINER_OUT = "/out"

FIRST_GID = 2001
FIRST_UID = 3001
PLAN_VERSION = 1
SECONDS_PER_DAY = 86400

GUFI_BINARIES = ("gufi_dir2index", "gufi_query", "gufi_treesummary_all")

WORDS = (
    "analysis", "archive", "audio", "backup", "batch", "bench", "build", "cache", "checkpoint", "chunk",
    "climate", "clean", "cluster", "code", "config", "corpus", "data", "dataset", "draft", "epoch",
    "eval", "field", "figures", "final", "fluid", "genome", "gpu", "grid", "image", "input",
    "job", "lattice", "layer", "lib", "logs", "mesh", "metrics", "model", "neuron", "node",
    "notes", "ocean", "old", "output", "paper", "params", "part", "particle", "phase", "plots",
    "profile", "protein", "raw", "report", "results", "run", "sample", "scratch", "shard", "signal",
    "sim", "slice", "src", "stage", "step", "survey", "sweep", "tests", "text", "tmp",
    "trace", "train", "video", "weights",
)

DEFAULT_EXTENSIONS = {
    "txt": 10, "log": 10, "csv": 8, "dat": 8, "py": 8, "json": 6, "h5": 5, "png": 5, "c": 4, "gz": 4,
}

_INDEX_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ACCOUNT_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_EXTENSION_RE = re.compile(r"[A-Za-z0-9]+")

DIR_MODES = ((0o750, 5), (0o755, 3), (0o700, 1), (0o770, 1))
FILE_MODES = ((0o644, 5), (0o640, 3), (0o600, 1), (0o755, 1))
SHARED_FILE_FRACTION = 0.05

# Disk accounting assumes this allocation unit for files and directories.
ALLOC_BLOCK = 4096

_SIZE_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


class GenError(Exception):
    """A failure to report to the user without a traceback."""


def parse_size(value: Any) -> Any:
    """Accept byte counts as ints or strings like '10g', '512MiB', '1.5G' (binary units)."""
    if isinstance(value, str):
        match = _SIZE_RE.fullmatch(value)
        if not match:
            raise ValueError(f"{value!r} is not a size like 4096, '512MiB', or '10g'")
        return int(float(match.group(1)) * _SIZE_UNITS[match.group(2).lower()])
    return value


ByteSize = Annotated[int, BeforeValidator(parse_size)]


def allocated(size: int) -> int:
    """Bytes a file of this size occupies on disk."""
    return -(-size // ALLOC_BLOCK) * ALLOC_BLOCK


#
# Configuration
#

class GenConfig(BaseModel):
    """Inputs for one synthetic index build (eval_design.md Part 1, Inputs)."""

    model_config = ConfigDict(extra="forbid")

    seed: Optional[int] = Field(default=None, ge=0, description="Random seed; a new one is drawn when omitted.")
    index_name: str = Field(default="eval_master", description="Index directory name under the export root.")
    export_root: Optional[Path] = Field(default=None, description="Where the index is written; defaults to GUFI_INDEX_ROOT.")

    groups: int | list[str] = Field(default=5, description="Number of groups, or explicit group names.")
    users: int | list[str] = Field(default=15, description="Number of users, or explicit user names.")

    files_per_user_min: int = Field(default=200, ge=0)
    files_per_user_max: int = Field(default=800, ge=0)
    dirs_per_user_min: int = Field(default=5, ge=1)
    dirs_per_user_max: int = Field(default=25, ge=1)
    dir_depth_min: int = Field(default=1, ge=1, description="Minimum depth below each home that at least one directory reaches.")
    dir_depth_max: int = Field(default=5, ge=1)

    disk_limit: ByteSize = Field(
        default=10 * 1024**3, ge=ALLOC_BLOCK,
        description="Hard cap on disk the tree may occupy (files are really allocated). Sizes are scaled down to fit.",
    )
    file_size_min: ByteSize = Field(default=1, ge=1, description="Smallest non-empty regular file size.")
    file_size_max: ByteSize = Field(default=4 * 1024**2, ge=1, description="Largest regular file size (log-uniform between min and max).")
    empty_file_fraction: float = Field(default=0.05, ge=0, le=1)
    large_files: int = Field(default=6, ge=0, description="Number of files drawn from the large size range instead.")
    large_file_size_min: ByteSize = Field(default=512 * 1024**2, ge=1)
    large_file_size_max: ByteSize = Field(default=2 * 1024**3, ge=1)

    extension_vocab: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_EXTENSIONS))
    no_extension_fraction: float = Field(default=0.05, ge=0, le=1)
    hidden_fraction: float = Field(default=0.03, ge=0, le=1)

    symlink_fraction: float = Field(default=0.03, ge=0, le=1, description="Symlinks per user, as a fraction of their file count.")
    dangling_symlink_fraction: float = Field(default=0.25, ge=0, le=1)

    mtime_range_days: tuple[int, int] = Field(default=(0, 1095), description="mtime is this many days before fixed_now.")
    atime_range_days: tuple[int, int] = Field(default=(0, 365), description="atime is this many days before fixed_now, never before mtime.")
    fixed_now: Optional[datetime] = Field(default=None, description="Reference time; defaults to today 00:00 UTC.")

    container_image: str = Field(default="python:3.13-slim", min_length=1)
    gufi_prefix: Optional[Path] = Field(default=None, description="GUFI install prefix containing bin/.")
    threads: int = Field(default_factory=lambda: os.cpu_count() or 1, ge=1)
    treesummary: bool = True
    index_ownership: Literal["host", "preserve"] = Field(
        default="host",
        description="host: chown the index to the invoking user. preserve: keep synthetic owners and modes "
                    "so GUFI permission enforcement applies (the host user cannot read it).",
    )

    @field_validator("index_name")
    @classmethod
    def index_name_safe(cls, value: str) -> str:
        if not _INDEX_NAME_RE.fullmatch(value) or value in (".", ".."):
            raise ValueError("index_name must be one path component of letters, digits, '.', '_', '-'")
        return value

    @field_validator("groups", "users")
    @classmethod
    def accounts_valid(cls, value: int | list[str], info) -> int | list[str]:
        if isinstance(value, int):
            if value < 1:
                raise ValueError(f"{info.field_name} must be at least 1")
            return value
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        bad = [name for name in value if not _ACCOUNT_RE.fullmatch(name)]
        if bad:
            raise ValueError(f"invalid {info.field_name} names: {bad}")
        if len(set(value)) != len(value):
            raise ValueError(f"{info.field_name} names must be unique")
        return value

    @field_validator("extension_vocab")
    @classmethod
    def extensions_valid(cls, value: dict[str, float]) -> dict[str, float]:
        if not value:
            raise ValueError("extension_vocab must not be empty")
        for ext, weight in value.items():
            if not _EXTENSION_RE.fullmatch(ext):
                raise ValueError(f"extension {ext!r} must be letters and digits only (no leading dot)")
            if weight <= 0:
                raise ValueError(f"extension {ext!r} must have a positive weight")
        return value

    @field_validator("mtime_range_days", "atime_range_days")
    @classmethod
    def day_range_valid(cls, value: tuple[int, int], info) -> tuple[int, int]:
        low, high = value
        if low < 0 or low > high:
            raise ValueError(f"{info.field_name} must be [min, max] with 0 <= min <= max")
        return value

    @field_validator("fixed_now")
    @classmethod
    def fixed_now_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def ranges_ordered(self) -> "GenConfig":
        for low, high in (("files_per_user_min", "files_per_user_max"), ("dirs_per_user_min", "dirs_per_user_max"),
                          ("dir_depth_min", "dir_depth_max"), ("file_size_min", "file_size_max"),
                          ("large_file_size_min", "large_file_size_max")):
            if getattr(self, low) > getattr(self, high):
                raise ValueError(f"{low} must not exceed {high}")
        if self.dirs_per_user_min < self.dir_depth_min:
            raise ValueError("dirs_per_user_min must be at least dir_depth_min so the minimum depth is reachable")
        return self

    def resolved_fixed_now(self) -> int:
        if self.fixed_now is not None:
            return int(self.fixed_now.timestamp())
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(today.timestamp())


def load_config(path: Optional[Path], overrides: dict[str, Any]) -> GenConfig:
    """Read a YAML or JSON config file (YAML is a superset of JSON) and apply CLI overrides."""
    data: dict[str, Any] = {}
    if path is not None:
        try:
            loaded = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as e:
            raise GenError(f"Could not read config {path}: {e}") from e
        if loaded is not None and not isinstance(loaded, dict):
            raise GenError(f"Config {path} must be a mapping of option names to values")
        data = loaded or {}
    data.update({key: value for key, value in overrides.items() if value is not None})
    try:
        return GenConfig.model_validate(data)
    except ValidationError as e:
        raise GenError(f"Invalid configuration:\n{e}") from e


#
# Plan
#

def _weighted(rng: random.Random, choices: tuple[tuple[Any, float], ...]) -> Any:
    values, weights = zip(*choices)
    return rng.choices(values, weights=weights, k=1)[0]


def _times(rng: random.Random, cfg: GenConfig, fixed_now: int) -> tuple[int, int]:
    """(mtime, atime) as epoch seconds, with atime no earlier than mtime."""
    mtime_age = rng.uniform(*cfg.mtime_range_days) * SECONDS_PER_DAY
    atime_low = cfg.atime_range_days[0] * SECONDS_PER_DAY
    atime_high = min(cfg.atime_range_days[1] * SECONDS_PER_DAY, mtime_age)
    atime_age = rng.uniform(atime_low, atime_high) if atime_low <= atime_high else mtime_age
    return fixed_now - int(mtime_age), fixed_now - int(atime_age)


def _log_uniform(rng: random.Random, low: int, high: int) -> int:
    return int(math.exp(rng.uniform(math.log(low), math.log(high))))


def _file_size(rng: random.Random, cfg: GenConfig) -> int:
    if rng.random() < cfg.empty_file_fraction:
        return 0
    return _log_uniform(rng, cfg.file_size_min, cfg.file_size_max)


def _fit_disk_limit(entries: list[dict], limit: int) -> float:
    """Scale file sizes down uniformly until the tree's allocation fits within limit.

    Returns the scale factor applied (1.0 when the plan already fits).
    """
    files = [e for e in entries if e["kind"] == "f"]
    overhead = sum(ALLOC_BLOCK for e in entries if e["kind"] == "d")
    budget = limit - overhead
    if budget <= 0:
        raise GenError(f"disk_limit {limit} is too small for {overhead // ALLOC_BLOCK} directories alone")
    scale = 1.0
    while (used := sum(allocated(e["size"]) for e in files)) > budget:
        step = budget / used * 0.999
        scale *= step
        for entry in files:
            entry["size"] = int(entry["size"] * step)
    return scale


class _NameAllocator:
    """Random names that are unique within each directory."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.taken: dict[str, set[str]] = defaultdict(set)

    def _unique(self, parent: str, base: str) -> str:
        name, n = base, 1
        while name in self.taken[parent]:
            n += 1
            name = f"{base}-{n}"
        self.taken[parent].add(name)
        return name

    def directory(self, parent: str) -> str:
        words = self.rng.sample(WORDS, self.rng.choice((1, 1, 2)))
        return self._unique(parent, "_".join(words))

    def file(self, parent: str, cfg: GenConfig) -> tuple[str, Optional[str]]:
        base = "_".join(self.rng.sample(WORDS, self.rng.choice((1, 2, 2))))
        if self.rng.random() < 0.5:
            base += f"-{self.rng.randrange(1000):03d}"
        if self.rng.random() < cfg.hidden_fraction:
            base = "." + base
        ext = None
        if self.rng.random() >= cfg.no_extension_fraction:
            ext = _weighted(self.rng, tuple(cfg.extension_vocab.items()))
        name = f"{base}.{ext}" if ext else base
        stem, n = name, 1
        while name in self.taken[parent]:
            n += 1
            name = f"{base}-{n}.{ext}" if ext else f"{stem}-{n}"
        self.taken[parent].add(name)
        return name, ext


def build_identities(cfg: GenConfig, rng: random.Random) -> tuple[list[dict], list[dict]]:
    """Groups and users with numeric ids; users are spread round-robin over groups in seeded order."""
    group_names = cfg.groups if isinstance(cfg.groups, list) else [f"grp{i + 1:02d}" for i in range(cfg.groups)]
    user_names = cfg.users if isinstance(cfg.users, list) else [f"usr{i + 1:03d}" for i in range(cfg.users)]
    groups = [{"name": name, "gid": FIRST_GID + i, "members": []} for i, name in enumerate(group_names)]
    order = list(range(len(user_names)))
    rng.shuffle(order)
    users = []
    for slot, i in enumerate(order):
        group = groups[slot % len(groups)]
        users.append({"name": user_names[i], "uid": FIRST_UID + i, "gid": group["gid"], "group": group["name"]})
    users.sort(key=lambda u: u["uid"])
    for user in users:
        next(g for g in groups if g["gid"] == user["gid"])["members"].append(user["name"])
    return groups, users


def build_plan(cfg: GenConfig, seed: int, fixed_now: int) -> dict:
    """Deterministic description of every directory, file and symlink in the tree.

    Entries are ordered so that every parent directory precedes its children.
    """
    rng = random.Random(seed)
    names = _NameAllocator(rng)
    groups, users = build_identities(cfg, rng)
    entries: list[dict] = []

    def add(kind: str, path: str, uid: int, gid: int, mode: int, **extra: Any) -> dict:
        mtime, atime = _times(rng, cfg, fixed_now)
        entry = {"kind": kind, "path": path, "uid": uid, "gid": gid, "mode": mode, "mtime": mtime, "atime": atime, **extra}
        entries.append(entry)
        return entry

    add("d", "", 0, 0, 0o755)
    add("d", "groups", 0, 0, 0o755)
    shared_dirs = {}
    for group in groups:
        add("d", f"groups/{group['name']}", 0, group["gid"], 0o2770)
        shared_dirs[group["gid"]] = add("d", f"groups/{group['name']}/shared", 0, group["gid"], 0o2770)["path"]

    user_dirs: dict[int, list[tuple[str, int]]] = {}
    for user in users:
        home = f"groups/{user['group']}/{user['name']}"
        add("d", home, user["uid"], user["gid"], 0o750)
        dirs = [(home, 0)]
        n_dirs = rng.randint(cfg.dirs_per_user_min, cfg.dirs_per_user_max)
        chain_depth = rng.randint(cfg.dir_depth_min, min(cfg.dir_depth_max, n_dirs))
        parent = home
        for depth in range(1, chain_depth + 1):
            parent = f"{parent}/{names.directory(parent)}"
            add("d", parent, user["uid"], user["gid"], _weighted(rng, DIR_MODES))
            dirs.append((parent, depth))
        for _ in range(n_dirs - chain_depth):
            parent, depth = rng.choice([d for d in dirs if d[1] < cfg.dir_depth_max])
            path = f"{parent}/{names.directory(parent)}"
            add("d", path, user["uid"], user["gid"], _weighted(rng, DIR_MODES))
            dirs.append((path, depth + 1))
        user_dirs[user["uid"]] = dirs

    for user in users:
        dirs = [path for path, _ in user_dirs[user["uid"]]]
        files = []
        for _ in range(rng.randint(cfg.files_per_user_min, cfg.files_per_user_max)):
            parent = shared_dirs[user["gid"]] if rng.random() < SHARED_FILE_FRACTION else rng.choice(dirs)
            name, ext = names.file(parent, cfg)
            files.append(add("f", f"{parent}/{name}", user["uid"], user["gid"], _weighted(rng, FILE_MODES),
                             size=_file_size(rng, cfg), ext=ext))
        for _ in range(round(len(files) * cfg.symlink_fraction)):
            parent = rng.choice(dirs)
            if not files or rng.random() < cfg.dangling_symlink_fraction:
                target = f"missing_{rng.choice(WORDS)}"
            else:
                target = os.path.relpath(rng.choice(files)["path"], parent)
            name = names.directory(parent) + ".lnk"
            add("l", f"{parent}/{name}", user["uid"], user["gid"], 0o777, target=target, size=len(target.encode()))

    all_files = [e for e in entries if e["kind"] == "f"]
    for entry in rng.sample(all_files, min(cfg.large_files, len(all_files))):
        entry["size"] = _log_uniform(rng, cfg.large_file_size_min, cfg.large_file_size_max)
    size_scale = _fit_disk_limit(entries, cfg.disk_limit)

    plan = {
        "version": PLAN_VERSION,
        "index_name": cfg.index_name,
        "seed": seed,
        "fixed_now": fixed_now,
        "disk_limit": cfg.disk_limit,
        "allocated_bytes": tree_allocation(entries),
        "size_scale": size_scale,
        "groups": groups,
        "users": users,
        "entries": entries,
    }
    plan["plan_id"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:16]
    return plan


def tree_allocation(entries: list[dict]) -> int:
    """Disk the materialized tree needs: allocated file blocks plus one block per directory."""
    return sum(allocated(e["size"]) if e["kind"] == "f" else ALLOC_BLOCK if e["kind"] == "d" else 0
               for e in entries)


def plan_stats(plan: dict) -> dict:
    """Summary numbers for the manifest and dry runs."""
    entries = plan["entries"]
    files = [e for e in entries if e["kind"] == "f"]
    counts = Counter(e["kind"] for e in entries)
    return {
        "disk_limit": plan["disk_limit"],
        "allocated_bytes": plan["allocated_bytes"],
        "size_scale": plan["size_scale"],
        "largest_file": max((e["size"] for e in files), default=0),
        "directories": counts["d"],
        "files": counts["f"],
        "symlinks": counts["l"],
        "dangling_symlinks": sum(1 for e in entries if e["kind"] == "l" and e["target"].startswith("missing_")),
        "total_file_size": sum(e["size"] for e in files),
        "files_over_1gib": sum(1 for e in files if e["size"] > 1024**3),
        "empty_files": sum(1 for e in files if e["size"] == 0),
        "files_per_uid": dict(sorted(Counter(str(e["uid"]) for e in files).items())),
        "files_per_gid": dict(sorted(Counter(str(e["gid"]) for e in files).items())),
        "files_per_extension": dict(sorted(Counter(e["ext"] or "" for e in files).items())),
        "mtime_min": min(e["mtime"] for e in entries),
        "mtime_max": max(e["mtime"] for e in entries),
        "atime_min": min(e["atime"] for e in entries),
        "atime_max": max(e["atime"] for e in entries),
    }


def expected_counts(plan: dict) -> list[list]:
    """The [type, uid, gid, count, size] rows gen_index_apply.verify_counts should return as root."""
    totals: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    for e in plan["entries"]:
        key = (e["kind"], e["uid"], e["gid"])
        totals[key][0] += 1
        totals[key][1] += e.get("size", 0) if e["kind"] != "d" else 0
    return sorted([*key, n, s] for key, (n, s) in totals.items())


#
# Docker orchestration
#

def resolve_gufi_prefix(cfg: GenConfig) -> Path:
    if cfg.gufi_prefix is not None:
        prefix = cfg.gufi_prefix
    elif found := shutil.which("gufi_dir2index"):
        prefix = Path(found).resolve().parent.parent
    elif gufi_query := os.getenv("GUFI_QUERY", "").strip():
        prefix = Path(gufi_query).resolve().parent.parent
    else:
        raise GenError("Could not find a GUFI install; set gufi_prefix or put gufi_dir2index on PATH")
    prefix = prefix.resolve()
    missing = [name for name in GUFI_BINARIES if not (prefix / "bin" / name).is_file()]
    if missing:
        raise GenError(f"GUFI install {prefix} is missing bin/{', bin/'.join(missing)}")
    return prefix


def resolve_export_root(cfg: GenConfig) -> Path:
    root = cfg.export_root
    if root is None:
        env_root = os.getenv("GUFI_INDEX_ROOT", "").strip()
        if not env_root:
            raise GenError("No export root: pass --export-root or set GUFI_INDEX_ROOT")
        root = Path(env_root)
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise GenError(f"Export root {root} does not exist or is not a directory")
    if not os.access(root, os.W_OK | os.X_OK):
        raise GenError(f"Export root {root} is not writable")
    return root


def check_docker() -> None:
    if shutil.which("docker") is None:
        raise GenError("docker was not found on PATH")
    probe = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise GenError(f"docker is not usable: {probe.stderr.strip() or 'docker info failed'}")


def docker_command(cfg: GenConfig, gufi_prefix: Path, export_root: Path, gen_dir: Path) -> list[str]:
    """argv for the container that materializes the plan and builds the index."""
    return [
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{gufi_prefix}:{gufi_prefix}:ro",
        "-v", f"{export_root}:{CONTAINER_OUT}",
        "-v", f"{gen_dir}:{CONTAINER_GEN}:ro",
        cfg.container_image,
        "python3", f"{CONTAINER_GEN}/{APPLY_SCRIPT.name}",
        "--plan", f"{CONTAINER_GEN}/plan.json",
        "--tree", CONTAINER_TREE,
        "--out", CONTAINER_OUT,
        "--gufi-bin", str(gufi_prefix / "bin"),
    ]


def run_container(cfg: GenConfig, plan: dict, gufi_prefix: Path, export_root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="gen_index_") as tmp:
        gen_dir = Path(tmp)
        gen_dir.chmod(0o755)
        (gen_dir / "plan.json").write_text(json.dumps(plan))
        shutil.copy(APPLY_SCRIPT, gen_dir / APPLY_SCRIPT.name)
        cmd = docker_command(cfg, gufi_prefix, export_root, gen_dir)
        print(f"Running: {' '.join(cmd)}", file=sys.stderr)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise GenError(f"Container exited with status {proc.returncode}; see the output above")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError) as e:
        raise GenError(f"Container did not report verification counts: {proc.stdout[-500:]!r}") from e


def verify(cfg: GenConfig, plan: dict, result: dict, gufi_prefix: Path, index: Path) -> dict:
    """Check the built index against the plan; return verification details for the manifest."""
    expected = expected_counts(plan)
    if result["root_counts"] != expected:
        raise GenError(
            "Index contents do not match the plan.\n"
            f"expected [type, uid, gid, count, size]: {expected}\nindexed: {result['root_counts']}"
        )
    details: dict[str, Any] = {"root_matches_plan": True}

    used = result["disk_used"]
    file_bytes = sum(e["size"] for e in plan["entries"] if e["kind"] == "f")
    if used > plan["disk_limit"]:
        raise GenError(f"Tree occupied {used} bytes on disk, over disk_limit {plan['disk_limit']}")
    if used < file_bytes:
        raise GenError(f"Tree occupied only {used} bytes for {file_bytes} bytes of file data; files are sparse")
    details["disk_used"] = used

    if cfg.index_ownership == "host":
        host_counts = verify_counts(gufi_prefix / "bin", index, cfg.threads)
        if host_counts != expected:
            raise GenError(f"The host user cannot read the whole index: got {host_counts}")
        details["host_readable"] = True
    else:
        probe = plan["settings"]["probe_user"]
        seen = result["user_counts"] or []
        own = [row for row in seen if row[0] in ("f", "l")]
        foreign = [row for row in own if row[2] != probe["gid"]]
        root_files = sum(row[3] for row in expected if row[0] == "f")
        seen_files = sum(row[3] for row in own if row[0] == "f")
        if not own or foreign or seen_files >= root_files:
            raise GenError(f"Permission check failed for {probe['name']}: saw {seen}")
        details["probe_user"] = {**probe, "files_visible": seen_files, "files_total": root_files}
    return details


#
# Manifest
#

def gufi_version(gufi_prefix: Path) -> str:
    proc = subprocess.run([str(gufi_prefix / "bin" / "gufi_query"), "--version"], capture_output=True, text=True)
    return proc.stdout.strip()


def write_manifest(cfg: GenConfig, plan: dict, verification: dict, gufi_prefix: Path, export_root: Path) -> Path:
    stats = plan_stats(plan)
    fixed_now = plan["fixed_now"]
    manifest = {
        "manifest_id": plan["plan_id"],
        "index_name": plan["index_name"],
        "index_path": str(export_root / plan["index_name"]),
        "seed": plan["seed"],
        "fixed_now": fixed_now,
        "fixed_now_iso": datetime.fromtimestamp(fixed_now, timezone.utc).isoformat(),
        "build_timestamp": datetime.now(timezone.utc).isoformat(),
        "ctime_note": "ctime is the build time; only mtime and atime are spread relative to fixed_now",
        "index_ownership": cfg.index_ownership,
        "treesummary": cfg.treesummary,
        "gufi_version": gufi_version(gufi_prefix),
        "container_image": cfg.container_image,
        "groups": plan["groups"],
        "users": plan["users"],
        "stats": stats,
        "verification": verification,
        "config": json.loads(cfg.model_dump_json()),
    }
    path = export_root / f"{plan['index_name']}.manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path


#
# CLI
#

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a synthetic GUFI eval index")
    parser.add_argument("--config", type=Path, help="YAML or JSON config (see gen_index.example.yaml)")
    parser.add_argument("--seed", type=int, help="Override the random seed")
    parser.add_argument("--export-root", type=Path, help="Index destination (default: GUFI_INDEX_ROOT)")
    parser.add_argument("--index-name", help="Index directory name")
    parser.add_argument("--fixed-now", help="Reference time for timestamps (ISO date/time or epoch seconds)")
    parser.add_argument("--ownership", choices=("host", "preserve"), help="Override index_ownership")
    parser.add_argument("--force", action="store_true", help="Replace an existing index with the same name")
    parser.add_argument("--dry-run", action="store_true", help="Build the plan and print its statistics only")
    parser.add_argument("--keep-plan", type=Path, help="Also write the plan JSON to this path")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv(HERE / ".env")
    args = parse_args(argv)
    try:
        cfg = load_config(args.config, {
            "seed": args.seed,
            "export_root": args.export_root,
            "index_name": args.index_name,
            "fixed_now": args.fixed_now,
            "index_ownership": args.ownership,
        })
        seed = cfg.seed if cfg.seed is not None else secrets.randbits(32)
        plan = build_plan(cfg, seed, cfg.resolved_fixed_now())
        if args.keep_plan:
            args.keep_plan.write_text(json.dumps(plan, indent=1))

        if args.dry_run:
            print(json.dumps({"seed": seed, "plan_id": plan["plan_id"], "stats": plan_stats(plan)}, indent=2))
            return 0

        check_docker()
        gufi_prefix = resolve_gufi_prefix(cfg)
        export_root = resolve_export_root(cfg)
        index = export_root / cfg.index_name
        if index.exists() and not args.force:
            raise GenError(f"Index {index} already exists; pass --force to replace it")

        plan["settings"] = {
            "threads": cfg.threads,
            "treesummary": cfg.treesummary,
            "ownership": cfg.index_ownership,
            "host_uid": os.getuid(),
            "host_gid": os.getgid(),
            "probe_user": plan["users"][0],
        }
        print(f"Building {index} from seed {seed} ({len(plan['entries'])} entries)", file=sys.stderr)
        result = run_container(cfg, plan, gufi_prefix, export_root)
        verification = verify(cfg, plan, result, gufi_prefix, index)
        manifest = write_manifest(cfg, plan, verification, gufi_prefix, export_root)
    except GenError as e:
        print(f"gen_index: {e}", file=sys.stderr)
        return 1

    print(f"Index written to {index}", file=sys.stderr)
    print(f"Manifest written to {manifest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
