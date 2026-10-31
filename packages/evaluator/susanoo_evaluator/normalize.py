"""Reviewed normalizers for simulator/reference comparison [Spec §14 › F02].

Only three kinds exist: timestamp tolerance, known generated identifiers and explicitly unordered
collections. Each must name a reviewer and a rationale. Normalizers apply to value comparison at one
path; they never touch status codes, access denials, record counts (duplicates), lifecycle state or
missing side effects, which are compared raw by the caller.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .dsl import PATH_PATTERN

ROOT_FIELDS_NEVER_NORMALIZED = frozenset({"status", "code", "error", "permission", "deleted", "lifecycle"})


class Normalizer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["timestamp_tolerance", "generated_id", "unordered_collection"]
    path: str = Field(pattern=PATH_PATTERN)
    tolerance_seconds: int = Field(default=0, ge=0, le=3600)
    reviewed_by: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def meaningful_fields_stay_raw(self) -> Normalizer:
        leaf = re.split(r"[.\[]", self.path)[-1].rstrip("]")
        if leaf in ROOT_FIELDS_NEVER_NORMALIZED:
            raise ValueError(f"{self.path} carries meaning that must never be normalized away")
        return self


class Difference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    simulator: Any = None
    reference: Any = None
    kind: Literal["value", "missing_in_simulator", "missing_in_reference", "length"]


_MISSING = object()


def _join(base: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{base}[{key}]"
    return f"{base}.{key}" if base else key


def _pattern(path: str) -> re.Pattern[str]:
    """Normalizer paths match at any list index: ``promises.created_at`` matches ``promises[3].created_at``."""
    escaped = re.escape(re.sub(r"\[[0-9]+\]", "", path))
    return re.compile("^" + escaped.replace(r"\.", r"(?:\[[0-9]+\])*\.") + r"(?:\[[0-9]+\])*$")


class NormalizerSet:
    def __init__(self, normalizers: list[Normalizer], id_map: dict[str, str] | None = None):
        self.normalizers = normalizers
        self.id_map = id_map or {}
        self._compiled = [(n, _pattern(n.path)) for n in normalizers]
        self.applied: set[str] = set()

    def _match(self, path: str, kind: str) -> Normalizer | None:
        for normalizer, pattern in self._compiled:
            if normalizer.kind == kind and pattern.match(path):
                return normalizer
        return None

    def logical(self, value: Any) -> Any:
        return self.id_map.get(value, value) if isinstance(value, str) else value

    def diff(self, simulator: Any, reference: Any, path: str = "") -> list[Difference]:
        if simulator is _MISSING:
            return [Difference(path=path, reference=reference, kind="missing_in_simulator")]
        if reference is _MISSING:
            return [Difference(path=path, simulator=simulator, kind="missing_in_reference")]
        if isinstance(simulator, dict) and isinstance(reference, dict):
            out: list[Difference] = []
            for key in sorted(set(simulator) | set(reference)):
                out += self.diff(simulator.get(key, _MISSING), reference.get(key, _MISSING), _join(path, key))
            return out
        if isinstance(simulator, list) and isinstance(reference, list):
            if len(simulator) != len(reference):
                # Length differences are never normalized: they are duplicates or missing records.
                return [Difference(path=path, simulator=len(simulator), reference=len(reference), kind="length")]
            if self._match(path, "unordered_collection"):
                self.applied.add(path)
                simulator = sorted(simulator, key=self._sort_key)
                reference = sorted(reference, key=self._sort_key)
            out = []
            for index, (left, right) in enumerate(zip(simulator, reference, strict=True)):
                out += self.diff(left, right, _join(path, index))
            return out
        if self._match(path, "generated_id") and isinstance(simulator, str) and isinstance(reference, str):
            self.applied.add(path)
            if self.logical(simulator) == self.logical(reference):
                return []
        tolerance = self._match(path, "timestamp_tolerance")
        if tolerance and _close_in_time(simulator, reference, tolerance.tolerance_seconds):
            self.applied.add(path)
            return []
        if type(simulator) is not type(reference) or simulator != reference:
            return [Difference(path=path, simulator=simulator, reference=reference, kind="value")]
        return []

    def _sort_key(self, item: Any) -> str:
        import json

        if isinstance(item, dict):
            item = {k: self.logical(v) for k, v in item.items()}
        return json.dumps(item, sort_keys=True, default=str)


def _close_in_time(left: Any, right: Any, tolerance: int) -> bool:
    if not (isinstance(left, str) and isinstance(right, str)):
        return False
    try:
        a = datetime.fromisoformat(left.replace("Z", "+00:00"))
        b = datetime.fromisoformat(right.replace("Z", "+00:00"))
    except ValueError:
        return False
    if a.tzinfo is None or b.tzinfo is None:
        return False
    return abs((a - b).total_seconds()) <= tolerance
