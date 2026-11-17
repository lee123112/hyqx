"""v1.1 form1 输出包 manifest 契约测试。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from utils import form1_package


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "video_keyframes" / "run001"
    run.mkdir(parents=True)
    (run / "05_frame_index.jsonl").write_text(
        json.dumps({"sample_id": "s1"}, ensure_ascii=False) + "\n", encoding="utf-8")
    (run / "05_frames").mkdir()
    (run / "05_frames" / "frame_000001.jpg").write_bytes(b"jpg")
    return run


def test_build_reference_writes_manifest_v1_when_run_has_no_manifest(tmp_path):
    run = _run_dir(tmp_path)
    reference = form1_package.build_reference(run)
    with zipfile.ZipFile(reference["zip"]) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["entrypoint"] == "05_frame_index.jsonl"
    assert "05_frame_index.jsonl" in manifest["files"]
    assert manifest["files"] == reference["files"]


def test_build_reference_normalizes_existing_manifest_format(tmp_path):
    run = _run_dir(tmp_path)
    (run / "manifest.json").write_text(json.dumps({
        "format": "crossmodal-files-v1",
        "entrypoint": "05_frame_index.jsonl",
        "artifacts": [{"path": "05_frame_index.jsonl", "role": "main"}],
    }), encoding="utf-8")
    reference = form1_package.build_reference(run)
    with zipfile.ZipFile(reference["zip"]) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["artifacts"] == [{"path": "05_frame_index.jsonl", "role": "main"}]
    assert manifest["entrypoint"] == "05_frame_index.jsonl"
