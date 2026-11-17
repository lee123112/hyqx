"""Measurement engine contract shared by all modalities."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EngineResult:
    # sample_id -> {"baseline": {...}, "candidate": {...}}
    measurements: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    # metric key -> measured_model / measured_rule / proxy / imported
    provenance: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    missing: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class BaseEngine(ABC):
    type: str = ""

    @abstractmethod
    def measure(self, samples: list[dict[str, Any]], config: dict[str, Any], *,
                seed: int = 42, strict: bool = True) -> EngineResult:
        """Return per-sample baseline/candidate measurements."""

    def name(self) -> str:
        return self.__class__.__name__
