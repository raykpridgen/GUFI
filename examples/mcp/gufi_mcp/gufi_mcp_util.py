import sqlite3
import sys
from pathlib import Path
from typing import Any, Literal, Optional
from sqlglot import parse_one, ParseError, exp
import os
from pydantic import BaseModel, Field, field_validator, model_validator, PrivateAttr
from dotenv import load_dotenv
import re
import subprocess
import json

load_dotenv()

SCHEMAFILE = os.getenv('SCHEMAFILE')
REMOTEHOST = os.getenv('REMOTEHOST')
MCPTRANSPORT = os.getenv('MCPTRANSPORT')
MCPSRVHOST = os.getenv('MCPSRVHOST')
MCPSRVPORT = os.getenv('MCPSRVPORT')
GUFIVTLIB = os.getenv('GUFIVTLIB')
GUFI_INDEX_ROOT = os.getenv('GUFI_INDEX_ROOT')
GUFI_QUERY = os.getenv('GUFI_QUERY')
from enum import Enum

class AggregateOption(BaseModel):
    option: Literal["treesummary", "vrsummary", "vrpentries"] = Field(
        default="vrpentries",
        description=(
            "One of three tables to query for compilation in an aggregate pipeline."
            "These three tables have context for columns available in the gufi_schemas resource."
        )
    )
    sql: str = Field(
        default="select name, size from vrpentries where type = 'f'",
        description=(
            "The actual SQL line that will be queried for the table specified"
            "Although table was specified in a separate attribute, the entire line of sql, INCLUDING the table should be included like the default of this model."
            "The additional condition WHERE is encouraged, however it is advised not to use LIMIT, ORDER BY, or other modifiers since per-thread functionality of GUFI may cause unexpected output."
        )
    )

    @field_validator("sql")
    @classmethod
    def sql_heuristic(cls, sql: Optional[str]) -> Optional[str]:
        if sql is None or not is_valid_sql_query(sql):
            raise ValueError("must have valid SQL with an aggregate option")
        return sql

    @model_validator(mode="after")
    def confirm_columns(self):
        ''' Confirm that each column mentioned in SQL exists in the table it references '''
        # Columns used in SQL of this option
        cols_referenced = get_columns_from_sqlin(self.sql)

        cols = get_schema_hints(self.option)

        for col in cols_referenced:
            try:
                cols[col]
            except KeyError:
                raise ValueError(f"column {col} not found in schema for {self.option}")

class GufiAggregateQuery(BaseModel):

    _columns_used: list = PrivateAttr(default_factory=list)

    index: str = Field(
        default=".",
        description=("The index to be queried. Must be present in the results of the gufi_indexes resource."
                     "If using treesummary as one of the scatter options, note that the index must it active for complete results. This can be confirmed through the gufi_indexes resource.")
    )
    # I, K managed internally

    # T, S, E managed here
    scatter_sql: list[AggregateOption] = Field(
        default_factory=list,
        min_length=1,
        description=(
            "Each entry in this list is a query from etiher treesummary, vrsummary, or vrpentries."
            "These entries serve as the 'scatter' portion of the query, where separate threads compile their own tables of the queried data based on distributed responsibility of the index tree."
            "The queried data will be compiled into intermediate tables further down the pipeline, which will be gathered and further queried from."
        ),
    )

    # I spec
    intermediate_specs: str = Field(
        default="size INT64",
        description=(
            "This is the specifier for the comma separated data types of the intermediate tables. They should include any columns addressed in the scatter sql."
        )
    )

    # J logic
    gather_sql: str = Field(
        default="select SUM(size) from intermediate",
        description=("This SQL is responsible for the 'gather' portion of the query, where GUFI logic compiles data queried in the previous step from per-thread tables into a single table."
                     "This allows for additional SQL to be applied between tables, as seen in the default.")
    )

    # K spec
    aggregate_spec: str = Field(
        default="total INT64",
        description = (
            "This is the specifier for the comma separated data types of the aggregate table. They should include any columns addressed in the aggregate SQL."
        )
    )

    # G logic
    final_sql: str = Field(
        default="select SUM(total) from aggregate",
        description = (
            "This is the final SQL the pipeline uses to extract the result from the aggregate SQL table."
            "It extracts columns defined in the aggregate table that have been transformed from the intermediate table."
        )
    )

    @field_validator("index")
    @classmethod
    def index_valid(cls, index: str) -> str:
        if index not in get_gufi_indexes():
            raise ValueError("Invalid index, not found in root.")
        else:
            return index

    @model_validator(mode="after")
    def check_scatter_treesum(self):
        if has_treesummary(self.index):
            return self
        else:
            for scatter_opt in self.scatter_sql:
                if scatter_opt.option == "treesummary":
                    raise ValueError("This index does not have treesummary active, but the SQL references using the treesummary table.")

    @model_validator(mode="before")
    def arrange_scatter_order(self):

        if len(self.scatter_sql) < 1:
            raise ValueError("Scatter SQL is empty.")
        elif len(self.scatter_sql) < 2:
            return self

        treesum_bound = 0
        scan = 0
        entries_bound = len(self.scatter_sql) - 1

        option_list = self.scatter_sql

        # Dutch national flag problem
        # T = 0 S = 1 E = 2
        while scan <= entries_bound:
            if option_list[scan] == "treesummary":
                option_list[treesum_bound], option_list[scan] = option_list[scan], option_list[treesum_bound]
                treesum_bound += 1
                scan += 1
            elif option_list[scan] == "vrsummary":
                scan += 1
            else:  # arr[scan] == 2
                option_list[scan], option_list[entries_bound] = option_list[entries_bound], option_list[scan]
                entries_bound -= 1

        # Should be 3-tiered sorted
        self.scatter_sql = option_list
        return self

    @model_validator(mode="before")
    def get_starting_cols(self):
        ''' Get the columns from scatter SQL to compare through pipeline '''

        total_cols = []

        for scatter_sql_item in self.scatter_sql:
            total_cols.append(get_columns_from_sqlin(scatter_sql_item.sql))

        # remove duplicates
        self._columns_used = list(set(total_cols))
        return self


    @model_validator(mode="after")
    def check_intermediate_specs(self):
        ''' Confirm that the intermediate_specs exist as columns of the same type in each aggregate option '''

        # Should already be set in private variable
        for col in self.intermediate_specs.split(","):
            # Ignore size specifiers
            if col not in self._columns_used and col not in {"STR, INT64"}:
                raise ValueError("Error: intermediate table specs reference column not queried in scatter SQL.")

        return self

    @model_validator(mode="after")
    def check_aggregate_specs(self):
        ''' Confirm that the aggregate_specs exist as columns of the same type in intermediate table '''

        # Pull from own class
        # col might have new name so just check size

        return self

    @model_validator(mode="after")
    def check_gather(self):
        ''' Confirm that the column being selected from intermediate exists in it '''

        if not is_valid_sql_query(self.gather_sql, "sqlite"):
            raise ValueError("Error: gather SQL has invalid syntax.")
        # Extract column either raw or inside parenthesese from function like SUM()
        # If col not found in intermediate, error
        # Also fix any inconsistency with temp table naming, class should use its own for consistency
        # Finally, use heuristic SQL function to check syntax

        return self

    @model_validator(mode="after")
    def check_final_sql(self):
        ''' Confirm that the final SQL is valid '''

        if not is_valid_sql_query(self.final_sql, "sqlite"):
            raise ValueError("Error: final SQL has invalid syntax.")

        # Pull from own class
        # Check that col(s) pulled from aggregate exist in that table
        # If col not found in aggregate, error
        # Also check syntax with sql heuristic func

        return self

    def build_query_string(self):

        sql_query_string = f'''
        CREATE VIRTUAL TABLE temp.gufi_mcp USING gufi_vt(
        I='CREATE TABLE intermediate({self.intermediate_specs}',
        '''

        # Build T, S, E portion
        for agg_opt in self.scatter_sql:
            if agg_opt.option == "treesummary":
                sql_query_string += f"T='INSERT INTO intermediate {agg_opt.sql}',"
            elif agg_opt.option == "vrsummary":
                sql_query_string += f"S='INSERT INTO intermediate {agg_opt.sql}',"
            elif agg_opt.option == "vrpentries":
                sql_query_string += f"E='INSERT INTO intermediate {agg_opt.sql}',"

        sql_query_string += f"K='CREATE TABLE aggregate({self.aggregate_spec}',"
        sql_query_string += f"J='INSERT INTO aggregate {self.gather_sql}',"
        sql_query_string += f"G='{self.final_sql}',"
        sql_query_string += f"index='{self.index}'"

        return sql_query_string

# Reference:
'''

        CREATE VIRTUAL TABLE temp.gufi_mcp
        USING gufi_vt(
            I='CREATE TABLE intermediate(size INT64)', 
            E='INSERT INTO intermediate SELECT size FROM entries WHERE type="f"', 
            K='CREATE TABLE aggregate(total INT64)', 
            J='INSERT INTO aggregate SELECT SUM(size) FROM intermediate', 
            G='SELECT SUM(total) FROM aggregate', 
            index='Downloads'
        )
'''



class GufiSQLOption(str, Enum):
    I = "-I"
    T = "-T"
    S = "-S"
    E = "-E"
    K = "-K"
    J = "-J"
    G = "-G"
    F = "-F"

# Object for GUFI options
class GufiOption(BaseModel):
    option: GufiSQLOption
    sql: str

# Object to handle query construction
class GufiQuery(BaseModel):
    index: str
    sql_options: list[GufiOption] = Field(default_factory=list)
    config: list[str] = Field(default_factory=list)
    delimiter: str = "\t"

# Object to handle query returns
class GufiQueryResult(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0

# Object for tool results
class GufiToolResult(BaseModel):
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0

class GufiToolArgsLS(BaseModel):
    """GUFI equivalent of ls: List directory contents."""

    path: str = Field(
        default=".",
        description=(
            "Directory to list. Absolute path preferred (starts with /). "
            "Relative paths are resolved from the current working directory. "
            "Example: '/home/user/project' or 'src'."
        ),
        min_length=1,
    )

    all: bool = Field(
        default=False,
        description="If true, include files/directories starting with '.' (like ls -a)",
    )

    almost_all: bool = Field(
        default=False,
        description="If true, include files/directories starting with '.' (like ls -a), without implied '.' and '..'",
    )

    ignore_backups: bool = Field(
        default=False,
        description="If true, exclude implied entries ending with ~",
    )

    full_time: bool = Field(
        default=False,
        description="If true, list entries with full time, similar to 'ls -l --time-style=full-iso'",
    )

    no_group: bool = Field(
        default=False,
        description="If true, exclude group names. Use during long output executions",
    )

    ignore_backups: bool = Field(
        default=False,
        description="If true, exclude implied entries ending with ~",
    )

    human_readable: bool = Field(
        default=False,
        description="If true, print sizes with common suffixes like k, m, g etc",
    )

    inode: bool = Field(
        default=False,
        description="If true, print index number of each file",
    )

    list_format: bool = Field(
        default=False,
        description="If true, print entries in list format, like 'ls -l'",
    )

    numeric_uid_gid: bool = Field(
        default=False,
        description="If true, print in list format with numeric UID and GID for entries",
    )

    reverse: bool = Field(
        default=False,
        description="If true, reverse order when sorting output",
    )

    recursive: bool = Field(
        default=False,
        description="If true, list subdirectories recursively",
    )

    size: bool = Field(
        default=False,
        description="If true, print allocated size of each file in blocks",
    )

    sort_size: bool = Field(
        default=False,
        description="If true, sort by file size descending before output",
    )

    sort_time: bool = Field(
        default=False,
        description="If true, sort by newest modification time before output",
    )

    block_size: Optional[int] = Field(
        default=4096,
        ge=1,
        le=1048576,
        description=(
            "Only affects output when listing options with -l"
            "Scales by input integer when listing options"
        ),
    )

    time_style: Literal["full-iso", "iso", "locale", "long-iso"] = Field(
        default="iso",
        description="Specify how to format time/date when using list_format",
    )

    model_config = {
        "extra": "forbid",
    }

args_ls_dict = {
    "all": "-a",
    "almost_all": "-A",
    "ignore_backups": "-B",
    "full_time": "--full-time",
    "no_group": "--no-group",
    "human_readable": "-h",
    "inode": "-i",
    "list_format": "-l",
    "numeric_uid_gid": "-n",
    "reverse": "-r",
    "recursive": "-R",
    "size": "-s",
    "sort_size": "-S",
    "sort_time": "-t",
    "block_size": "--block-size",
    "time_style": "--time-style"
}

class GufiStatsStatistic(str, Enum):
    DEPTH = "depth"
    FILESIZE = "filesize"
    FILECOUNT = "filecount"
    LINKCOUNT = "linkcount"
    DIRCOUNT = "dircount"
    LEAF_DIRS = "leaf-dirs"
    LEAF_DEPTH = "leaf-depth"
    LEAF_FILES = "leaf-files"
    LEAF_LINKS = "leaf-links"
    TOTAL_FILESIZE = "total-filesize"
    TOTAL_FILECOUNT = "total-filecount"
    TOTAL_LINKCOUNT = "total-linkcount"
    TOTAL_DIRCOUNT = "total-dircount"
    TOTAL_LEAF_FILES = "total-leaf-files"
    TOTAL_LEAF_LINKS = "total-leaf-links"
    FILES_PER_LEVEL = "files-per-level"
    LINKS_PER_LEVEL = "links-per-level"
    DIRS_PER_LEVEL = "dirs-per-level"
    EXTENSIONS = "extensions"
    EXTENSIONS_HIST = "extensions-hist"
    FILESIZE_LOG2_BINS = "filesize-log2-bins"
    FILESIZE_LOG1024_BINS = "filesize-log1024-bins"
    FILESIZE_LOG2_BINS_HIST = "filesize-log2-bins-hist"
    FILESIZE_LOG1024_BINS_HIST = "filesize-log1024-bins-hist"
    DIRFILECOUNT_LOG2_BINS = "dirfilecount-log2-bins"
    DIRFILECOUNT_LOG1024_BINS = "dirfilecount-log1024-bins"
    DIRFILECOUNT_LOG2_BINS_HIST = "dirfilecount-log2-bins-hist"
    DIRFILECOUNT_LOG1024_BINS_HIST = "dirfilecount-log1024-bins-hist"
    FILE_AGE_ATIME = "file-age-atime"
    FILE_AGE_MTIME = "file-age-mtime"
    FILE_AGE_CTIME = "file-age-ctime"
    DIR_AGE_ATIME = "dir-age-atime"
    DIR_AGE_MTIME = "dir-age-mtime"
    DIR_AGE_CTIME = "dir-age-ctime"
    FILE_AGE_ATIME_HIST = "file-age-atime-hist"
    FILE_AGE_MTIME_HIST = "file-age-mtime-hist"
    FILE_AGE_CTIME_HIST = "file-age-ctime-hist"
    DIR_AGE_ATIME_HIST = "dir-age-atime-hist"
    DIR_AGE_MTIME_HIST = "dir-age-mtime-hist"
    DIR_AGE_CTIME_HIST = "dir-age-ctime-hist"
    UNIQUE_FILE_UID_COUNT = "unique-file-uid-count"
    UNIQUE_FILE_GID_COUNT = "unique-file-gid-count"
    UNIQUE_LINK_UID_COUNT = "unique-link-uid-count"
    UNIQUE_LINK_GID_COUNT = "unique-link-gid-count"
    UNIQUE_DIR_UID_COUNT = "unique-dir-uid-count"
    UNIQUE_DIR_GID_COUNT = "unique-dir-gid-count"
    AVERAGE_LEAF_FILES = "average-leaf-files"
    AVERAGE_LEAF_LINKS = "average-leaf-links"
    AVERAGE_LEAF_SIZE = "average-leaf-size"
    MEDIAN_LEAF_FILES = "median-leaf-files"
    MEDIAN_LEAF_LINKS = "median-leaf-links"
    MEDIAN_LEAF_SIZE = "median-leaf-size"
    DUPLICATE_NAMES = "duplicate-names"
    UID_SIZE = "uid-size"
    GID_SIZE = "gid-size"


class GufiToolArgsDU(BaseModel):
    """GUFI equivalent of du: Summarize disk usage."""

    paths: list[str] = Field(
        default_factory=lambda: ["."],
        min_length=1,
        description=(
            "One or more paths to summarize. Absolute paths preferred (start with /). "
            "Directories should have treesummary data for meaningful results."
        ),
    )

    apparent_size: bool = Field(
        default=False,
        description=(
            "If true, print apparent sizes rather than disk usage (may be larger than "
            "disk usage for sparse or fragmented files)."
        ),
    )

    block_size: Optional[int] = Field(
        default=None,
        ge=1,
        le=1048576,
        description="Scale reported sizes by this block size before printing (-B/--block-size).",
    )

    total: bool = Field(
        default=False,
        description="If true, produce a grand total across all paths (-c/--total).",
    )

    human_readable: bool = Field(
        default=False,
        description="If true, print sizes in human-readable form (e.g. 1K, 234M, 2G).",
    )

    inodes: bool = Field(
        default=False,
        description="If true, report inode usage instead of block usage.",
    )

    separate_dirs: bool = Field(
        default=False,
        description=(
            "If true, for directories do not include the size of subdirectories (-S/--separate-dirs)."
        ),
    )

    summarize: bool = Field(
        default=False,
        description="If true, display only a total for each path (-s/--summarize).",
    )

    verbose: bool = Field(
        default=False,
        description="If true, show the gufi_query being executed (-V).",
    )

    model_config = {
        "extra": "forbid",
    }

    @field_validator("paths")
    @classmethod
    def paths_non_empty(cls, value: list[str]) -> list[str]:
        cleaned = [p.strip() for p in value if p.strip()]
        if not cleaned:
            raise ValueError("paths must contain at least one non-empty path")
        return cleaned


args_du_dict = {
    "apparent_size": "--apparent-size",
    "block_size": "--block-size",
    "total": "-c",
    "human_readable": "-h",
    "inodes": "--inodes",
    "separate_dirs": "-S",
    "summarize": "-s",
    "verbose": "-V",
}


class GufiToolArgsStat(BaseModel):
    """GUFI equivalent of stat: Display file status."""

    files: list[str] = Field(
        default_factory=lambda: ["."],
        min_length=1,
        description=(
            "Files or directories to stat. Absolute paths preferred. "
            "Multiple entries are passed as separate positional arguments."
        ),
    )

    format: Optional[str] = Field(
        default=None,
        description=(
            "Optional printf-style format string (-c/--format). "
            "When set, output follows this format instead of the default."
        ),
    )

    terse: bool = Field(
        default=False,
        description="If true, print information in terse form (-t/--terse).",
    )

    verbose: bool = Field(
        default=False,
        description="If true, show the actual command being run (-V).",
    )

    model_config = {
        "extra": "forbid",
    }

    @field_validator("files")
    @classmethod
    def files_non_empty(cls, value: list[str]) -> list[str]:
        cleaned = [f.strip() for f in value if f.strip()]
        if not cleaned:
            raise ValueError("files must contain at least one non-empty path")
        return cleaned


args_stat_dict = {
    "format": "-c",
    "terse": "-t",
    "verbose": "-V",
}


class GufiToolArgsStats(BaseModel):
    """GUFI equivalent of gufi_stats: Index-level filesystem statistics."""

    statistic: GufiStatsStatistic = Field(
        default=GufiStatsStatistic.TOTAL_FILECOUNT,
        description="Which statistic to compute (positional argument to gufi_stats).",
    )

    path: str = Field(
        default=".",
        min_length=1,
        description=(
            "Path to obtain the statistic from. Resolved internally against the GUFI index."
        ),
    )

    recursive: bool = Field(
        default=False,
        description=(
            "If true, run recursively for statistics that support it (-r). "
            "Mutually exclusive with cumulative."
        ),
    )

    cumulative: bool = Field(
        default=False,
        description=(
            "If true, return cumulative values for statistics that support it (-c). "
            "Mutually exclusive with recursive."
        ),
    )

    order: Optional[str] = Field(
        default=None,
        description="Sort output when the chosen statistic supports ordering (--order).",
    )

    num_results: Optional[int] = Field(
        default=None,
        ge=1,
        description="Return only the first n results when applicable (--num-results).",
    )

    uid: Optional[str] = Field(
        default=None,
        description="Restrict results to this user name or numeric UID (--uid/--user).",
    )

    reftime: Optional[str] = Field(
        default=None,
        description="Reference time used when comparing timestamps (--reftime).",
    )

    delim: str = Field(
        default="\t",
        min_length=1,
        description="Delimiter separating output columns (--delim).",
    )

    skip_file: Optional[str] = Field(
        default=None,
        description="Basenames of directories to skip, listed one per line in this file (--skip-file).",
    )

    verbose: bool = Field(
        default=False,
        description="If true, show the gufi_query being executed (-V).",
    )

    model_config = {
        "extra": "forbid",
    }

    @model_validator(mode="after")
    def recursive_and_cumulative_exclusive(self) -> "GufiToolArgsStats":
        if self.recursive and self.cumulative:
            raise ValueError("recursive and cumulative cannot both be true (-r and -c are mutually exclusive)")
        return self


args_stats_dict = {
    "recursive": "-r",
    "cumulative": "-c",
    "order": "--order",
    "num_results": "--num-results",
    "uid": "--uid",
    "reftime": "--reftime",
    "skip_file": "--skip-file",
    "verbose": "-V",
}


class GufiToolArgsGetfattr(BaseModel):
    """GUFI equivalent of getfattr: Read extended attributes from indexed paths."""

    paths: list[str] = Field(
        default_factory=lambda: ["."],
        min_length=1,
        description="Paths to read extended attributes from (one or more positional arguments).",
    )

    name: Optional[str] = Field(
        default=None,
        description="Dump the value of this named extended attribute only (-n/--name).",
    )

    dump: bool = Field(
        default=False,
        description="If true, dump all extended attributes on each path (-d/--dump).",
    )

    match: str = Field(
        default=r"^user\.",
        description=(
            "Regular expression limiting which attribute names are included (-m/--match). "
            "Default is '^user\\.' (user namespace). Use '-' to include all attributes."
        ),
    )

    only_values: bool = Field(
        default=False,
        description="If true, dump raw extended attribute value(s) without encoding (--only-values).",
    )

    recursive: bool = Field(
        default=False,
        description="If true, list attributes recursively for all files and directories (-R).",
    )

    delim: str = Field(
        default="\t",
        min_length=1,
        description="Delimiter separating output columns (--delim).",
    )

    skip_file: Optional[str] = Field(
        default=None,
        description="Basenames of directories to skip, listed one per line in this file (--skip-file).",
    )

    verbose: bool = Field(
        default=False,
        description="If true, show the gufi_query being executed (-V).",
    )

    model_config = {
        "extra": "forbid",
    }

    @field_validator("paths")
    @classmethod
    def paths_non_empty(cls, value: list[str]) -> list[str]:
        cleaned = [p.strip() for p in value if p.strip()]
        if not cleaned:
            raise ValueError("paths must contain at least one non-empty path")
        return cleaned

    @model_validator(mode="after")
    def name_or_dump_not_both(self) -> "GufiToolArgsGetfattr":
        if self.name and self.dump:
            raise ValueError("Specify either name or dump, not both (-n and -d together are ambiguous)")
        return self


args_getfattr_dict = {
    "name": "-n",
    "dump": "-d",
    "match": "-m",
    "only_values": "--only-values",
    "recursive": "-R",
    "skip_file": "--skip-file",
    "verbose": "-V",
}


class GufiToolArgsFind(BaseModel):
    """GUFI equivalent of find: Search indexed directory trees."""

    paths: list[str] = Field(
        default_factory=lambda: ["."],
        min_length=1,
        description="Starting paths for the search (trailing positional arguments to gufi_find).",
    )

    maxdepth: Optional[int] = Field(
        default=None,
        ge=0,
        description="Descend at most this many directory levels below each starting path (-maxdepth).",
    )

    mindepth: Optional[int] = Field(
        default=None,
        ge=0,
        description="Do not apply tests or actions above this depth (-mindepth).",
    )

    amin: Optional[int] = Field(
        default=None,
        description="File was last accessed n minutes ago (-amin).",
    )

    anewer: Optional[str] = Field(
        default=None,
        description="File was accessed more recently than this reference file was modified (-anewer).",
    )

    atime: Optional[int] = Field(
        default=None,
        description="File was last accessed n*24 hours ago (-atime).",
    )

    cmin: Optional[int] = Field(
        default=None,
        description="File status was last changed n minutes ago (-cmin).",
    )

    cnewer: Optional[str] = Field(
        default=None,
        description="File status was changed more recently than this reference file was modified (-cnewer).",
    )

    ctime: Optional[int] = Field(
        default=None,
        description="File status was last changed n*24 hours ago (-ctime).",
    )

    empty: bool = Field(
        default=False,
        description="If true, match empty regular files or directories (-empty).",
    )

    executable: bool = Field(
        default=False,
        description="If true, match executable files and searchable directories (-executable).",
    )

    false: bool = Field(
        default=False,
        description="If true, never match (-false).",
    )

    gid: Optional[int] = Field(
        default=None,
        ge=0,
        description="File numeric group ID must equal n (-gid).",
    )

    group: Optional[str] = Field(
        default=None,
        description="File belongs to group gname; numeric GID allowed (-group).",
    )

    iname: Optional[str] = Field(
        default=None,
        description="Base name matches pattern; case insensitive regex (-iname).",
    )

    inum: Optional[int] = Field(
        default=None,
        ge=0,
        description="File inode number (-inum). Prefer -samefile when comparing paths.",
    )

    iregex: Optional[str] = Field(
        default=None,
        description="Full path matches regex; case insensitive (-iregex).",
    )

    links: Optional[int] = Field(
        default=None,
        ge=0,
        description="File has exactly n hard links (-links).",
    )

    lname: Optional[str] = Field(
        default=None,
        description="Symbolic link contents match shell pattern (-lname).",
    )

    mmin: Optional[int] = Field(
        default=None,
        description="File data was modified n minutes ago (-mmin).",
    )

    mtime: Optional[int] = Field(
        default=None,
        description="File data was modified n*24 hours ago (-mtime).",
    )

    name: Optional[str] = Field(
        default=None,
        description="Base name matches shell pattern (-name).",
    )

    newer: Optional[str] = Field(
        default=None,
        description="File was modified more recently than reference file (-newer).",
    )

    path_pattern: Optional[str] = Field(
        default=None,
        description="Full path matches shell pattern (-path).",
    )

    perm: Optional[str] = Field(
        default=None,
        description="Permission bits as octal mode, optionally prefixed with - or / (-perm).",
    )

    readable: bool = Field(
        default=False,
        description="If true, match readable files (-readable).",
    )

    regex: Optional[str] = Field(
        default=None,
        description="Full path matches regular expression (-regex).",
    )

    samefile: Optional[str] = Field(
        default=None,
        description="File refers to the same inode as this path (-samefile).",
    )

    size: Optional[str] = Field(
        default=None,
        description=(
            "File size predicate (-size). For less-than searches use a leading sign "
            "(e.g. '-2k' as size='-2k' or '-size=-2k' style values per gufi_find docs)."
        ),
    )

    true: bool = Field(
        default=False,
        description="If true, always match (-true).",
    )

    file_type: Optional[list[Literal["b", "c", "d", "p", "f", "l", "s"]]] = Field(
        default=None,
        description=(
            "Restrict to file type(s): b block, c char, d directory, p fifo, "
            "f regular, l symlink, s socket (-type). Repeat flag for each type."
        ),
    )

    uid: Optional[int] = Field(
        default=None,
        ge=0,
        description="File numeric user ID must equal n (-uid).",
    )

    user: Optional[str] = Field(
        default=None,
        description="File is owned by user uname; numeric UID allowed (-user).",
    )

    writable: bool = Field(
        default=False,
        description="If true, match writable files (-writable).",
    )

    fprint: Optional[str] = Field(
        default=None,
        description="Write matching paths to this file (-fprint). Mutually exclusive with other actions.",
    )

    ls: bool = Field(
        default=False,
        description="List each match in ls -dils format (-ls). Mutually exclusive with other actions.",
    )

    print: bool = Field(
        default=False,
        description=(
            "Print full path of each match followed by newline (-print). "
            "When no other action is set, gufi_find behaves as if -print were given."
        ),
    )

    print0: bool = Field(
        default=False,
        description="Print full path of each match followed by NUL (-print0). Mutually exclusive with other actions.",
    )

    printf: Optional[str] = Field(
        default=None,
        description="Print each match using GNU find printf format (-printf). Mutually exclusive with other actions.",
    )

    num_results: Optional[int] = Field(
        default=None,
        ge=1,
        description="Return only the first n matches (--num-results).",
    )

    smallest: bool = Field(
        default=False,
        description="Sort output by size ascending (--smallest). Mutually exclusive with largest.",
    )

    largest: bool = Field(
        default=False,
        description="Sort output by size descending (--largest). Mutually exclusive with smallest.",
    )

    compress: bool = Field(
        default=False,
        description="If true, reduce memory use via zlib compression when available (--compress).",
    )

    delim: str = Field(
        default="\t",
        min_length=1,
        description="Delimiter separating output columns (--delim).",
    )

    skip_file: Optional[str] = Field(
        default=None,
        description="Basenames of directories to skip, listed one per line in this file (--skip-file).",
    )

    verbose: bool = Field(
        default=False,
        description="If true, show the gufi_query being executed (-V).",
    )

    model_config = {
        "extra": "forbid",
    }

    @field_validator("paths")
    @classmethod
    def paths_non_empty(cls, value: list[str]) -> list[str]:
        cleaned = [p.strip() for p in value if p.strip()]
        if not cleaned:
            raise ValueError("paths must contain at least one non-empty path")
        return cleaned

    @field_validator("perm")
    @classmethod
    def perm_looks_like_mode(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        mode = value.lstrip("-/")
        if not re.fullmatch(r"[0-7]{3,4}", mode):
            raise ValueError("perm must be octal digits, optionally prefixed with - or /")
        return value

    @model_validator(mode="after")
    def depth_order_sane(self) -> "GufiToolArgsFind":
        if (
            self.maxdepth is not None
            and self.mindepth is not None
            and self.mindepth > self.maxdepth
        ):
            raise ValueError("mindepth cannot be greater than maxdepth")
        return self

    @model_validator(mode="after")
    def sort_direction_exclusive(self) -> "GufiToolArgsFind":
        if self.smallest and self.largest:
            raise ValueError("smallest and largest cannot both be true")
        return self

    @model_validator(mode="after")
    def single_find_action(self) -> "GufiToolArgsFind":
        actions: list[str] = []
        if self.fprint is not None:
            actions.append("fprint")
        if self.ls:
            actions.append("ls")
        if self.print:
            actions.append("print")
        if self.print0:
            actions.append("print0")
        if self.printf is not None:
            actions.append("printf")
        if len(actions) > 1:
            raise ValueError(
                f"choose one find action only; multiple set: {', '.join(actions)} "
                "(fprint, ls, print, print0, printf)"
            )
        return self

    @model_validator(mode="after")
    def false_and_true_exclusive(self) -> "GufiToolArgsFind":
        if self.false and self.true:
            raise ValueError("false and true cannot both be true")
        return self


args_find_dict = {
    "maxdepth": "-maxdepth",
    "mindepth": "-mindepth",
    "amin": "-amin",
    "anewer": "-anewer",
    "atime": "-atime",
    "cmin": "-cmin",
    "cnewer": "-cnewer",
    "ctime": "-ctime",
    "empty": "-empty",
    "executable": "-executable",
    "false": "-false",
    "gid": "-gid",
    "group": "-group",
    "iname": "-iname",
    "inum": "-inum",
    "iregex": "-iregex",
    "links": "-links",
    "lname": "-lname",
    "mmin": "-mmin",
    "mtime": "-mtime",
    "name": "-name",
    "newer": "-newer",
    "path_pattern": "-path",
    "perm": "-perm",
    "readable": "-readable",
    "regex": "-regex",
    "samefile": "-samefile",
    "size": "-size",
    "true": "-true",
    "file_type": "-type",
    "uid": "-uid",
    "user": "-user",
    "writable": "-writable",
    "fprint": "-fprint",
    "ls": "-ls",
    "print": "-print",
    "print0": "-print0",
    "printf": "-printf",
    "num_results": "--num-results",
    "smallest": "--smallest",
    "largest": "--largest",
    "compress": "--compress",
    "skip_file": "--skip-file",
    "verbose": "-V",
}

def is_valid_sql_query(sql_query: str, dialect: str = "sqlite") -> bool:
    try:
        parse_one(sql_query, read=dialect)
        return True
    except ParseError as e:
        print(f"SQL Grammar Error: {str(e)}", file=sys.stderr)
        return False

def table_exists(index: str, table_name: str) -> bool:
    ''' Confirm existence of a table based on an index and table name '''

    conn = sqlite3.connect(f'{GUFI_INDEX_ROOT}{index}/db.db')
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
            AND name = ?
        """, (table_name,))

        return cursor.fetchone() is not None

    finally:
        conn.close()

def get_columns_from_sqlin(sqlin) -> list[str]:
    ''' Separate out column names into a list '''
    col_string = re.match(r'(?i)SELECT\s+(.+?)\s+FROM', sqlin, flags=re.IGNORECASE | re.DOTALL).group(1)
    return [col.strip() for col in col_string.split(',')]

def resolve_local_index(index: str) -> str:
    ''' resolve name of a local index to the full path '''

    if index not in get_gufi_indexes():
        raise RuntimeError("Error: Index provided not found at index root.")

    return f'{GUFI_INDEX_ROOT}{index}'

def resolve_remote_index(index: str) -> str:
    ''' resolve name of a remote index to the full path '''

    # SELECT * FROM paths WHERE path ~ '^/[^/]+/?$';

    return f'{index}'

def get_gufi_indexes() -> list[str]:
    index_root = Path(GUFI_INDEX_ROOT).resolve()
    if not index_root.exists():
        raise RuntimeError("Error: GUFI index root does not exist.")
    indexes = []

    # Confirm path is a directory with other dirs inside
    if index_root.is_dir():
        for entry in index_root.iterdir():
            if not entry.is_dir():
                continue
            indexes.append(entry.name)

    return indexes

def execute_sql(sqlline: str, gufi_vt: bool, plain_index: str = None) -> list[Any]:
    ''' Wrapper to execute SQL queries '''

    # GUFI_VT uses memory and operates through the function
    if gufi_vt:
        conn = sqlite3.connect(':memory:')
    # Pulling schema needs a path to an index
    else:
        # Specific path / table to connect to
        if plain_index:
            index = f"{plain_index}" + "/db.db"
        else:
            index = f"{GUFI_INDEX_ROOT}" + "/db.db"
        conn = sqlite3.connect(index)

    # Handle SQLITE3 errors
    try:
        # Using gufi_vt extension
        if gufi_vt:
            conn.enable_load_extension(True)
            cursor = conn.cursor()
            conn.load_extension(GUFIVTLIB)
            conn.enable_load_extension(False)
        # Plain SQL query
        else:
            cursor = conn.cursor()

        print(sqlline, file=sys.stderr)

        # Call GUFI_VT
        cursor.execute(sqlline)

        return cursor.fetchall()

    except sqlite3.Error as e:
        print(f"An SQLite error occurred: {e}",file=sys.stderr)
        conn.close()
        return [f"sql error:", f"{str(e)}"]

    finally:
        conn.close()

def has_treesummary(index: str) -> bool:

    if index == GUFI_INDEX_ROOT:
        response = execute_sql("SELECT name, type FROM sqlite_master WHERE sql IS NOT NULL", False)

    elif index not in get_gufi_indexes():
        raise RuntimeError("Error: Index provided not found at index root.")

    else:
        response = execute_sql("SELECT name FROM sqlite_master WHERE sql IS NOT NULL AND name == 'treesummary'", False, resolve_local_index(index))

    if len(response) == 0:
        return False
    else:
        return True

def subpath_exists(path: str) -> bool:
    ''' Confirm a subpath exists within gufi index root '''
    result = subprocess.run(["gufi_ls", f"{path}"], capture_output=True, text=True)
    if result.stdout:
        return True
    else:
        return False

    raw = str(index).strip().rstrip("/")
    if os.path.isabs(raw):
        return os.path.isdir(raw)

    if raw in get_gufi_indexes():
        return True

def load_schema_hints(name: str, schema: list[list[str]]) -> dict:
    ''' Attach static hints to dynamically loaded schema from PRAGMA '''

    with open(SCHEMAFILE, "r") as f:
        hints = json.load(f)
    schema_hints = hints["tables"][name]["columns"]
    schema_comp = {}

    # For each column in the schema, attach hint to a dictionary entry
    for col in schema:
        # Fill type from file if blank
        if col[1] == '':
            schema_comp[col[0]] = {"type": schema_hints[col[0]]["type"], "hint": schema_hints[col[0]]["hint"]}
        else:
            schema_comp[col[0]] = {"type": col[1], "hint": schema_hints[col[0]]["hint"]}

    return schema_comp

def get_schema_hints(name: str) -> dict:
    with open(SCHEMAFILE, "r") as f:
        hints = json.load(f)
    return hints["tables"][name]["columns"]


def parse_tool_args(args: dict, tool_dict: dict) -> list:
    ''' Take a pydantic model and convert it into a list subprocess can run'''

    cmd = []

    # parse args from struct into cli command for subprocess
    agent_options = args.__dict__

    # For each option the agent submitted
    for option in agent_options:
        try:
            # Try to obtain the match from the CLI dict
            cli_option = tool_dict[option]
            # If the type is not a bool and the value is not none
            if type(agent_options[option]) is not bool and agent_options[option]:
                # Add the option and it's value
                cmd.append(cli_option)
                cmd.append(f"{agent_options[option]}")
            # If it is a bool, only append the option
            else:
                if agent_options[option]:
                    cmd.append(cli_option)
        # Simply skip key errors in case it is a positional argument
        except KeyError:
            continue
    return cmd