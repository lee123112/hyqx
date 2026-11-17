"""传输适配层测试：文件内容对象、运行清单、入口定位与路径安全。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

from utils import transport
from utils.transport import (
    FILE_OBJECT_FORMAT,
    MANIFEST_FORMAT,
    OutputTooLargeError,
    TransportError,
)


def _write(directory: Path, relative: str, text: str) -> Path:
    target = directory / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_pack_file_object_marks_text_utf8_and_binary_base64(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write(run_dir, "20_alignment_qc.jsonl", '{"sample_id": "s1"}\n')
    (run_dir / "media").mkdir(parents=True, exist_ok=True)
    (run_dir / "media" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    payload = transport.pack_file_object(
        run_dir, "20_alignment_qc.jsonl",
        ["20_alignment_qc.jsonl", "media/a.png"])

    assert payload["format"] == FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "20_alignment_qc.jsonl"
    by_path = {item["path"]: item for item in payload["files"]}
    assert by_path["20_alignment_qc.jsonl"]["encoding"] == "utf-8"
    assert by_path["media/a.png"]["encoding"] == "base64"


def test_restore_roundtrip_reproduces_entry_and_media(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write(run_dir, "20_alignment_qc.jsonl", '{"sample_id": "s1"}\n')
    (run_dir / "media").mkdir(parents=True, exist_ok=True)
    raw = b"\x89PNG\r\n\x1a\nbytes"
    (run_dir / "media" / "a.png").write_bytes(raw)

    payload = transport.pack_file_object(
        run_dir, "20_alignment_qc.jsonl", ["20_alignment_qc.jsonl", "media/a.png"])
    restored = tmp_path / "restored"
    entry = transport.restore_file_object(payload, restored)

    assert entry.name == "20_alignment_qc.jsonl"
    assert (restored / "media" / "a.png").read_bytes() == raw
    assert json.loads(entry.read_text(encoding="utf-8"))["sample_id"] == "s1"


def test_pack_rejects_missing_entrypoint(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write(run_dir, "other.jsonl", "{}\n")
    with pytest.raises(TransportError):
        transport.pack_file_object(run_dir, "20_alignment_qc.jsonl", ["other.jsonl"])


@pytest.mark.parametrize("bad", ["/abs/path.jsonl", "../escape.jsonl", "a/../../b.jsonl",
                                 "", "media\\win.jsonl"])
def test_pack_rejects_unsafe_relative_paths(tmp_path: Path, bad: str) -> None:
    run_dir = tmp_path / "run"
    _write(run_dir, "20_alignment_qc.jsonl", "{}\n")
    with pytest.raises(TransportError):
        transport.pack_file_object(run_dir, "20_alignment_qc.jsonl",
                                  ["20_alignment_qc.jsonl", bad])


def test_pack_fails_when_output_too_large(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write(run_dir, "20_alignment_qc.jsonl", "x" * 4096 + "\n")
    with pytest.raises(OutputTooLargeError):
        transport.pack_file_object(run_dir, "20_alignment_qc.jsonl",
                                   ["20_alignment_qc.jsonl"], max_response_bytes=256)


def test_restore_rejects_duplicate_and_non_file_object(tmp_path: Path) -> None:
    with pytest.raises(TransportError):
        transport.restore_file_object({"format": "other"}, tmp_path / "d1")

    payload = {
        "format": FILE_OBJECT_FORMAT,
        "entrypoint": "a.jsonl",
        "files": [
            {"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"},
            {"path": "A.jsonl", "encoding": "utf-8", "content": "{}\n"},
        ],
    }
    with pytest.raises(TransportError):
        transport.restore_file_object(payload, tmp_path / "d2")


def test_restore_rejects_bad_base64(tmp_path: Path) -> None:
    payload = {
        "format": FILE_OBJECT_FORMAT,
        "entrypoint": "a.jsonl",
        "files": [{"path": "a.jsonl", "encoding": "base64", "content": "!!not-base64!!"}],
    }
    with pytest.raises(TransportError):
        transport.restore_file_object(payload, tmp_path / "d")


def test_manifest_roundtrip_and_entrypoint_resolution(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write(run_dir, "20_alignment_qc.jsonl", "{}\n")
    manifest = transport.build_manifest("20_alignment_qc.jsonl", [
        {"path": "20_alignment_qc.jsonl", "role": "main"},
        {"path": "20_reverse_verification.jsonl", "role": "sidecar"},
    ])
    assert manifest["format"] == MANIFEST_FORMAT
    transport.write_manifest(run_dir, manifest)

    loaded = transport.load_manifest(run_dir)
    assert loaded is not None
    entry, relative = transport.resolve_entrypoint(run_dir, loaded)
    assert entry.name == "20_alignment_qc.jsonl"
    assert relative == "20_alignment_qc.jsonl"


def test_resolve_entrypoint_requires_manifest_when_ambiguous(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write(run_dir, "a.jsonl", "{}\n")
    _write(run_dir, "b.jsonl", "{}\n")
    with pytest.raises(TransportError):
        transport.resolve_entrypoint(run_dir)


def test_resolve_entrypoint_ignores_dot_directories(tmp_path: Path) -> None:
    run_dir = tmp_path / "run" / ".tmp" / "upload_x" / "content"
    _write(run_dir, "20_alignment_qc.jsonl", "{}\n")
    entry, relative = transport.resolve_entrypoint(run_dir)
    assert entry.name == "20_alignment_qc.jsonl"
    assert relative == "20_alignment_qc.jsonl"


def test_load_manifest_rejects_wrong_format(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps({"format": "nope"}),
                                           encoding="utf-8")
    with pytest.raises(TransportError):
        transport.load_manifest(run_dir)


def test_iter_manifest_paths_is_ordered_and_unique() -> None:
    manifest = {
        "entrypoint": "main.jsonl",
        "artifacts": [{"path": "main.jsonl"}, {"path": "side.jsonl"}],
    }
    assert transport.iter_manifest_paths(manifest) == ["main.jsonl", "side.jsonl"]
