"""传输层测试：``crossmodal-files-v1`` 打包/还原、清单、入口定位与安全解压。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from utils import io as io_utils
from utils import transport

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")


def build_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "media").mkdir(parents=True)
    (run_dir / "01_workitems.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (run_dir / "01_batch_registry.json").write_text('{"b": 2}', encoding="utf-8")
    (run_dir / "media" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nopaque")
    return run_dir


def test_pack_and_restore_roundtrip(tmp_path):
    run_dir = build_dir(tmp_path)
    payload = transport.pack_file_object(
        run_dir, "01_workitems.jsonl",
        ["01_workitems.jsonl", "01_batch_registry.json", "media/a.png"])
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["entrypoint"] == "01_workitems.jsonl"
    assert payload["fileCount"] == 3
    encodings = {item["path"]: item["encoding"] for item in payload["files"]}
    assert encodings["01_workitems.jsonl"] == "utf-8"
    assert encodings["media/a.png"] == "base64"

    restored = transport.restore_file_object(payload, tmp_path / "restored")
    assert restored.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert (restored.parent / "media" / "a.png").read_bytes() == \
        (run_dir / "media" / "a.png").read_bytes()


def test_pack_rejects_missing_and_escape(tmp_path):
    run_dir = build_dir(tmp_path)
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "nope.jsonl")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "01_workitems.jsonl",
                                   ["01_workitems.jsonl", "../escape.jsonl"])
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "/abs/01_workitems.jsonl")


def test_pack_size_limit_fails_instead_of_truncating(tmp_path):
    run_dir = build_dir(tmp_path)
    payload = transport.pack_file_object(run_dir, "01_workitems.jsonl",
                                        ["01_workitems.jsonl"])
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["fileCount"] == 1
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(run_dir, "01_workitems.jsonl",
                                   ["01_workitems.jsonl", "media/a.png"],
                                   max_response_bytes=4)


def test_restore_preserves_lf_newlines(tmp_path):
    payload = {
        "format": "crossmodal-files-v1",
        "entrypoint": "a.jsonl",
        "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "x\ny\n"}],
    }
    target = transport.restore_file_object(payload, tmp_path / "out")
    assert target.read_bytes() == b"x\ny\n"


@pytest.mark.parametrize("bad", [
    {"format": "other", "entrypoint": "a", "files": []},
    {"format": "crossmodal-files-v1", "files": [{"path": "a", "encoding": "utf-8",
                                                 "content": "x"}]},
    {"format": "crossmodal-files-v1", "entrypoint": "a", "files": []},
    {"format": "crossmodal-files-v1", "entrypoint": "a",
     "files": [{"path": "../a", "encoding": "utf-8", "content": "x"}]},
    {"format": "crossmodal-files-v1", "entrypoint": "a",
     "files": [{"path": "a", "encoding": "utf-8", "content": "x"},
               {"path": "A", "encoding": "utf-8", "content": "y"}]},
    {"format": "crossmodal-files-v1", "entrypoint": "a",
     "files": [{"path": "a", "encoding": "base64", "content": "!!!not-base64"}]},
    {"format": "crossmodal-files-v1", "entrypoint": "b",
     "files": [{"path": "a", "encoding": "utf-8", "content": "x"}]},
])
def test_restore_rejects_bad_payload(tmp_path, bad):
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(bad, tmp_path / "out")


def test_build_manifest_dedupes_and_inserts_entrypoint(tmp_path):
    manifest = transport.build_manifest("main.jsonl", [
        {"path": "main.jsonl", "role": "main"},
        {"path": "x.jsonl", "role": "side"},
        {"path": "x.jsonl", "role": "dup"},
    ])
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["entrypoint"] == "main.jsonl"
    assert [item["path"] for item in manifest["artifacts"]] == ["main.jsonl", "x.jsonl"]
    assert transport.iter_manifest_paths(manifest) == ["main.jsonl", "x.jsonl"]


def test_manifest_roundtrip_and_load(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "a.jsonl").write_text("{}\n", encoding="utf-8")
    manifest = transport.build_manifest("a.jsonl", [{"path": "a.jsonl", "role": "main"}])
    transport.write_manifest(run_dir, manifest)
    loaded = transport.load_manifest(run_dir)
    assert loaded == manifest
    assert transport.load_manifest(tmp_path / "none") is None

    (run_dir / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.load_manifest(run_dir)


def test_resolve_entrypoint_requires_single_jsonl(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(run_dir)

    (run_dir / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "a.partial").write_text("", encoding="utf-8")
    path, relative = transport.resolve_entrypoint(run_dir)
    assert relative == "a.jsonl" and path.is_file()

    (run_dir / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(run_dir)


def test_resolve_entrypoint_from_manifest(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "sub").mkdir(parents=True)
    (run_dir / "sub" / "entry.jsonl").write_text("{}\n", encoding="utf-8")
    manifest = {"format": "crossmodal-manifest-v1", "entrypoint": "sub/entry.jsonl",
                "artifacts": [{"path": "sub/entry.jsonl", "role": "main"}]}
    path, relative = transport.resolve_entrypoint(run_dir, manifest)
    assert relative == "sub/entry.jsonl"
    bad = {"format": "crossmodal-manifest-v1", "entrypoint": "../escape.jsonl"}
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(run_dir, bad)


def test_safe_extract_zip_rejects_escape_and_duplicates(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.jsonl", "{}\n")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(io.BytesIO(buffer.getvalue()), tmp_path / "a")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("x.jsonl", "{}\n")
        archive.writestr("X.jsonl", "{}\n")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(io.BytesIO(buffer.getvalue()), tmp_path / "b")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("media/a.png", b"\x89PNG")
    written = io_utils.safe_extract_zip(io.BytesIO(buffer.getvalue()), tmp_path / "c")
    assert written == ["media/a.png"]
    assert (tmp_path / "c" / "media" / "a.png").is_file()


def test_safe_extract_zip_respects_limits(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(3):
            archive.writestr("f%02d.jsonl" % index, "{}\n")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(io.BytesIO(buffer.getvalue()), tmp_path / "d",
                                  max_files=2)
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(io.BytesIO(buffer.getvalue()), tmp_path / "e",
                                  max_unpacked_bytes=2)


def test_normalize_zip_member_name_rejects_ambiguity():
    assert io_utils.normalize_zip_member_name("a/b.jsonl") == "a/b.jsonl"
    assert io_utils.normalize_zip_member_name("./a//b.jsonl") == "a/b.jsonl"
    for bad in ("", "/abs", "a\\b", "..", "a/../b", "C:/x", "~x", "http://h/x"):
        with pytest.raises(ValueError):
            io_utils.normalize_zip_member_name(bad)


def test_partial_writer_commits_and_leaves_no_temp(tmp_path):
    target = tmp_path / "out.jsonl"
    with io_utils.PartialJsonlWriter(target) as writer:
        writer.write({"a": 1})
        writer.write({"b": 2})
    assert io_utils.count_lines(target) == 2
    assert not list(tmp_path.glob("*.partial"))
    assert json.loads(target.read_text(encoding="utf-8").splitlines()[0]) == {"a": 1}
