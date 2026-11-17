"""Carry only explicitly bound quality and quarantine artifacts from this input package."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

FORMAT = "crossmodal-material-handoff-v1"
QUARANTINES = ("origin_quarantine", "sample_quarantine")
ROLES = {"M20": ("quality_report", *QUARANTINES), "M21": QUARANTINES}


def _manifest(root):
    path = root / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}


def _digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _file(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("material_handoff: invalid material path")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
        raise ValueError("material_handoff: invalid material path")
    target = (root / relative).resolve()
    target.relative_to(root.resolve())
    if not target.is_file():
        raise ValueError("material_handoff: missing material %s" % relative)
    return target


def _validate_items(root, items, *, require_digest):
    if not isinstance(items, list):
        raise ValueError("material_handoff: invalid items")
    result = []
    seen = set()
    paths = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("material_handoff: invalid binding")
        stage, role = item.get("stage"), item.get("role")
        if stage not in ROLES or role not in ROLES[stage] or (stage, role) in seen:
            raise ValueError("material_handoff: duplicate or invalid stage/role")
        relative = item.get("path")
        target = _file(root, relative)
        if target in paths:
            raise ValueError("material_handoff: reused material path")
        digest = _digest(target)
        expected = item.get("sha256")
        if (require_digest or "sha256" in item) and (
                not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or digest != expected):
            raise ValueError("material_handoff: SHA-256 mismatch for %s" % relative)
        result.append({"stage": stage, "role": role, "path": relative, "sha256": digest})
        seen.add((stage, role))
        paths.add(target)
    return result


def read_bound(root):
    """An invalid declaration must never fall back to heuristic material discovery."""
    root = Path(root)
    manifest = _manifest(root)
    if "material_handoff" not in manifest:
        return []
    handoff = manifest["material_handoff"]
    if not isinstance(handoff, dict) or handoff.get("format") != FORMAT:
        raise ValueError("material_handoff: invalid format")
    return _validate_items(root, handoff.get("items"), require_digest=True)


def _write(path, writer):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as sink:
            writer(sink)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def forward(input_root, run_root, local_quarantines=None):
    input_root, run_root = Path(input_root), Path(run_root)
    if local_quarantines is None:
        items = read_bound(input_root)
    else:
        # M21 consumes M20's native artifact roles, before any handoff exists.
        source = _manifest(input_root)
        bound = []
        for artifact in source.get("artifacts", []):
            if isinstance(artifact, dict) and artifact.get("role") in ROLES["M20"]:
                bound.append(dict(artifact, stage="M20"))
        items = _validate_items(input_root, bound, require_digest=False)
    sources = [(input_root, item) for item in items]
    if local_quarantines is not None:
        local = [{"stage":"M21", "role":role, "path":relative}
                 for role, relative in local_quarantines.items()]
        sources.extend((run_root, item) for item in _validate_items(
            run_root, local, require_digest=False))
    if not sources:
        return
    output = []
    for base, item in sources:
        stage, role = item["stage"], item["role"]
        suffix = ".json" if role == "quality_report" else ".jsonl"
        relative = "material_handoff/%s/%s%s" % (stage, role, suffix)
        original = _file(base, item["path"])
        def copy_verified(sink):
            digest = hashlib.sha256()
            with original.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    sink.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != item["sha256"]:
                raise ValueError("material_handoff: material changed during copy")
        _write(run_root / relative, copy_verified)
        output.append(dict(item, path=relative))

    manifest = _manifest(run_root)
    manifest["material_handoff"] = {"format": FORMAT, "items": output}
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    _write(run_root / "manifest.json", lambda sink: sink.write(encoded))
