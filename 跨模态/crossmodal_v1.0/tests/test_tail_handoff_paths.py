"""File identity and portable ZIP-name uniqueness for all four local helpers."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ["CMA_package_aligned_corpus", "CMA_writeback_sampling_quality_report",
            "CMA_manage_manual_review_task", "CMA_generate_delivery_sample"]


@pytest.fixture(params=PACKAGES)
def handoff(request):
    spec = importlib.util.spec_from_file_location(
        "handoff_path_test_" + request.param,
        ROOT / request.param / "utils" / "delivery_handoff.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _archive(path, handoff, duplicates=False):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({
            "format": handoff.ARCHIVE_FORMAT, "entrypoint": "train/samples.jsonl"}))
        archive.writestr("train/samples.jsonl", "")
        archive.writestr("version.json", "{}")
        if duplicates:
            archive.writestr("materials/report.json", "{}")
            archive.writestr("materials/REPORT.JSON", "{}")


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive physical-file alias")
def test_review_roles_cannot_bind_two_case_aliases_of_one_file(handoff, tmp_path):
    _archive(tmp_path / "native.zip", handoff)
    (tmp_path / "main.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "report.json").write_text(
        '{"writeback":{"completed":true}}', encoding="utf-8")

    def spec(name, role):
        return {"path": name, "role": role,
                "sha256": hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}

    block = {"format": handoff.FORMAT,
             "archive": spec("native.zip", "delivery_archive"),
             "reviews": [{"operator_number": 24, "artifacts": [
                 spec("main.jsonl", "main"), spec("report.json", "quality_report"),
                 spec("REPORT.JSON", "sampling_conclusion")]}]}
    (tmp_path / "manifest.json").write_text(
        json.dumps({handoff.KEY: block}), encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous"):
        handoff.load(tmp_path)


def test_native_archive_rejects_case_colliding_member_names(handoff, tmp_path):
    archive = tmp_path / "native.zip"
    _archive(archive, handoff, duplicates=True)
    with pytest.raises(ValueError, match="duplicate"):
        handoff.archive_manifest(archive)
