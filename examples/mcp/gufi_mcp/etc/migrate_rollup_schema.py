#!/usr/bin/env python3
"""Upgrade GUFI index databases from rollupscore to canrollup/isrolledup (2026-06 schema)."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

INT64_MAX = 9223372036854775807
INT64_MIN = -9223372036854775808

# View definitions from src/dbutils.c (current tree); keep in sync with GUFI if upstream changes.
VRSUMMARY_CREATE = """
DROP VIEW IF EXISTS vrsummary;
CREATE VIEW vrsummary AS SELECT
REPLACE(summary.name, RTRIM(summary.name, REPLACE(summary.name, '/', '')), '') AS dname,
summary.name AS sname,
summary.isrolledup AS sroll,
(SELECT COUNT(*) FROM summary AS c WHERE c.pinode == summary.inode) AS srollsubdirs,
summary.*
FROM summary;
"""

PENTRIES_CREATE = """
DROP VIEW IF EXISTS pentries;
CREATE VIEW pentries AS SELECT
entries.*, summary.inode AS pinode,
summary.pinode AS ppinode
FROM entries, summary
WHERE isroot == 1
UNION
SELECT * FROM pentries_rollup;
"""

VRPENTRIES_CREATE = """
DROP VIEW IF EXISTS vrpentries;
CREATE VIEW vrpentries AS SELECT
REPLACE(vrsummary.name, RTRIM(vrsummary.name, REPLACE(vrsummary.name, '/', '')), '') AS dname,
vrsummary.name         AS sname,      vrsummary.mode        AS dmode,
vrsummary.nlink        AS dnlink,
vrsummary.uid          AS duid,       vrsummary.gid         AS dgid,
vrsummary.size         AS dsize,
vrsummary.blksize      AS dblksize,   vrsummary.blocks      AS dblocks,
vrsummary.atime        AS datime,
vrsummary.mtime        AS dmtime,
vrsummary.ctime        AS dctime,
vrsummary.linkname     AS dlinkname,
vrsummary.totfiles     AS dtotfiles,  vrsummary.totlinks    AS dtotlinks,
vrsummary.minuid       AS dminuid,    vrsummary.maxuid      AS dmaxuid,
vrsummary.mingid       AS dmingid,    vrsummary.maxgid      AS dmaxgid,
vrsummary.minsize      AS dminsize,   vrsummary.maxsize     AS dmaxsize,
vrsummary.totzero      AS dtotzero,
vrsummary.totltk       AS dtotltk,    vrsummary.totmtk      AS dtotmtk,
vrsummary.totltm       AS totltm,     vrsummary.totmtm      AS dtotmtm,
vrsummary.totmtg       AS dtotmtg,    vrsummary.totmtt      AS dtotmtt,
vrsummary.totsize      AS dtotsize,
vrsummary.mincrtime    AS dminctime,  vrsummary.maxctime    AS dmaxctime,
vrsummary.totctime     AS dtotctime,
vrsummary.minmtime     AS dminmtime,  vrsummary.maxmtime    AS dmaxmtime,
vrsummary.totmtime     AS dtotmtime,
vrsummary.minatime     AS dminatime,  vrsummary.maxatime    AS dmaxatime,
vrsummary.totatime     AS dtotatime,
vrsummary.minblocks    AS dminblocks, vrsummary.maxblocks   AS dmaxblocks,
vrsummary.totblocks    AS dtotblocks,
vrsummary.totxattr     AS dtotxattr,  vrsummary.depth       AS ddepth,
vrsummary.mincrtime    AS dmincrtime, vrsummary.maxcrtime   AS dmaxcrtime,
vrsummary.totcrtime    AS dtotcrtime,
vrsummary.isrolledup   AS sroll,      vrsummary.isroot      AS atroot,
vrsummary.srollsubdirs AS srollsubdirs,
pentries.*
FROM vrsummary, pentries
WHERE vrsummary.inode == pentries.pinode;
"""

SUMMARYLONG_CREATE = """
DROP VIEW IF EXISTS summarylong;
CREATE VIEW summarylong AS SELECT * FROM summary;
"""

VRSUMMARYLONG_CREATE = """
DROP VIEW IF EXISTS vrsummarylong;
CREATE VIEW vrsummarylong AS SELECT * FROM vrsummary;
"""

VSUMMARY_VIEWS = """
DROP VIEW IF EXISTS vsummarydir;
CREATE VIEW vsummarydir AS SELECT * FROM summary WHERE rectype == 0;
DROP VIEW IF EXISTS vsummaryuser;
CREATE VIEW vsummaryuser AS SELECT * FROM summary WHERE rectype == 1;
DROP VIEW IF EXISTS vsummarygroup;
CREATE VIEW vsummarygroup AS SELECT * FROM summary WHERE rectype == 2;
"""

VIEW_REBUILD = (
    VRSUMMARY_CREATE
    + PENTRIES_CREATE
    + VRPENTRIES_CREATE
    + SUMMARYLONG_CREATE
    + VRSUMMARYLONG_CREATE
    + VSUMMARY_VIEWS
)


def summary_columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM pragma_table_info('summary')").fetchall()
    return {r[0] for r in rows}


def seed_root_summary(db_path: Path, dir_path: Path, summary_name: str, dry_run: bool) -> str:
    """Insert isroot summary row when a GUFI index root db.db is empty (shell directory only)."""
    conn = sqlite3.connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM summary WHERE isroot = 1 LIMIT 1").fetchone():
            return "root_exists"
        cols = [r[1] for r in conn.execute("PRAGMA table_info(summary)")]
        if dry_run:
            return "would_seed_root"

        st = os.stat(dir_path, follow_symlinks=False)
        row: dict[str, object] = {}
        row["name"] = summary_name
        row["type"] = "d"
        row["inode"] = str(st.st_ino)
        row["mode"] = st.st_mode
        row["nlink"] = st.st_nlink
        row["uid"] = st.st_uid
        row["gid"] = st.st_gid
        row["size"] = st.st_size
        row["blksize"] = getattr(st, "st_blksize", 4096)
        row["blocks"] = st.st_blocks
        row["atime"] = int(st.st_atime)
        row["mtime"] = int(st.st_mtime)
        row["ctime"] = int(st.st_ctime)
        row["linkname"] = ""
        row["xattr_names"] = b""
        row["crtime"] = int(st.st_ctime)
        for c in cols:
            if c in row:
                continue
            if c.startswith("max"):
                row[c] = INT64_MAX
            elif c.startswith("min"):
                row[c] = INT64_MIN
            elif c.startswith("tot") or c in ("totblocks",):
                row[c] = 0
            elif c in ("ossint1", "ossint2", "ossint3", "ossint4"):
                row[c] = 0
            elif c in ("osstext1", "osstext2"):
                row[c] = ""
            elif c == "depth":
                row[c] = 0
            elif c == "rectype":
                row[c] = 0
            elif c == "pinode":
                row[c] = "0"
            elif c == "isroot":
                row[c] = 1
            elif c == "canrollup":
                row[c] = 0
            elif c == "isrolledup":
                row[c] = 0
            elif c == "rollupscore":
                row[c] = 0
            else:
                row[c] = 0
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO summary ({', '.join(cols)}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )
        conn.commit()
        return "seeded_root"
    finally:
        conn.close()


def migrate_db(db_path: Path, dry_run: bool) -> str:
    conn = sqlite3.connect(db_path)
    try:
        cols = summary_columns(conn)
        if "canrollup" in cols and "isrolledup" in cols:
            if "rollupscore" not in cols:
                return "already_current"
            # Has new columns and old; still rebuild views if vrsummary references rollupscore
            sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='vrsummary' AND type='view'"
            ).fetchone()
            if sql_row and sql_row[0] and "rollupscore" not in sql_row[0]:
                return "already_current"
        elif "rollupscore" not in cols:
            return "unknown_schema"

        if dry_run:
            return "would_migrate"

        conn.execute("PRAGMA foreign_keys=OFF")
        if "canrollup" not in cols:
            conn.execute("ALTER TABLE summary ADD COLUMN canrollup INT64 DEFAULT 0")
        if "isrolledup" not in cols:
            conn.execute("ALTER TABLE summary ADD COLUMN isrolledup INT64 DEFAULT 0")
        if "rollupscore" in cols:
            conn.execute(
                "UPDATE summary SET isrolledup = rollupscore, "
                "canrollup = CASE WHEN rollupscore != 0 THEN 1 ELSE 0 END"
            )
        conn.executescript(
            "DROP VIEW IF EXISTS vrpentries;"
            "DROP VIEW IF EXISTS vrsummarylong;"
            "DROP VIEW IF EXISTS vrsummary;"
            "DROP VIEW IF EXISTS pentries;"
            "DROP VIEW IF EXISTS summarylong;"
            "DROP VIEW IF EXISTS vsummarydir;"
            "DROP VIEW IF EXISTS vsummarygroup;"
            "DROP VIEW IF EXISTS vsummaryuser;"
        )
        conn.executescript(VIEW_REBUILD)
        conn.commit()
        return "migrated"
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index_path", type=Path, help="Root of a GUFI index (directory containing db.db)")
    parser.add_argument(
        "--copy-from",
        type=Path,
        help="If set, copy this index tree to index_path before migrating (index_path must not exist)",
    )
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument(
        "--seed-root",
        action="store_true",
        help="Insert a directory summary row in index_path/db.db from stat( index_path ) "
        "when missing (for hollow index roots so gufi_ls lists them under IndexRoot)",
    )
    args = parser.parse_args()

    if args.copy_from:
        if args.index_path.exists():
            print(f"Refusing to copy: {args.index_path} already exists", file=sys.stderr)
            return 1
        print(f"Copying {args.copy_from} -> {args.index_path} ...")
        shutil.copytree(args.copy_from, args.index_path, symlinks=True)

    if not (args.index_path / "db.db").is_file():
        print(f"Not a GUFI index (no db.db): {args.index_path}", file=sys.stderr)
        return 1

    if args.seed_root:
        root_db = args.index_path / "db.db"
        name = args.index_path.name.rstrip("/") + "/"
        status = seed_root_summary(root_db, args.index_path, name, args.dry_run)
        print(f"Root summary: {status} ({root_db})")
        return 0

    dbs = sorted(args.index_path.rglob("db.db"))
    counts: dict[str, int] = {}
    for db in dbs:
        status = migrate_db(db, args.dry_run)
        counts[status] = counts.get(status, 0) + 1

    print(f"Processed {len(dbs)} db.db files: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
