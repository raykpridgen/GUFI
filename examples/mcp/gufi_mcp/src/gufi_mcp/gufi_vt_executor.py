"""
Module 5: gufi_vt execution engine.

Loads the gufi_vt SQLite extension once into a persistent in-memory connection
and runs compiled SQL from the QueryPlan compiler (Module 3).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from gufi_mcp.query_plan import QueryPlanPipeline


@dataclass
class ExecuteResult:
    success: bool
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float
    compiled_sql: list[str]
    error: str | None = None


def resolve_gufi_vt_lib(
    vt_lib: Path | None = None,
    *,
    server_prefix: Path | None = None,
) -> Path:
    """Resolve gufi_vt.so from explicit path, env settings, or server_prefix/lib."""
    if vt_lib is not None:
        path = Path(vt_lib)
        if path.is_file():
            return path.resolve()
        raise FileNotFoundError(f"gufi_vt extension not found: {path}")

    try:
        from gufi_util import get_settings

        settings = get_settings()
        if settings.gufi_vt_lib and settings.gufi_vt_lib.is_file():
            return settings.gufi_vt_lib.resolve()
        prefix = server_prefix or settings.server_prefix
    except (ImportError, ValueError):
        prefix = server_prefix

    if prefix is not None:
        candidate = Path(prefix) / "lib" / "gufi_vt.so"
        if candidate.is_file():
            return candidate.resolve()

    raise FileNotFoundError(
        "gufi_vt extension not found. Set GUFIVTLIB or GUFI_SERVER_PREFIX/lib/gufi_vt.so."
    )


class GufiVtExecutor:
    """Persistent sqlite3 connection with gufi_vt loaded (§5.7 lifecycle)."""

    def __init__(self, vt_lib: Path | str) -> None:
        self._vt_lib = Path(vt_lib)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def connect(self) -> None:
        if self._conn is not None:
            return
        if not self._vt_lib.is_file():
            raise FileNotFoundError(f"gufi_vt extension not found: {self._vt_lib}")

        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        conn.load_extension(str(self._vt_lib))
        conn.enable_load_extension(False)
        self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def execute_sql(
        self,
        statements: list[str],
        *,
        row_limit: int = 0,
    ) -> ExecuteResult:
        """Run a compiled SQL script; return rows from the last result-set statement."""
        self.connect()
        assert self._conn is not None

        with self._lock:
            started = time.perf_counter()
            rows: list[tuple[Any, ...]] = []
            try:
                for statement in statements:
                    sql = statement.strip()
                    if not sql:
                        continue
                    cursor = self._conn.execute(sql)
                    if cursor.description is not None:
                        rows = cursor.fetchall()

                elapsed_ms = (time.perf_counter() - started) * 1000
                serialized = [list(row) for row in rows]
                truncated = row_limit > 0 and len(serialized) >= row_limit
                return ExecuteResult(
                    success=True,
                    rows=serialized,
                    row_count=len(serialized),
                    truncated=truncated,
                    elapsed_ms=elapsed_ms,
                    compiled_sql=list(statements),
                )
            except sqlite3.Error as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                return ExecuteResult(
                    success=False,
                    rows=[],
                    row_count=0,
                    truncated=False,
                    elapsed_ms=elapsed_ms,
                    compiled_sql=list(statements),
                    error=str(exc),
                )


_EXECUTOR: GufiVtExecutor | None = None
_EXECUTOR_VT_LIB: Path | None = None


def get_executor(vt_lib: Path | str | None = None) -> GufiVtExecutor:
    """Return the process-wide GufiVtExecutor singleton (loads extension once)."""
    global _EXECUTOR, _EXECUTOR_VT_LIB

    resolved = resolve_gufi_vt_lib(Path(vt_lib) if vt_lib is not None else None)
    if _EXECUTOR is None or _EXECUTOR_VT_LIB != resolved:
        if _EXECUTOR is not None:
            _EXECUTOR.close()
        _EXECUTOR = GufiVtExecutor(resolved)
        _EXECUTOR_VT_LIB = resolved
    return _EXECUTOR


def reset_executor() -> None:
    """Close and discard the singleton (for tests)."""
    global _EXECUTOR, _EXECUTOR_VT_LIB
    if _EXECUTOR is not None:
        _EXECUTOR.close()
    _EXECUTOR = None
    _EXECUTOR_VT_LIB = None


def execute_compiled_sql(
    compiled_sql: list[str],
    *,
    row_limit: int = 0,
    executor: GufiVtExecutor | None = None,
) -> ExecuteResult:
    """Execute pre-compiled SQL via the shared executor."""
    ex = executor or get_executor()
    return ex.execute_sql(compiled_sql, row_limit=row_limit)


def execute_plan(
    pipeline: QueryPlanPipeline,
    index_path: str,
    *,
    dry_run: bool = False,
    executor: GufiVtExecutor | None = None,
    table_suffix: str | None = None,
) -> ExecuteResult:
    """Compile a QueryPlan to gufi_vt SQL and optionally execute it."""
    compiled = pipeline.compile_sql(index_path, table_suffix=table_suffix)
    if dry_run:
        return ExecuteResult(
            success=True,
            rows=[],
            row_count=0,
            truncated=False,
            elapsed_ms=0.0,
            compiled_sql=compiled,
        )
    return execute_compiled_sql(
        compiled,
        row_limit=pipeline.plan.output.row_limit,
        executor=executor,
    )


def execute_result_to_dict(result: ExecuteResult) -> dict[str, Any]:
    return asdict(result)
