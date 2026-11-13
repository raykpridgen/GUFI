"""Ablation switches for the GUFI MCP server (ablation_design.md section 6).

Each factor turns one part of the server on, off, or to a sparser level so the eval
harness can measure what that part contributes. The values come from environment
variables read once at import; every default is today's full server.

  GUFI_ABLATION_SUBMISSION  pydantic | plain     aggregate tool takes a structured query or a gufi_query command
  GUFI_ABLATION_SCHEMA      none | naive | hints  gufi_schemas absent, names and types only, or with hints
  GUFI_ABLATION_PRIMER      1 | 0                 server instructions on or off
  GUFI_ABLATION_FUNCTIONS   1 | 0                 gufi_functions / gufi_function tools and column function tags
  GUFI_ABLATION_PATH        server | rpath        synthetic path columns, or raw rpath(sname, sroll[, name])
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping

ENV_PREFIX = "GUFI_ABLATION_"

CHOICES: dict[str, tuple[str, ...]] = {
    "submission": ("pydantic", "plain"),
    "schema": ("hints", "naive", "none"),
    "primer": ("1", "0"),
    "functions": ("1", "0"),
    "path": ("server", "rpath"),
}

# Short codes for run ids, in field order: e.g. full is "py-hints-p1-f1-server".
_ID_CODES = {"primer": "p", "functions": "f"}


class AblationError(ValueError):
    """An ablation setting has a value outside its choices."""


@dataclass(frozen=True)
class AblationConfig:
    submission: str = "pydantic"
    schema: str = "hints"
    primer: bool = True
    functions: bool = True
    path: str = "server"

    def __post_init__(self) -> None:
        for name in ("submission", "schema", "path"):
            value = getattr(self, name)
            if value not in CHOICES[name]:
                raise AblationError(f"{ENV_PREFIX}{name.upper()}={value!r}; choose one of {', '.join(CHOICES[name])}")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "AblationConfig":
        """Build from {factor: value}; booleans accept 1/0, true/false, on/off. Unknown keys are an error."""
        unknown = set(values) - {f.name for f in fields(cls)}
        if unknown:
            raise AblationError(f"unknown ablation factor(s): {sorted(unknown)}; known: {list(CHOICES)}")
        kwargs: dict[str, Any] = {}
        for name, value in values.items():
            if name in ("primer", "functions"):
                kwargs[name] = _as_bool(name, value)
            else:
                kwargs[name] = str(value).strip().lower()
        return cls(**kwargs)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "AblationConfig":
        values = {name: environ[ENV_PREFIX + name.upper()] for name in CHOICES
                  if environ.get(ENV_PREFIX + name.upper(), "").strip()}
        return cls.from_mapping(values)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def env(self) -> dict[str, str]:
        """Environment variables that reproduce this config in a server subprocess."""
        out = {}
        for name, value in self.as_dict().items():
            out[ENV_PREFIX + name.upper()] = ("1" if value else "0") if isinstance(value, bool) else value
        return out

    @property
    def id(self) -> str:
        parts = []
        for name, value in self.as_dict().items():
            if isinstance(value, bool):
                parts.append(f"{_ID_CODES[name]}{int(value)}")
            else:
                parts.append("py" if value == "pydantic" else value)
        return "-".join(parts)

    @property
    def is_full(self) -> bool:
        return self == AblationConfig()

    def expected_tools(self) -> list[str]:
        """Tool names a server built with this config registers, in registration order."""
        tools = ["aggregate_sql_query", "gufi_indexes"]
        if self.schema != "none":
            tools.append("gufi_schemas")
        if self.functions:
            tools += ["gufi_functions", "gufi_function"]
        return tools


FULL = AblationConfig()


def _as_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "on", "yes"):
        return True
    if text in ("0", "false", "off", "no"):
        return False
    raise AblationError(f"{ENV_PREFIX}{name.upper()}={value!r}; choose 1 or 0")


CONFIG = AblationConfig.from_env()
