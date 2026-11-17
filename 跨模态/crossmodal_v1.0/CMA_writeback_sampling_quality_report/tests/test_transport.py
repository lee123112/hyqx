"""传输层测试：``crossmodal-files-v1`` 内容对象、运行清单与入口解析。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

from utils import transport


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run001"
    run.mkdir(parents=True, exist_ok=True)
    (run / "24_spot_check.jsonl").write_text('{"sample_id": "a"}\n', encoding="utf-8")
    (run / "24_quality_report.json").write_text('{"status": "written_back"}',
                                                encoding="utf-8")
    (run / "media").mkdir(exist_ok=True)
    (run / "media" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")
    return run


def test_pack_and_restore_round_trip_is_byte_exact(tmp_path):
    run = _run_dir(tmp_path)
    payload = transport.pack_file_object(
        run, "24_spot_check.jsonl",
        ["24_spot_check.jsonl", "24_quality_report.json", "media/a.png"])
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["entrypoint"] == "24_spot_check.jsonl"
    assert payload["fileCount"] == 3

    target = tmp_path / "restored"
    entry = transport.restore_file_object(payload, target)
    assert entry == target / "24_spot_check.jsonl"
    assert (target / "24_spot_check.jsonl").read_bytes() == \
        (run / "24_spot_check.jsonl").read_bytes()
    assert (target / "media" / "a.png").read_bytes() == \
        (run / "media" / "a.png").read_bytes()


def test_pack_rejects_entrypoint_outside_run_dir(tmp_path):
    run = _run_dir(tmp_path)
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run, "../evil.jsonl", ["../evil.jsonl"])


def test_pack_rejects_absolute_and_url_paths(tmp_path):
    run = _run_dir(tmp_path)
    for bad in ("/abs/x.jsonl", "C:/x.jsonl", "http://example.com/x.jsonl"):
        with pytest.raises(transport.TransportError):
            transport.pack_file_object(run, "24_spot_check.jsonl",
                                       ["24_spot_check.jsonl", bad])


def test_pack_raises_when_response_too_large(tmp_path):
    run = _run_dir(tmp_path)
    (run / "big.jsonl").write_text("x" * 4096, encoding="utf-8")
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(run, "24_spot_check.jsonl",
                                   ["24_spot_check.jsonl", "big.jsonl"],
                                   max_response_bytes=256)


def test_restore_rejects_duplicate_members(tmp_path):
    payload = {
        "format": "crossmodal-files-v1",
        "entrypoint": "a.jsonl",
        "files": [
            {"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"},
            {"path": "A.jsonl", "encoding": "utf-8", "content": "{}\n"},
        ],
    }
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "out")


def test_manifest_build_write_and_load(tmp_path):
    run = _run_dir(tmp_path)
    manifest = transport.build_manifest("24_spot_check.jsonl", [
        {"path": "24_spot_check.jsonl", "role": "main"},
        {"path": "24_quality_report.json", "role": "quality_report"},
        {"path": "24_spot_check.jsonl", "role": "duplicate"},
    ])
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert [item["path"] for item in manifest["artifacts"]] == [
        "24_spot_check.jsonl", "24_quality_report.json"]
    transport.write_manifest(run, manifest)
    loaded = transport.load_manifest(run)
    assert loaded["entrypoint"] == "24_spot_check.jsonl"
    assert transport.iter_manifest_paths(loaded) == [
        "24_spot_check.jsonl", "24_quality_report.json"]


def test_load_manifest_returns_none_when_absent(tmp_path):
    assert transport.load_manifest(tmp_path) is None


def test_resolve_entrypoint_requires_single_jsonl_without_manifest(tmp_path):
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)


def test_resolve_entrypoint_uses_manifest(tmp_path):
    run = _run_dir(tmp_path)
    manifest = transport.build_manifest("24_spot_check.jsonl", [
        {"path": "24_spot_check.jsonl", "role": "main"},
        {"path": "24_quality_report.json", "role": "quality_report"}])
    entry, relative = transport.resolve_entrypoint(run, manifest)
    assert relative == "24_spot_check.jsonl"
    assert entry.is_file()


def test_output_too_large_error_is_transport_error():
    assert issubclass(transport.OutputTooLargeError, transport.TransportError)
