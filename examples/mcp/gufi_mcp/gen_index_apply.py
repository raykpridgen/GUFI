#!/usr/bin/env python3
"""Container-side half of gen_index.py. Standard library only.

Runs as root inside a throwaway container: materializes a tree plan under
--tree, indexes it into --out with the mounted GUFI install, and prints one
JSON line of verification counts to stdout. Ownership handling follows the
plan's "ownership" setting.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

VERIFY_PIPELINE = {
    "I": "CREATE TABLE intermediate(type TEXT, uid INT64, gid INT64, n INT64, s INT64);",
    "K": "CREATE TABLE aggregate(type TEXT, uid INT64, gid INT64, n INT64, s INT64);",
    "J": "INSERT INTO aggregate SELECT type, uid, gid, SUM(n), SUM(s) FROM intermediate GROUP BY type, uid, gid;",
    "G": "SELECT type, uid, gid, SUM(n), SUM(s) FROM aggregate GROUP BY type, uid, gid;",
}
# Run as separate queries: when -S returns no rows (an INSERT returns none) gufi_query skips -E.
VERIFY_SCANS = (
    ("-E", "INSERT INTO intermediate SELECT type, uid, gid, COUNT(*), SUM(size) FROM entries GROUP BY type, uid, gid;"),
    ("-S", "INSERT INTO intermediate SELECT 'd', uid, gid, 1, 0 FROM summary WHERE isroot = 1;"),
)


def log(message: str) -> None:
    print(f"[gen_index_apply] {message}", file=sys.stderr, flush=True)


def run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    log(" ".join(cmd))
    return subprocess.run(cmd, check=check, text=True, **kwargs)


class DiskLimitError(Exception):
    pass


def check_free_space(tree: Path, needed: int, limit: int) -> None:
    if needed > limit:
        raise DiskLimitError(f"plan needs {needed} bytes but disk_limit is {limit}")
    tree.mkdir(parents=True, exist_ok=True)
    st = os.statvfs(tree)
    free = st.f_bavail * st.f_frsize
    if free < needed * 1.05:
        raise DiskLimitError(f"{tree} has {free} bytes free; the tree needs {needed} (+5% headroom)")


def allocate(f, size: int) -> None:
    """Give the file real blocks (not a sparse hole) so block-based usage matches its size."""
    if size == 0:
        return
    try:
        os.posix_fallocate(f.fileno(), 0, size)
    except OSError:
        chunk = b"\0" * (1 << 20)
        remaining = size
        while remaining:
            remaining -= f.write(chunk[:min(remaining, len(chunk))])


def disk_used(root: Path) -> int:
    """Bytes actually allocated under root, as du would count them."""
    total = os.lstat(root).st_blocks * 512
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            total += os.lstat(os.path.join(dirpath, name)).st_blocks * 512
    return total


def materialize(plan: dict, tree: Path) -> Path:
    """Create every planned entry, then set times bottom-up so children don't disturb them."""
    os.umask(0)
    root = tree / plan["index_name"]
    entries = plan["entries"]
    limit = plan["disk_limit"]
    written = 0
    for entry in entries:
        path = root / entry["path"] if entry["path"] else root
        kind = entry["kind"]
        if kind == "d":
            path.mkdir(parents=entry["path"] == "")
        elif kind == "f":
            written += entry["size"]
            if written > limit:
                raise DiskLimitError(f"writing {entry['path']} would exceed disk_limit {limit}")
            with open(path, "xb") as f:
                allocate(f, entry["size"])
        elif kind == "l":
            os.symlink(entry["target"], path)
        else:
            raise ValueError(f"unknown entry kind {kind!r} for {entry['path']!r}")
        os.lchown(path, entry["uid"], entry["gid"])
        if kind != "l":
            os.chmod(path, entry["mode"])

    # Deepest paths first, so setting a directory's times comes after all writes inside it.
    for entry in sorted(entries, key=lambda e: e["path"].count("/") + (1 if e["path"] else 0), reverse=True):
        path = root / entry["path"] if entry["path"] else root
        os.utime(path, ns=(entry["atime"] * 10**9, entry["mtime"] * 10**9), follow_symlinks=False)
    return root


def verify_counts(gufi_bin: Path, index: Path, threads: int, as_user: tuple[int, int] | None = None) -> list[list]:
    """Counts and size totals per (type, uid, gid), as seen by root or by as_user."""
    kwargs = {}
    if as_user is not None:
        # Permission-denied directories make gufi_query exit non-zero; that is the expected outcome here.
        kwargs = {"user": as_user[0], "group": as_user[1], "extra_groups": [], "check": False}
    rows = []
    for scan_flag, scan_sql in VERIFY_SCANS:
        cmd = [str(gufi_bin / "gufi_query"), "-n", str(threads), "-d", "|", scan_flag, scan_sql]
        for flag in ("I", "K", "J", "G"):
            cmd += [f"-{flag}", VERIFY_PIPELINE[flag]]
        cmd.append(str(index))
        proc = run(cmd, capture_output=True, **kwargs)
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            kind, uid, gid, n, s = line.split("|")
            rows.append([kind, int(uid), int(gid), int(n), int(s or 0)])
    return sorted(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--tree", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--gufi-bin", required=True, type=Path)
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text())
    settings = plan["settings"]
    threads = settings["threads"]
    index = args.out / plan["index_name"]

    if index.exists():
        log(f"removing existing index {index}")
        run(["rm", "-rf", "--one-file-system", str(index)])

    try:
        check_free_space(args.tree, plan["allocated_bytes"], plan["disk_limit"])
        source = materialize(plan, args.tree)
    except DiskLimitError as e:
        log(f"aborting: {e}")
        return 3
    used = disk_used(source)
    log(f"materialized {len(plan['entries'])} entries under {source} ({used} bytes on disk)")
    if used > plan["disk_limit"]:
        log(f"aborting: tree occupies {used} bytes, over disk_limit {plan['disk_limit']}")
        return 3

    run([str(args.gufi_bin / "gufi_dir2index"), "-n", str(threads), str(source), str(args.out)])
    if settings["treesummary"]:
        run([str(args.gufi_bin / "gufi_treesummary_all"), "-n", str(threads), str(index)])

    result = {"root_counts": verify_counts(args.gufi_bin, index, threads), "user_counts": None, "disk_used": used}

    if settings["ownership"] == "preserve":
        probe = settings["probe_user"]
        result["user_counts"] = verify_counts(args.gufi_bin, index, threads, (probe["uid"], probe["gid"]))
    else:
        run(["chown", "-R", f"{settings['host_uid']}:{settings['host_gid']}", str(index)])

    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
