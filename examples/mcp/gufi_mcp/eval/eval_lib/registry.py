"""Scenario registries (scenarios.single.yaml, scenarios.general.yaml) and index manifests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Mode = Literal["single", "general"]


class RegistryError(Exception):
    """A registry or manifest problem to report without a traceback."""


class Scenario(BaseModel):
    """One registry entry. Both registry files use exactly these keys."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    prompt: str = Field(min_length=1)
    tags: list[str]
    enabled: bool
    index: str = Field(min_length=1)
    parameters: dict[str, Any]
    ground_truth: Optional[dict[str, Any]]
    notes: str

    @field_validator("ground_truth")
    @classmethod
    def ground_truth_is_query(cls, value: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if value is not None and "index" in value:
            raise ValueError("ground_truth must not set index; it is taken from the scenario's index")
        return value


class IndexManifest(BaseModel):
    """The parts of gen_index.py's NAME.manifest.json that evaluation relies on."""

    model_config = ConfigDict(extra="allow")

    manifest_id: str
    index_name: str
    seed: int
    fixed_now: int
    path: Optional[Path] = None

    @property
    def fixed_now_iso(self) -> str:
        return datetime.fromtimestamp(self.fixed_now, timezone.utc).isoformat()


def load_manifest(path: Path) -> IndexManifest:
    try:
        data = json.loads(path.read_text())
    except OSError as e:
        raise RegistryError(f"Cannot read index manifest {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise RegistryError(f"Index manifest {path} is not valid JSON: {e}") from e
    try:
        return IndexManifest.model_validate({**data, "path": path})
    except ValidationError as e:
        raise RegistryError(f"Index manifest {path} is missing required fields:\n{e}") from e


def template_values(manifest: IndexManifest, parameters: dict[str, Any]) -> dict[str, Any]:
    """Values available to {placeholders} in prompts and ground-truth SQL."""
    now = datetime.fromtimestamp(manifest.fixed_now, timezone.utc)
    return {
        "fixed_now": manifest.fixed_now,
        "fixed_now_iso": manifest.fixed_now_iso,
        "fixed_now_date": now.date().isoformat(),
        "index": manifest.index_name,
        "parameters": SimpleNamespace(**parameters),
    }


def render(text: str, values: dict[str, Any], where: str) -> str:
    try:
        return text.format_map(values)
    except (KeyError, AttributeError, IndexError, ValueError) as e:
        raise RegistryError(f"{where}: cannot fill placeholder ({type(e).__name__}: {e})") from e


class RenderedScenario(BaseModel):
    """A scenario with placeholders filled in for one index manifest."""

    mode: Mode
    scenario: Scenario
    prompt: str
    ground_truth: Optional[dict[str, Any]]


def render_scenario(scenario: Scenario, mode: Mode, manifest: IndexManifest) -> RenderedScenario:
    values = template_values(manifest, scenario.parameters)
    where = f"scenario {scenario.id!r}"
    prompt = render(scenario.prompt, values, f"{where} prompt")
    ground_truth = None
    if scenario.ground_truth is not None:
        ground_truth = {"index": scenario.index}
        for key, value in scenario.ground_truth.items():
            if isinstance(value, str):
                value = render(value, values, f"{where} ground_truth.{key}")
            elif isinstance(value, list):
                value = [render(v, values, f"{where} ground_truth.{key}") if isinstance(v, str) else v for v in value]
            ground_truth[key] = value
    return RenderedScenario(mode=mode, scenario=scenario, prompt=prompt, ground_truth=ground_truth)


def load_registry(path: Path, mode: Mode) -> list[Scenario]:
    """Load and validate one registry file; single-query entries need ground truth, general entries must not have it."""
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as e:
        raise RegistryError(f"Cannot read registry {path}: {e}") from e
    except yaml.YAMLError as e:
        raise RegistryError(f"Registry {path} is not valid YAML: {e}") from e
    if not isinstance(data, list):
        raise RegistryError(f"Registry {path} must be a list of scenarios")

    scenarios: list[Scenario] = []
    errors: list[str] = []
    for i, item in enumerate(data):
        try:
            scenario = Scenario.model_validate(item)
        except ValidationError as e:
            name = item.get("id", f"#{i}") if isinstance(item, dict) else f"#{i}"
            errors.append(f"{name}: {e}")
            continue
        if mode == "single" and scenario.ground_truth is None:
            errors.append(f"{scenario.id}: single-query scenarios need a ground_truth")
        if mode == "general" and scenario.ground_truth is not None:
            errors.append(f"{scenario.id}: general scenarios must have ground_truth: null")
        scenarios.append(scenario)

    ids = [s.id for s in scenarios]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        errors.append(f"duplicate scenario ids: {duplicates}")
    if errors:
        raise RegistryError(f"Registry {path} has errors:\n  " + "\n  ".join(errors))
    return scenarios


def validate_ground_truth(rendered: RenderedScenario) -> None:
    """Check rendered ground truth with the server's own model (needs the GUFI environment)."""
    from gufi_mcp_util import GufiAggregateQuery

    try:
        GufiAggregateQuery.model_validate(rendered.ground_truth)
    except ValidationError as e:
        raise RegistryError(f"scenario {rendered.scenario.id!r}: ground_truth is not a valid aggregate query:\n{e}") from e


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
