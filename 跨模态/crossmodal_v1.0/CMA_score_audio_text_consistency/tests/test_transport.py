"""传输适配层测试：文件内容对象与 manifest。"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import json
from pathlib import Path

import pytest

from utils import transport


def _run_dir(tmp_path) -> Path:
    directory = tmp_path / "run001"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "13_passthrough.jsonl").write_text(
        json.dumps({"sample_id": "s1"}, ensure_ascii=False) + "\n", encoding="utf-8")
    (directory / "13_rejects.jsonl").write_text("", encoding="utf-8")
    (directory / "media").mkdir(exist_ok=True)
    (directory / "media" / "seg.wav").write_bytes(b"RIFF....WAVEfmt ")
    return directory


def test_pack_and_restore_roundtrip(tmp_path):
    directory = _run_dir(tmp_path)
    payload = transport.pack_file_object(
        directory, "13_passthrough.jsonl",
        ["13_passthrough.jsonl", "13_rejects.jsonl", "media/seg.wav"])
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    kinds = {item["path"]: item["encoding"] for item in payload["files"]}
    assert kinds["13_passthrough.jsonl"] == "utf-8"
    assert kinds["media/seg.wav"] == "base64"
    restored = transport.restore_file_object(payload, tmp_path / "restored")
    assert restored.name == "13_passthrough.jsonl"
    assert (tmp_path / "restored" / "media" / "seg.wav").read_bytes() == b"RIFF....WAVEfmt "


def test_pack_requires_entrypoint_in_files(tmp_path):
    directory = _run_dir(tmp_path)
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(directory, "13_passthrough.jsonl", ["13_rejects.jsonl"])


def test_pack_rejects_path_escape(tmp_path):
    directory = _run_dir(tmp_path)
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(directory, "13_passthrough.jsonl",
                                   ["13_passthrough.jsonl", "../outside.txt"])


def test_pack_rejects_missing_file(tmp_path):
    directory = _run_dir(tmp_path)
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(directory, "13_passthrough.jsonl",
                                   ["13_passthrough.jsonl", "absent.txt"])


def test_pack_rejects_oversized_payload(tmp_path):
    directory = _run_dir(tmp_path)
    (directory / "big.jsonl").write_text("x" * 4096, encoding="utf-8")
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(directory, "big.jsonl", ["big.jsonl"],
                                   max_response_bytes=128)


def test_restore_rejects_non_empty_directory(tmp_path):
    directory = _run_dir(tmp_path)
    payload = transport.pack_file_object(directory, "13_passthrough.jsonl",
                                         ["13_passthrough.jsonl"])
    target = tmp_path / "restored"
    target.mkdir()
    (target / "existing.txt").write_text("x", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, target)


def test_restore_rejects_bad_format(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({"format": "other"}, tmp_path / "x")


def test_restore_rejects_bad_base64(tmp_path):
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.wav",
               "files": [{"path": "a.wav", "encoding": "base64", "content": "***"}]}
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "y")


def test_manifest_build_write_load(tmp_path):
    manifest = transport.build_manifest(
        "13_passthrough.jsonl",
        [{"path": "13_passthrough.jsonl", "role": "main"},
         {"path": "media/seg.wav", "role": "media"},
         {"path": "manifest.json", "role": "sidecar"}])
    assert manifest["format"] == transport.MANIFEST_FORMAT
    assert [item["path"] for item in manifest["artifacts"]] == [
        "13_passthrough.jsonl", "media/seg.wav"]
    path = transport.write_manifest(tmp_path, manifest)
    assert path.name == "manifest.json"
    assert transport.load_manifest(tmp_path)["entrypoint"] == "13_passthrough.jsonl"
    assert transport.iter_manifest_paths(manifest) == [
        "13_passthrough.jsonl", "media/seg.wav"]


def test_load_manifest_absent_returns_none(tmp_path):
    assert transport.load_manifest(tmp_path) is None


def test_load_manifest_broken_raises(tmp_path):
    (tmp_path / "manifest.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.load_manifest(tmp_path)


def test_resolve_entrypoint_prefers_manifest(tmp_path):
    (tmp_path / "other.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "demo.jsonl").write_text("", encoding="utf-8")
    transport.write_manifest(tmp_path, transport.build_manifest(
        "other.jsonl", [{"path": "other.jsonl", "role": "main"}]))
    path, name = transport.resolve_entrypoint(tmp_path)
    assert name == "other.jsonl"


def test_resolve_entrypoint_requires_unique_when_no_manifest(tmp_path):
    (tmp_path / "a.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)


def test_resolve_entrypoint_single_candidate(tmp_path):
    (tmp_path / "only.jsonl").write_text("", encoding="utf-8")
    path, name = transport.resolve_entrypoint(tmp_path)
    assert name == "only.jsonl"


def test_is_text_and_media_path():
    assert transport.is_text_path("13_passthrough.jsonl")
    assert transport.is_media_path("media/seg.wav")
    assert not transport.is_media_path("13_passthrough.jsonl")
