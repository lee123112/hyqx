"""传输层与路径安全测试：文件内容对象、清单、安全解压、原子写入与任务锁。"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import json
import sys
import zipfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from conftest import png_bytes  # noqa: E402
from utils import io as io_utils  # noqa: E402
from utils import transport  # noqa: E402

MAIN = "18_phrase_region_annotations.jsonl"


# --------------------------------------------------------------------------- #
# 文件内容对象
# --------------------------------------------------------------------------- #

def test_pack_and_restore_roundtrip(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "media").mkdir(parents=True)
    (run_dir / MAIN).write_text('{"a": 1}\n', encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps({"format": "x"}), encoding="utf-8")
    (run_dir / "media" / "m.png").write_bytes(png_bytes())

    obj = transport.pack_file_object(
        run_dir, MAIN, [MAIN, "manifest.json", "media/m.png"])
    assert obj["format"] == "crossmodal-files-v1"
    assert obj["entrypoint"] == MAIN
    assert {item["encoding"] for item in obj["files"]} == {"utf-8", "base64"}

    dest = tmp_path / "restored"
    entry = transport.restore_file_object(obj, dest)
    assert entry.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert (dest / "media" / "m.png").read_bytes() == png_bytes()


def test_pack_requires_entrypoint(tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "other.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(tmp_path / "run", MAIN, ["other.jsonl"])


def test_pack_missing_file_raises(tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / MAIN).write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(tmp_path / "run", MAIN, [MAIN, "gone.png"])


def test_pack_rejects_escape_path(tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / MAIN).write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(tmp_path / "run", MAIN, [MAIN, "../escape.png"])


def test_pack_respects_size_limit(tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / MAIN).write_text("{}\n" * 50, encoding="utf-8")
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(tmp_path / "run", MAIN, [MAIN],
                                   max_response_bytes=16)


def test_restore_rejects_wrong_format():
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({"format": "other", "files": []}, "out")


def test_restore_rejects_duplicate_and_escape(tmp_path):
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
               "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}"},
                         {"path": "A.jsonl", "encoding": "utf-8", "content": "{}"}]}
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "dup")

    escaped = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "../x.jsonl",
               "files": [{"path": "../x.jsonl", "encoding": "utf-8", "content": "{}"}]}
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(escaped, tmp_path / "esc")


def test_restore_rejects_missing_entrypoint(tmp_path):
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
               "files": [{"path": "b.jsonl", "encoding": "utf-8", "content": "{}"}]}
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "miss")


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #

def test_build_manifest_dedups_and_skips_manifest():
    manifest = transport.build_manifest(MAIN, [
        {"path": MAIN, "role": "main"},
        {"path": MAIN, "role": "main"},
        {"path": "manifest.json", "role": "sidecar"},
        {"path": "media/a.png", "role": "media"},
    ])
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert [item["path"] for item in manifest["artifacts"]] == [MAIN, "media/a.png"]
    assert transport.iter_manifest_paths(manifest) == [MAIN, "media/a.png"]


def test_manifest_roundtrip(tmp_path):
    manifest = transport.build_manifest(MAIN, [{"path": MAIN, "role": "main"}])
    transport.write_manifest(tmp_path, manifest)
    assert transport.load_manifest(tmp_path) == manifest


def test_load_manifest_rejects_broken(tmp_path):
    (tmp_path / "manifest.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.load_manifest(tmp_path)


def test_resolve_entrypoint_prefers_manifest(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({
        "format": transport.MANIFEST_FORMAT, "entrypoint": "demo.jsonl",
        "artifacts": []}), encoding="utf-8")
    (tmp_path / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "other.jsonl").write_text("{}\n", encoding="utf-8")
    path, name = transport.resolve_entrypoint(tmp_path)
    assert name == "demo.jsonl" and path.is_file()


def test_resolve_entrypoint_requires_uniqueness(tmp_path):
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["../x.png", "/abs/x.png", "~x.png",
                                   "http://host/x.png", "media\\x.png", "..",
                                   "C:\\x.png"])
def test_resolve_media_rejects_unsafe_paths(tmp_path, value):
    with pytest.raises(io_utils.UnsupportedMediaPath):
        io_utils.resolve_media(tmp_path, value)


def test_resolve_media_accepts_relative(tmp_path):
    target = tmp_path / "media" / "a.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png_bytes())
    assert io_utils.resolve_media(tmp_path, "media/a.png") == target.resolve()
    assert io_utils.resolve_media(tmp_path, "./media/a.png") == target.resolve()


@pytest.mark.parametrize("value", ["", "  ", "../escape", "a/b", "CON", "nul.txt",
                                   "x.", "..", "a\\b"])
def test_validate_unique_id_rejects_unsafe(value):
    with pytest.raises(ValueError):
        io_utils.validate_unique_id(value)


@pytest.mark.parametrize("value", ["task_file1", "任务-01_f1", "a.b.c_f1", "x_1"])
def test_validate_unique_id_accepts_safe(value):
    assert io_utils.validate_unique_id(value) == value


def test_resolve_in_task_rejects_escape(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    with pytest.raises(ValueError):
        io_utils.resolve_in_task(task, "../other/x.jsonl")


def test_resolve_in_task_accepts_inside(tmp_path):
    task = tmp_path / "task"
    (task / "sub").mkdir(parents=True)
    inside = task / "sub" / "x.jsonl"
    inside.write_text("{}\n", encoding="utf-8")
    assert io_utils.resolve_in_task(task, "sub/x.jsonl") == inside.resolve()


# --------------------------------------------------------------------------- #
# zip 安全解压
# --------------------------------------------------------------------------- #

def test_safe_extract_zip_rejects_escape(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.jsonl", "{}")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "dest")
    assert not (tmp_path / "escape.jsonl").exists()


def test_safe_extract_zip_rejects_absolute(tmp_path):
    archive = tmp_path / "abs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/abs.jsonl", "{}")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "dest_abs")


def test_safe_extract_zip_rejects_duplicate_case_insensitive(tmp_path):
    archive = tmp_path / "dup.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.jsonl", "{}")
        zf.writestr("A.jsonl", "{}")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "dest_dup")


def test_safe_extract_zip_rejects_size_limit(tmp_path):
    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.jsonl", "x" * 4096)
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "dest_big",
                                  max_unpacked_bytes=128)


def test_safe_extract_zip_extracts_nested(tmp_path):
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("demo.jsonl", "{}\n")
        zf.writestr("media/a.png", png_bytes())
    written = io_utils.safe_extract_zip(archive, tmp_path / "dest_ok")
    assert sorted(written) == ["demo.jsonl", "media/a.png"]
    assert (tmp_path / "dest_ok" / "media" / "a.png").is_file()


# --------------------------------------------------------------------------- #
# 原子写入与任务锁
# --------------------------------------------------------------------------- #

def test_atomic_write_json_replaces_without_truncation(tmp_path):
    target = tmp_path / "lineage.json"
    io_utils.atomic_write_json(target, {"a": 1})
    io_utils.atomic_write_json(target, {"b": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"b": 2}
    assert list(tmp_path.glob("*.tmp")) == []


def test_jsonl_writer_keeps_source_when_same_path(tmp_path):
    """输入输出同路径时必须原子替换，不得截断源文件。"""
    src = tmp_path / "18.jsonl"
    src.write_text('{"sample_id": "a"}\n', encoding="utf-8")
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines()]
    out = src.with_suffix(".tmp.jsonl")
    with io_utils.JsonlWriter(out) as writer:
        for row in rows:
            writer.write(row)
    import os
    os.replace(out, src)
    assert json.loads(src.read_text(encoding="utf-8")) == {"sample_id": "a"}


def test_task_lock_is_exclusive_across_instances(tmp_path):
    root = tmp_path / "output"
    first = io_utils.TaskLock(root, "task_a")
    first.acquire()
    with pytest.raises(io_utils.TaskBusyError):
        io_utils.TaskLock(root, "task_a").acquire()
    other = io_utils.TaskLock(root, "task_b")
    other.acquire()
    other.release()
    first.release()


def test_remove_task_removes_only_target(tmp_path):
    root = tmp_path / "output"
    (root / "task_a").mkdir(parents=True)
    (root / "task_b").mkdir(parents=True)
    assert io_utils.remove_task(root, "task_a") is True
    assert not (root / "task_a").exists()
    assert (root / "task_b").exists()
    assert io_utils.remove_task(root, "task_a") is False


def test_iter_decoded_lines_rejects_non_utf8(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_bytes('{"sample_id": "中文"}'.encode("gbk") + b"\n")
    with pytest.raises(UnicodeDecodeError):
        list(io_utils.iter_decoded_lines(path, "utf-8"))
