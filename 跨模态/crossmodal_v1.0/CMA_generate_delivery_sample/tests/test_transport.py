"""I/O 安全与传输测试：路径校验、原子写入、锁、安全解压、文件对象往返。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

from utils import io as io_utils
from utils import transport


# --------------------------------------------------------------------------- #
# uniqueId 与相对引用
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["", "   ", "a/b", "../x", "C:/x", "x.", "con", ".",
                                   "..", "x\x00", "a\\b"])
def test_validate_unique_id_rejects_bad_values(value):
    with pytest.raises(ValueError):
        io_utils.validate_unique_id(value)


@pytest.mark.parametrize("value", ["task_1", "任务-1_f1", "cm-0001_x.2",
                                   "A" * 16 + "_" + "B" * 16])
def test_validate_unique_id_accepts_good_values(value):
    assert io_utils.validate_unique_id(value) == value


@pytest.mark.parametrize("value", ["A" * 64, "task", "t1", "_f1", "task_"])
def test_validate_unique_id_rejects_missing_task_or_file_segment(value):
    """规范正文第 2 条：``uniqueid=任务id_文件id``；单段或空段一律拒绝。"""
    with pytest.raises(ValueError):
        io_utils.validate_unique_id(value)


@pytest.mark.parametrize("value", ["C:/x", "/x", "../x", "a\\b", "~x", "http://x",
                                   "oss://b/k", "", "."])
def test_normalize_relative_rejects_unsafe(value):
    with pytest.raises(io_utils.UnsupportedMediaPath):
        io_utils._normalize_relative(value)
    assert io_utils.is_safe_relative(value) is False


def test_normalize_relative_accepts_relative():
    assert io_utils._normalize_relative("media/a.png") == "media/a.png"
    assert io_utils._normalize_relative("a/./b.png") == "a/b.png"
    assert io_utils.is_external_reference("oss://b/k") is True


def test_resolve_media_blocks_traversal(tmp_path):
    with pytest.raises(io_utils.UnsupportedMediaPath):
        io_utils.resolve_media(tmp_path, "../x")
    assert io_utils.resolve_media(tmp_path, "a/b.png").is_absolute()


# --------------------------------------------------------------------------- #
# 原子写入
# --------------------------------------------------------------------------- #

def test_jsonl_writer_does_not_truncate_same_path_input(tmp_path):
    target = tmp_path / "same.jsonl"
    target.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    with io_utils.JsonlWriter(target) as writer:
        for line in io_utils.iter_decoded_lines(target):
            writer.write({"value": json.loads(line)["a"]})
    rows = [json.loads(line) for line in
            target.read_text(encoding="utf-8").strip().splitlines()]
    assert rows == [{"value": 1}, {"value": 2}]


def test_jsonl_writer_removes_partial_on_error(tmp_path):
    target = tmp_path / "boom.jsonl"
    with pytest.raises(RuntimeError):
        with io_utils.JsonlWriter(target) as writer:
            writer.write({"a": 1})
            raise RuntimeError("boom")
    assert not target.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_atomic_write_json_roundtrip(tmp_path):
    target = tmp_path / "x.json"
    io_utils.atomic_write_json(target, {"中文": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"中文": 1}
    assert not list(tmp_path.glob("*.partial"))


def test_iter_decoded_lines_raises_on_bad_encoding(tmp_path):
    target = tmp_path / "gbk.jsonl"
    target.write_bytes("中文\n".encode("gbk"))
    with pytest.raises(UnicodeDecodeError):
        list(io_utils.iter_decoded_lines(target))


def test_count_jsonl_records_counts_non_empty(tmp_path):
    target = tmp_path / "c.jsonl"
    target.write_text('{"a":1}\n\n{"a":2}\n', encoding="utf-8")
    assert io_utils.count_jsonl_records(target) == 2


# --------------------------------------------------------------------------- #
# 任务锁
# --------------------------------------------------------------------------- #

def test_task_lock_is_exclusive_and_released(tmp_path):
    with io_utils.TaskLock(tmp_path, "locktask_f1"):
        with pytest.raises(io_utils.TaskBusyError):
            io_utils.TaskLock(tmp_path, "locktask_f1").acquire()
    with io_utils.TaskLock(tmp_path, "locktask_f1"):
        pass


def test_remove_task_is_idempotent_and_scoped(tmp_path):
    (tmp_path / "one_f1").mkdir()
    (tmp_path / "two").mkdir()
    assert io_utils.remove_task(tmp_path, "one_f1") is True
    assert io_utils.remove_task(tmp_path, "one_f1") is False
    assert (tmp_path / "two").is_dir()


# --------------------------------------------------------------------------- #
# 安全解压
# --------------------------------------------------------------------------- #

def test_safe_extract_zip_rejects_traversal(tmp_path):
    archive = tmp_path / "slip.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.jsonl", "{}")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out")
    assert not (tmp_path / "escape.jsonl").exists()


def test_safe_extract_zip_rejects_absolute_path_member(tmp_path):
    archive = tmp_path / "abs.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("/abs.jsonl", "{}")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out2")
    assert not (tmp_path / "abs.jsonl").exists()


def test_zip_member_backslash_is_rejected_by_normalizer():
    """zipfile 会把 os.sep 归一为 "/"，因此反斜杠防线在路径归一函数上验证。"""
    with pytest.raises(ValueError):
        io_utils._normalize_relative("a\\b.jsonl")
    assert io_utils.is_safe_relative("a\\b.jsonl") is False


def test_safe_extract_zip_rejects_duplicate_case_insensitive(tmp_path):
    archive = tmp_path / "dup.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("a.jsonl", "{}")
        bundle.writestr("A.jsonl", "{}")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out4")


def test_safe_extract_zip_rejects_size_limit(tmp_path):
    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("a.jsonl", "x" * 2048)
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(archive, tmp_path / "out5",
                                  max_unpacked_bytes=1024)


def test_safe_extract_zip_writes_normal_files(tmp_path):
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("train/samples.jsonl", "{}\n")
        bundle.writestr("manifest.json", "{}")
    written = io_utils.safe_extract_zip(archive, tmp_path / "out6")
    assert sorted(written) == ["manifest.json", "train/samples.jsonl"]
    assert (tmp_path / "out6" / "train" / "samples.jsonl").is_file()


# --------------------------------------------------------------------------- #
# 文件内容对象
# --------------------------------------------------------------------------- #

def test_pack_and_restore_file_object_roundtrip(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "26_samples.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (run_dir / "26_sample_list.xlsx").write_bytes(b"PK\x03\x04binary")
    payload = transport.pack_file_object(
        run_dir, "26_samples.jsonl", ["26_samples.jsonl", "26_sample_list.xlsx"])
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    kinds = {item["path"]: item["encoding"] for item in payload["files"]}
    assert kinds["26_samples.jsonl"] == "utf-8"
    assert kinds["26_sample_list.xlsx"] == "base64"

    dest = tmp_path / "restored"
    entry = transport.restore_file_object(payload, dest)
    assert entry == dest / "26_samples.jsonl"
    assert entry.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert (dest / "26_sample_list.xlsx").read_bytes() == b"PK\x03\x04binary"


def test_pack_file_object_rejects_missing_entrypoint(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "a.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "missing.jsonl", ["a.jsonl"])


def test_pack_file_object_rejects_traversal(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "../x.jsonl", ["../x.jsonl"])


def test_pack_file_object_enforces_size_limit(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "a.jsonl").write_text("x" * 4096, encoding="utf-8")
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(run_dir, "a.jsonl", ["a.jsonl"],
                                   max_response_bytes=128)
    assert (run_dir / "a.jsonl").read_text(encoding="utf-8") == "x" * 4096


def test_restore_rejects_bad_format_and_duplicate_paths(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({"format": "other", "files": []}, tmp_path)
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
               "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}"},
                         {"path": "A.jsonl", "encoding": "utf-8", "content": "{}"}]}
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "dup")


def test_manifest_build_and_load(tmp_path):
    manifest = transport.build_manifest("a.jsonl", [
        {"path": "a.jsonl", "role": "records"}, {"path": "manifest.json", "role": "sidecar"},
        {"path": "a.jsonl", "role": "records"}])
    assert [item["path"] for item in manifest["artifacts"]] == ["a.jsonl",
                                                                "manifest.json"]
    written = transport.write_manifest(tmp_path, manifest)
    assert transport.load_manifest(tmp_path)["entrypoint"] == "a.jsonl"
    assert written.is_file()


def test_resolve_entrypoint_prefers_manifest(tmp_path):
    (tmp_path / "b.jsonl").write_text("{}\n", encoding="utf-8")
    transport.write_manifest(tmp_path, transport.build_manifest(
        "b.jsonl", [{"path": "b.jsonl", "role": "records"}]))
    entry, relative = transport.resolve_entrypoint(tmp_path)
    assert relative == "b.jsonl" and entry.is_file()


def test_resolve_entrypoint_without_manifest(tmp_path):
    (tmp_path / "samples.jsonl").write_text("{}\n", encoding="utf-8")
    entry, relative = transport.resolve_entrypoint(tmp_path)
    assert relative == "samples.jsonl"


def test_resolve_entrypoint_ambiguous_is_rejected(tmp_path):
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "c.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)
