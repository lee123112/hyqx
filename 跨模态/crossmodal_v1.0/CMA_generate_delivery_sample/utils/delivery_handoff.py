"""Explicit, digest-bound delivery archive and subsequent review evidence.

This local module is shipped with each of M23--M26. It never selects a ZIP by
filename order and never promotes pending material to deliverable.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import zipfile

FORMAT = "crossmodal-delivery-handoff-v1"
KEY = "delivery_handoff"
ARCHIVE_FORMAT = "crossmodal-delivery-package-v1"
REVIEW_ROLES = {24: {"main", "quality_report", "sampling_conclusion", "sample_quarantine",
                     "pending_manual", "origin_quarantine", "disputes", "rejects"},
                25: {"main", "review_report", "review_conclusions", "sample_quarantine",
                     "pending_manual", "origin_quarantine", "rejects", "review_tasks"}}


def safe_path(root, value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or "\x00" in value or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("delivery handoff contains unsafe relative path")
    relative = PurePosixPath(value)
    base = Path(root).resolve()
    target = (base / Path(*relative.parts)).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError("delivery handoff path escapes input root") from exc
    return target


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path_identity(path):
    """Compare validated files using the host's resolved path/case semantics."""
    return os.path.normcase(str(Path(path).resolve()))


def _spec(root, path, role):
    path = Path(path)
    return {"path": path.relative_to(Path(root)).as_posix(), "role": role,
            "sha256": _sha(path)}


def _checked(root, spec):
    if not isinstance(spec, dict):
        raise ValueError("delivery handoff requires one unambiguous file reference")
    path = safe_path(root, spec.get("path"))
    digest = spec.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("delivery handoff requires sha256")
    if not path.is_file() or _sha(path) != digest:
        raise ValueError("delivery handoff file missing or sha256 mismatch")
    return path


def archive_manifest(path):
    """Validate all member names before reading the declared native manifest."""
    try:
        return _archive_manifest(path)
    except (zipfile.BadZipFile, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid native M23 delivery archive") from exc


def _archive_manifest(path):
    with zipfile.ZipFile(path) as archive:
        names = set()
        portable_names = set()
        infos = archive.infolist()
        if len(infos) > 50000 or sum(item.file_size for item in infos) > 8 * 1024 ** 3:
            raise ValueError("delivery archive exceeds extraction limits")
        for info in infos:
            name = info.filename.rstrip("/")
            if not name:
                continue
            safe_path(Path(path).parent, name)
            # A portable delivery archive must not overwrite a prior member
            # when unpacked on Windows, even if it was authored on Linux.
            portable_name = name.casefold()
            if portable_name in portable_names or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("delivery archive has duplicate or symbolic-link members")
            names.add(name)
            portable_names.add(portable_name)
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != ARCHIVE_FORMAT:
            raise ValueError("delivery handoff archive is not a native M23 package")
        entry = manifest.get("entrypoint")
        safe_path(Path(path).parent, entry)
        if entry not in names or "version.json" not in names:
            raise ValueError("delivery archive is missing declared entry/version")
        return manifest


def create(root, archive):
    archive_manifest(archive)
    return {"format": FORMAT, "archive": _spec(root, archive, "delivery_archive"),
            "reviews": []}


def load(root):
    manifest_path = Path(root) / "manifest.json"
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("delivery handoff manifest must be an object")
    if KEY not in manifest:
        return None
    block = manifest[KEY]
    if not isinstance(block, dict) or block.get("format") != FORMAT:
        raise ValueError("invalid delivery handoff declaration")
    archive = _checked(root, block.get("archive"))
    if block["archive"].get("role") != "delivery_archive":
        raise ValueError("delivery handoff archive role is invalid")
    archive_manifest(archive)
    reviews = block.get("reviews")
    if not isinstance(reviews, list):
        raise ValueError("delivery handoff reviews must be an explicit list")
    paths = {_path_identity(archive)}
    for step in reviews:
        if not isinstance(step, dict) or step.get("operator_number") not in REVIEW_ROLES:
            raise ValueError("unsupported delivery review stage")
        artifacts = step.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("delivery review artifacts must be an explicit list")
        roles = set()
        for item in artifacts:
            checked_path = _checked(root, item)
            role, path = item.get("role"), _path_identity(checked_path)
            if role not in REVIEW_ROLES[step["operator_number"]] or role in roles or path in paths:
                raise ValueError("ambiguous delivery review evidence")
            roles.add(role)
            paths.add(path)
        required = {"main", "quality_report", "sampling_conclusion"} if step["operator_number"] == 24 \
            else {"main", "review_report"}
        if not required.issubset(roles):
            raise ValueError("delivery review evidence is incomplete")
    return copy.deepcopy(block)


def declared_paths(manifest):
    block = manifest.get(KEY) or {}
    result = [block["archive"]["path"]] if block else []
    for step in block.get("reviews", []):
        result.extend(item["path"] for item in step["artifacts"])
    return list(dict.fromkeys(result))


def propagate(source_root, run_root, manifest, stage):
    block = load(source_root)
    if block is None:
        return
    run_root = Path(run_root)
    archive = _checked(source_root, block["archive"])
    destination = run_root / "delivery_handoff" / "archive.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, destination)
    block["archive"] = _spec(run_root, destination, "delivery_archive")
    for step in block["reviews"]:
        for item in step["artifacts"]:
            source = _checked(source_root, item)
            target = safe_path(run_root, item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    current = []
    folder = run_root / "delivery_handoff" / ("review_%03d_m%d" % (len(block["reviews"]) + 1, stage))
    for item in manifest.get("artifacts", []):
        if item.get("role") not in REVIEW_ROLES[stage]:
            continue
        source = safe_path(run_root, item["path"])
        if not source.is_file():
            continue
        target = folder / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        current.append(_spec(run_root, target, item["role"]))
    block["reviews"].append({"operator_number": stage, "artifacts": current})
    manifest[KEY] = block


def source_quality_report(root, destination):
    block = load(root)
    if block is None:
        return None
    archive = _checked(root, block["archive"])
    member = "materials/quality_report.json"
    native = archive_manifest(archive)
    if not any(item.get("path") == member for item in native.get("files", [])):
        return None
    target = Path(destination) / "delivery_handoff" / "source_quality_report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        target.write_bytes(bundle.read(member))
    return target


def _rows(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("delivery review row must be an object")
            rows.append(row)
    return rows


def review_effects(root, block):
    """Return current records and negative gates; never upgrade the native gate."""
    records, excluded, blockers = None, set(), []
    for step in block["reviews"]:
        stage = step["operator_number"]
        roles = {item["role"]: _checked(root, item) for item in step["artifacts"]}
        current = {}
        for row in _rows(roles["main"]):
            identifier = row.get("sample_id")
            if not isinstance(identifier, str) or not identifier.strip() or identifier in current:
                raise ValueError("ambiguous delivery review sample identifier")
            current[identifier] = row
        records = current
        for role in ("sample_quarantine", "origin_quarantine", "pending_manual", "disputes"):
            for row in _rows(roles[role]) if role in roles else []:
                identifier = row.get("sample_id")
                if isinstance(identifier, str) and identifier.strip():
                    excluded.add(identifier.strip())
                if role == "pending_manual" or (role == "disputes" and row.get("review_required") is not False):
                    blockers.append("m%d_%s" % (stage, role))
        if stage == 24:
            report = json.loads(roles["quality_report"].read_text(encoding="utf-8"))
            conclusion = json.loads(roles["sampling_conclusion"].read_text(encoding="utf-8"))
            if ((report.get("writeback") or {}).get("completed") is not True
                    or (conclusion.get("writeback") or {}).get("completed") is not True):
                blockers.append("m24_writeback_incomplete")
        else:
            report = json.loads(roles["review_report"].read_text(encoding="utf-8"))
            queue = (report.get("review") or {}).get("queue") or {}
            if (report.get("status") in ("waiting_manual", "failed", "degraded")
                    or queue.get("concluded_pending", 0) or queue.get("concluded_uncertain", 0)):
                blockers.append("m25_review_incomplete")
    return {"records": records, "excluded_ids": excluded, "blockers": sorted(set(blockers))}
