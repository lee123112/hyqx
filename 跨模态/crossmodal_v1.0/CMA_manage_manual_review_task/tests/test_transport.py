"""传输层单测：``crossmodal-files-v1`` 内容对象、运行清单与路径边界。"""
from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

from tests.conftest import uncertain_record, write_jsonl
from utils import io as io_utils
from utils import transport


def make_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run001"
    run_dir.mkdir(parents=True)
    write_jsonl(run_dir / "25_review_candidates.jsonl", [uncertain_record("tp-01")])
    (run_dir / "25_review_report.json").write_text('{"status": "waiting_manual"}',
                                                   encoding="utf-8")
    (run_dir / "media.bin").write_bytes(b"\x00\x01\x02\x03")
    return run_dir


def test_pack_and_restore_round_trip(tmp_path):
    run_dir = make_run_dir(tmp_path)
    payload = transport.pack_file_object(run_dir, "25_review_candidates.jsonl", [
        "25_review_candidates.jsonl", "25_review_report.json", "media.bin"])
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["fileCount"] == 3
    by_path = {item["path"]: item for item in payload["files"]}
    assert by_path["25_review_candidates.jsonl"]["encoding"] == "utf-8"
    assert by_path["media.bin"]["encoding"] == "base64"

    destination = tmp_path / "restored"
    entry = transport.restore_file_object(payload, destination)
    assert entry == destination / "25_review_candidates.jsonl"
    assert entry.read_bytes() == (run_dir / "25_review_candidates.jsonl").read_bytes()
    assert (destination / "media.bin").read_bytes() == b"\x00\x01\x02\x03"


def test_pack_rejects_escape_and_missing(tmp_path):
    run_dir = make_run_dir(tmp_path)
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "../escape.jsonl")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "25_review_candidates.jsonl", ["not_here.jsonl"])


def test_pack_deduplicates_paths_case_insensitively(tmp_path):
    run_dir = make_run_dir(tmp_path)
    payload = transport.pack_file_object(run_dir, "25_review_candidates.jsonl", [
        "25_review_candidates.jsonl", "25_review_candidates.jsonl"])
    assert payload["fileCount"] == 1


def test_size_limit_fails_instead_of_truncating(tmp_path):
    run_dir = make_run_dir(tmp_path)
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(run_dir, "25_review_candidates.jsonl",
                                   ["25_review_candidates.jsonl", "media.bin"],
                                   max_response_bytes=4)


def test_restore_rejects_absolute_and_parent_paths(tmp_path):
    for bad in ("../escape.jsonl", "/etc/passwd", "a\\b.jsonl", "C:/x.jsonl"):
        payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": bad,
                   "files": [{"path": bad, "encoding": "utf-8", "content": "x"}]}
        with pytest.raises(transport.TransportError):
            transport.restore_file_object(payload, tmp_path / "r")


def test_restore_rejects_duplicate_and_bad_encoding(tmp_path):
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
               "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "1"},
                         {"path": "A.jsonl", "encoding": "utf-8", "content": "2"}]}
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "dup")

    payload["files"] = [{"path": "a.jsonl", "encoding": "base64", "content": "!!!"}]
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "bad")


def test_restore_preserves_lf_newlines(tmp_path):
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
               "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "1\n2\n"}]}
    entry = transport.restore_file_object(payload, tmp_path / "lf")
    assert entry.read_bytes() == b"1\n2\n"


def test_restore_rejects_non_file_object(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({"format": "other"}, tmp_path / "x")


def test_manifest_round_trip_and_validation(tmp_path):
    manifest = transport.build_manifest("a.jsonl", [
        {"path": "a.jsonl", "role": "main"}, {"path": "b.jsonl", "role": "rejects"}])
    transport.write_manifest(tmp_path, manifest)
    loaded = transport.load_manifest(tmp_path)
    assert loaded["entrypoint"] == "a.jsonl"
    assert transport.iter_manifest_paths(loaded) == ["a.jsonl", "b.jsonl"]

    (tmp_path / transport.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.load_manifest(tmp_path)


def test_load_manifest_returns_none_when_absent(tmp_path):
    assert transport.load_manifest(tmp_path) is None


def test_resolve_entrypoint_prefers_manifest(tmp_path):
    manifest = transport.build_manifest("chosen.jsonl", [{"path": "chosen.jsonl"}])
    write_jsonl(tmp_path / "chosen.jsonl", [uncertain_record("e-01")])
    transport.write_manifest(tmp_path, manifest)
    entry, relative = transport.resolve_entrypoint(tmp_path, manifest)
    assert entry.name == "chosen.jsonl"
    assert relative == "chosen.jsonl"


def test_resolve_entrypoint_requires_unambiguous_single_jsonl(tmp_path):
    write_jsonl(tmp_path / "one.jsonl", [uncertain_record("e-01")])
    entry, _ = transport.resolve_entrypoint(tmp_path, None)
    assert entry.name == "one.jsonl"

    write_jsonl(tmp_path / "two.jsonl", [uncertain_record("e-02")])
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path, None)


def test_resolve_entrypoint_missing_directory(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path / "nope", None)


# --------------------------------------------------------------------------- #
# 安全解压
# --------------------------------------------------------------------------- #

def build_zip(entries) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    buffer.seek(0)
    return buffer


def test_safe_extract_zip_writes_relative_members(tmp_path):
    archive = build_zip([("in/a.jsonl", "{}\n"), ("in/b.jsonl", "{}\n")])
    written = io_utils.safe_extract_zip(archive, tmp_path / "unzip")
    assert written == ["in/a.jsonl", "in/b.jsonl"]
    assert (tmp_path / "unzip" / "in" / "a.jsonl").is_file()


@pytest.mark.parametrize("name", ["../escape.jsonl", "/abs.jsonl",
                                  "C:/x.jsonl", "~/x.jsonl", "http://x/a.jsonl"])
def test_safe_extract_zip_rejects_escapes(tmp_path, name):
    archive = build_zip([(name, "{}\n")])
    destination = tmp_path / "unzip"
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, destination)
    assert not any(destination.parent.glob("escape*"))


def test_safe_extract_zip_rejects_too_many_files(tmp_path):
    archive = build_zip([("f%02d.jsonl" % index, "{}\n") for index in range(5)])
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "many", max_files=3)


def test_safe_extract_zip_requires_empty_destination(tmp_path):
    destination = tmp_path / "notempty"
    destination.mkdir()
    (destination / "existing.txt").write_text("x", encoding="utf-8")
    archive = build_zip([("a.jsonl", "{}\n")])
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, destination)


def test_normalize_zip_member_name_collapses_dots():
    assert io_utils.normalize_zip_member_name("./a/./b.jsonl") == "a/b.jsonl"
    with pytest.raises(ValueError):
        io_utils.normalize_zip_member_name("")


def test_base64_member_content_is_validated(tmp_path):
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.bin",
               "files": [{"path": "a.bin", "encoding": "base64",
                          "content": base64.b64encode(b"\x00\x01").decode("ascii")}]}
    entry = transport.restore_file_object(payload, tmp_path / "b64")
    assert entry.read_bytes() == b"\x00\x01"


def test_manifest_entries_have_json_serialisable_roles(tmp_path):
    manifest = transport.build_manifest("a.jsonl", [{"path": "a.jsonl"}])
    json.dumps(manifest)
    assert manifest["artifacts"][0]["path"] == "a.jsonl"
