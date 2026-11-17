"""Lineage JSONL writer.  Only logical container paths are persisted."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .ids import now
from .security import logical_path, sanitize_text

SCHEMA_VERSION = "mty-lineage-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LineageWriter:
    def __init__(self, path: Path, unique_id: str) -> None:
        self.path = path
        self.unique_id = unique_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        item = {
            "schemaVersion": SCHEMA_VERSION,
            "uniqueId": self.unique_id,
            "time": now(),
            "kind": kind,
        }
        for key, value in fields.items():
            if isinstance(value, Path):
                value = logical_path(value, self.unique_id)
            elif isinstance(value, str):
                value = sanitize_text(value)
            item[key] = value
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        return item

    def input_file(self, path: Path, *, source: str = "upload") -> None:
        self.record("input_file", path=path, source=source,
                    size=path.stat().st_size if path.exists() else 0,
                    sha256=sha256_file(path) if path.is_file() else "")

    def output(self, path: Path, *, kind: str = "artifact") -> None:
        self.record("output", path=path, artifactKind=kind,
                    size=path.stat().st_size if path.exists() else 0,
                    sha256=sha256_file(path) if path.is_file() else "")

    def verdict(self, *, outcome: str, accepted: bool | None, metrics: dict[str, Any] | None = None) -> None:
        self.record("verdict", outcome=outcome, accepted=accepted, metrics=metrics or {})

    def finalize(self) -> None:
        self.record("complete", file=self.path.name)
