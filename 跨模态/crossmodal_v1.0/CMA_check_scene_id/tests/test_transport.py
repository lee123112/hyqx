"""传输层与 IO 安全测试：crossmodal-files-v1、manifest、ZIP 安全解压、原子写。"""
from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

import pytest

from utils import io as io_utils
from utils import transport
from tests.support import read_json

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")


def make_zip(path, entries, symlink=None):
    """按给定成员名写 zip；entries 为 {name: bytes}。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(target), "w") as handle:
        for name, payload in entries.items():
            handle.writestr(name, payload)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            handle.writestr(info, "../outside")
    return target


# ------------------------------------------------------------ manifest
def test_build_manifest_dedupes_and_skips_manifest_itself():
    manifest = transport.build_manifest("main.jsonl", [
        {"path": "main.jsonl", "role": "main"},
        {"path": "main.jsonl", "role": "main"},
        {"path": "manifest.json", "role": "sidecar"},
        {"path": "./media/a.png", "role": "media"},
    ])
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["entrypoint"] == "main.jsonl"
    assert manifest["artifacts"] == [
        {"path": "main.jsonl", "role": "main"},
        {"path": "media/a.png", "role": "media"},
    ]


@pytest.mark.parametrize("path", ["", "  ", "/abs/a.jsonl", "a/../b.jsonl", "../b.jsonl"])
def test_build_manifest_rejects_unsafe_paths(path):
    with pytest.raises(transport.TransportError):
        transport.build_manifest("main.jsonl", [{"path": path, "role": "main"}])


def test_write_and_load_manifest_roundtrip(tmp_path):
    manifest = transport.build_manifest("main.jsonl", [{"path": "main.jsonl", "role": "main"}])
    transport.write_manifest(tmp_path, manifest)
    loaded = transport.load_manifest(tmp_path)
    assert loaded["entrypoint"] == "main.jsonl"
    assert read_json(tmp_path / "manifest.json")["format"] == "crossmodal-manifest-v1"


def test_load_manifest_broken_json(tmp_path):
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.load_manifest(tmp_path)


def test_load_manifest_wrong_format(tmp_path):
    (tmp_path / "manifest.json").write_text('{"format": "other"}', encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.load_manifest(tmp_path)


# ------------------------------------------------------------ 入口定位
def test_resolve_entrypoint_uses_manifest(tmp_path):
    (tmp_path / "main.jsonl").write_text("{}\n", encoding="utf-8")
    transport.write_manifest(tmp_path, transport.build_manifest(
        "main.jsonl", [{"path": "main.jsonl", "role": "main"}]))
    entry, relative = transport.resolve_entrypoint(tmp_path)
    assert entry.name == "main.jsonl"
    assert relative == "main.jsonl"


def test_resolve_entrypoint_missing_manifest_entrypoint_file(tmp_path):
    transport.write_manifest(tmp_path, transport.build_manifest(
        "gone.jsonl", [{"path": "gone.jsonl", "role": "main"}]))
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)


def test_resolve_entrypoint_auto_detect_known_name(tmp_path):
    (tmp_path / "02_scene_verified.jsonl").write_text("{}\n", encoding="utf-8")
    entry, relative = transport.resolve_entrypoint(tmp_path)
    assert entry.name == "02_scene_verified.jsonl"
    assert relative == "02_scene_verified.jsonl"


def test_resolve_entrypoint_auto_detect_single_jsonl(tmp_path):
    (tmp_path / "anything.jsonl").write_text("{}\n", encoding="utf-8")
    entry, _relative = transport.resolve_entrypoint(tmp_path)
    assert entry.name == "anything.jsonl"


def test_resolve_entrypoint_ambiguous(tmp_path):
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)


def test_resolve_entrypoint_none(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)


# ------------------------------------------------------------ 打包 / 恢复
def _bundle(tmp_path):
    (tmp_path / "media").mkdir(parents=True, exist_ok=True)
    (tmp_path / "main.jsonl").write_text('{"sample_id": "s-1"}\n', encoding="utf-8")
    (tmp_path / "media" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    return tmp_path


def test_pack_file_object_text_and_binary(tmp_path):
    bundle = _bundle(tmp_path / "src")
    payload = transport.pack_file_object(bundle, "main.jsonl",
                                         ["main.jsonl", "media/a.png"])
    assert payload["format"] == "crossmodal-files-v1"
    encodings = {item["path"]: item["encoding"] for item in payload["files"]}
    assert encodings["main.jsonl"] == "utf-8"
    assert encodings["media/a.png"] == "base64"


def test_restore_file_object_roundtrip(tmp_path):
    bundle = _bundle(tmp_path / "src")
    payload = transport.pack_file_object(bundle, "main.jsonl",
                                         ["main.jsonl", "media/a.png"])
    restored = transport.restore_file_object(payload, tmp_path / "back")
    assert restored.name == "main.jsonl"
    assert restored.read_text(encoding="utf-8") == '{"sample_id": "s-1"}\n'
    assert (tmp_path / "back" / "media" / "a.png").read_bytes() == b"\x89PNG\r\n\x1a\n\x00"


def test_pack_file_object_requires_entrypoint(tmp_path):
    bundle = _bundle(tmp_path / "src")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(bundle, "main.jsonl", ["media/a.png"])


def test_pack_file_object_missing_file(tmp_path):
    bundle = _bundle(tmp_path / "src")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(bundle, "main.jsonl", ["main.jsonl", "media/none.png"])


def test_pack_file_object_rejects_escape(tmp_path):
    bundle = _bundle(tmp_path / "src")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(bundle, "main.jsonl", ["main.jsonl", "../outside.txt"])


def test_pack_file_object_too_large(tmp_path):
    bundle = _bundle(tmp_path / "src")
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(bundle, "main.jsonl", ["main.jsonl"],
                                   max_response_bytes=16)


def test_pack_file_object_rejects_non_utf8_text(tmp_path):
    target = tmp_path / "src"
    target.mkdir()
    (target / "main.jsonl").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(target, "main.jsonl", ["main.jsonl"])


@pytest.mark.parametrize("payload", [
    {}, {"format": "other"}, {"format": "crossmodal-files-v1"},
    {"format": "crossmodal-files-v1", "entrypoint": "a.jsonl", "files": []},
])
def test_restore_file_object_invalid_payload(payload, tmp_path):
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "dest")


def test_restore_file_object_rejects_duplicate_paths(tmp_path):
    payload = {
        "format": "crossmodal-files-v1", "entrypoint": "a.jsonl",
        "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"},
                  {"path": "A.jsonl", "encoding": "utf-8", "content": "{}\n"}],
    }
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "dest")


def test_restore_file_object_rejects_traversal(tmp_path):
    payload = {
        "format": "crossmodal-files-v1", "entrypoint": "../evil.jsonl",
        "files": [{"path": "../evil.jsonl", "encoding": "utf-8", "content": "{}\n"}],
    }
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "dest")


def test_restore_file_object_rejects_bad_base64(tmp_path):
    payload = {
        "format": "crossmodal-files-v1", "entrypoint": "a.png",
        "files": [{"path": "a.png", "encoding": "base64", "content": "!!!!"}],
    }
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "dest")


def test_restore_file_object_rejects_unknown_encoding(tmp_path):
    payload = {
        "format": "crossmodal-files-v1", "entrypoint": "a.jsonl",
        "files": [{"path": "a.jsonl", "encoding": "hex", "content": "00"}],
    }
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "dest")


def test_restore_file_object_requires_empty_destination(tmp_path):
    payload = {
        "format": "crossmodal-files-v1", "entrypoint": "a.jsonl",
        "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"}],
    }
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "occupied.txt").write_text("x", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, dest)


def test_restore_file_object_base64_content_is_recovered(tmp_path):
    raw = b"\x00\x01\x02binary"
    payload = {
        "format": "crossmodal-files-v1", "entrypoint": "blob.bin",
        "files": [{"path": "blob.bin", "encoding": "base64",
                   "content": base64.b64encode(raw).decode("ascii")}],
    }
    restored = transport.restore_file_object(payload, tmp_path / "dest")
    assert restored.read_bytes() == raw


def test_iter_manifest_paths_orders_entrypoint_first():
    manifest = transport.build_manifest("main.jsonl", [
        {"path": "media/a.png", "role": "media"},
        {"path": "main.jsonl", "role": "main"},
    ])
    assert transport.iter_manifest_paths(manifest) == ["main.jsonl", "media/a.png"]


# ------------------------------------------------------------ ZIP 安全解压
def test_safe_extract_zip_writes_members(tmp_path):
    archive = make_zip(tmp_path / "ok.zip", {"main.jsonl": b"{}\n",
                                             "media/a.png": b"\x89PNG"})
    members = io_utils.safe_extract_zip(archive, tmp_path / "dest")
    assert sorted(members) == ["main.jsonl", "media/a.png"]
    assert (tmp_path / "dest" / "media" / "a.png").read_bytes() == b"\x89PNG"


@pytest.mark.parametrize("name", ["../evil.txt", "/abs/evil.txt", "a/../../evil.txt",
                                  "c:evil.txt", "a.txt:ads"])
def test_safe_extract_zip_rejects_unsafe_members(name, tmp_path):
    archive = make_zip(tmp_path / "bad.zip", {name: b"x"})
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "dest")
    # 失败时不创建目标目录内容
    assert not (tmp_path / "dest").exists() or not any((tmp_path / "dest").iterdir())


def test_safe_extract_zip_normalises_separator_member(tmp_path):
    """反斜杠成员由 zipfile 规范化后落在目标目录内，不产生逃逸。"""
    archive = make_zip(tmp_path / "sep.zip", {"sub\\inner.jsonl": b"{}\n"})
    members = io_utils.safe_extract_zip(archive, tmp_path / "dest")
    assert members == ["sub/inner.jsonl"]
    assert (tmp_path / "dest" / "sub" / "inner.jsonl").is_file()


def test_zip_member_name_rejects_colon(tmp_path):
    with pytest.raises(ValueError):
        io_utils.normalize_zip_member_name("a.txt:ads")


def test_safe_extract_zip_rejects_symlink(tmp_path):
    archive = make_zip(tmp_path / "link.zip", {"main.jsonl": b"{}\n"},
                       symlink="link.txt")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "dest")


def test_safe_extract_zip_rejects_case_collision(tmp_path):
    archive = make_zip(tmp_path / "dup.zip", {"A.jsonl": b"1", "a.jsonl": b"2"})
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "dest")


def test_safe_extract_zip_rejects_reserved_name(tmp_path):
    archive = make_zip(tmp_path / "res.zip", {"nul.jsonl": b"1"})
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "dest")


def test_safe_extract_zip_rejects_too_many_files(tmp_path):
    archive = make_zip(tmp_path / "many.zip", {"f%d.jsonl" % i: b"1" for i in range(5)})
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "dest", max_files=2)


def test_safe_extract_zip_rejects_oversize(tmp_path):
    archive = make_zip(tmp_path / "big.zip", {"main.jsonl": b"x" * 4096})
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "dest", max_unpacked_bytes=128)


def test_safe_extract_zip_rejects_empty_archive(tmp_path):
    archive = make_zip(tmp_path / "empty.zip", {})
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "dest")


def test_safe_extract_zip_requires_empty_destination(tmp_path):
    archive = make_zip(tmp_path / "ok.zip", {"main.jsonl": b"{}\n"})
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "occupied").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, dest)


def test_commit_directory_moves_into_place(tmp_path):
    source = tmp_path / "stage"
    source.mkdir()
    (source / "a.jsonl").write_text("{}\n", encoding="utf-8")
    target = tmp_path / "final"
    io_utils.commit_directory(source, target)
    assert (target / "a.jsonl").is_file()
    assert not source.exists()


def test_commit_directory_refuses_existing_target(tmp_path):
    source = tmp_path / "stage"
    source.mkdir()
    target = tmp_path / "final"
    target.mkdir()
    with pytest.raises(ValueError):
        io_utils.commit_directory(source, target)


# ------------------------------------------------------------ 路径与 IO 工具
@pytest.mark.parametrize("value,expected", [
    ("a/b", "a/b"), ("./a/b", "a/b"), ("a//b", "a/b"),
])
def test_normalize_zip_member_name(value, expected):
    assert io_utils.normalize_zip_member_name(value) == expected


@pytest.mark.parametrize("value", ["", "/abs", "../x", "a\\b", ".."])
def test_normalize_zip_member_name_rejects(value):
    with pytest.raises(ValueError):
        io_utils.normalize_zip_member_name(value)


def test_resolve_media_rejects_url_and_absolute(tmp_path):
    for value in ("http://example.com/a.png", "/abs/a.png", "C:/abs/a.png", "~/a.png",
                  "../a.png", "media\\a.png"):
        with pytest.raises(io_utils.UnsupportedMediaPath):
            io_utils.resolve_media(tmp_path, value)


def test_resolve_media_accepts_relative(tmp_path):
    target = io_utils.resolve_media(tmp_path, "media/a.png")
    assert target == (tmp_path / "media" / "a.png").resolve()


def test_iter_decoded_lines_strict_utf8(tmp_path):
    path = tmp_path / "gbk.txt"
    path.write_bytes("中文".encode("gbk"))
    with pytest.raises(UnicodeDecodeError):
        list(io_utils.iter_decoded_lines(path, "utf-8"))


def test_iter_decoded_lines_strips_crlf(tmp_path):
    path = tmp_path / "crlf.jsonl"
    path.write_bytes(b'{"a": 1}\r\n{"a": 2}\r\n')
    assert list(io_utils.iter_decoded_lines(path)) == ['{"a": 1}', '{"a": 2}']


def test_jsonl_writer_counts_records(tmp_path):
    path = tmp_path / "out.jsonl"
    with io_utils.JsonlWriter(path) as writer:
        writer.write({"a": 1})
        writer.write({"b": 2})
    assert writer.count == 2
    assert path.read_text(encoding="utf-8").count("\n") == 2


def test_atomic_write_json_replaces_existing(tmp_path):
    path = tmp_path / "x.json"
    io_utils.atomic_write_json(path, {"v": 1})
    io_utils.atomic_write_json(path, {"v": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 2}
    assert list(tmp_path.glob("*.tmp")) == []


def test_next_run_id_increments(tmp_path):
    assert io_utils.next_run_id(tmp_path) == "run001"
    (tmp_path / "run001").mkdir()
    assert io_utils.next_run_id(tmp_path) == "run002"


def test_remove_task_only_removes_target(tmp_path):
    root = io_utils.resolve_root(tmp_path / "root")
    (root / "task_a").mkdir(parents=True, exist_ok=True)
    (root / "task_b").mkdir(parents=True, exist_ok=True)
    assert io_utils.remove_task(root, "task_a") is True
    assert not (root / "task_a").exists()
    assert (root / "task_b").exists()
    assert io_utils.remove_task(root, "task_a") is False


def test_directory_size_and_sha256(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"12345")
    assert transport.directory_size(tmp_path) == 5
    assert len(transport.file_sha256(tmp_path / "a.bin")) == 64
