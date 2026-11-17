"""传输层测试：``crossmodal-files-v1`` 文件对象、清单与安全解包。"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import io
import json
import zipfile

import pytest

from tests import fixtures
from utils import io as io_utils
from utils import transport


# --------------------------------------------------------------------------- #
# 文件内容对象
# --------------------------------------------------------------------------- #

def test_pack_and_restore_roundtrip(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "media").mkdir(parents=True)
    entry = run_dir / "17_local_annotations.jsonl"
    entry.write_text('{"sample_id": "s-1"}\n', encoding="utf-8")
    image = fixtures.png_bytes(8, 8)
    (run_dir / "media" / "demo.png").write_bytes(image)
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")

    payload = transport.pack_file_object(
        run_dir, entry.name,
        ["17_local_annotations.jsonl", "media/demo.png", "manifest.json"])
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "17_local_annotations.jsonl"
    encoded = {item["path"]: item for item in payload["files"]}
    assert encoded["17_local_annotations.jsonl"]["encoding"] == "utf-8"
    assert encoded["media/demo.png"]["encoding"] == "base64"

    restored = transport.restore_file_object(payload, tmp_path / "restored")
    assert restored.read_text(encoding="utf-8") == '{"sample_id": "s-1"}\n'
    assert (tmp_path / "restored" / "media" / "demo.png").read_bytes() == image


def test_restore_requires_empty_directory(tmp_path):
    entry_dir = tmp_path / "restored"
    entry_dir.mkdir(parents=True)
    (entry_dir / "leftover.txt").write_text("x", encoding="utf-8")
    payload = {
        "format": transport.FILE_OBJECT_FORMAT,
        "entrypoint": "a.jsonl",
        "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": ""}],
    }
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, entry_dir)


def test_pack_rejects_escape_and_missing(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "main.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "main.jsonl", ["../secret.jsonl"])
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "main.jsonl", ["main.jsonl", "absent.jsonl"])


def test_pack_enforces_max_response_bytes(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "main.jsonl").write_text("{" + "x" * 500 + "}\n", encoding="utf-8")
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(run_dir, "main.jsonl", ["main.jsonl"],
                                   max_response_bytes=64)


def test_pack_requires_entrypoint_in_files(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "other.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "main.jsonl", ["other.jsonl"])


def test_pack_rejects_non_utf8_text(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "main.jsonl").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "main.jsonl", ["main.jsonl"])


def test_restore_rejects_wrong_format_and_duplicates(tmp_path):
    with pytest.raises(transport.TransportError):
        transport.restore_file_object({"format": "other", "files": []}, tmp_path / "a")
    payload = {
        "format": transport.FILE_OBJECT_FORMAT,
        "entrypoint": "a.jsonl",
        "files": [
            {"path": "a.jsonl", "encoding": "utf-8", "content": ""},
            {"path": "A.jsonl", "encoding": "utf-8", "content": ""},
        ],
    }
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "b")


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #

def test_manifest_roundtrip_and_entrypoint(tmp_path):
    manifest = transport.build_manifest("main.jsonl", [
        {"path": "main.jsonl", "role": "main"},
        {"path": "17_rejects.jsonl", "role": "review"},
        {"path": "manifest.json", "role": "sidecar"},
    ])
    paths = [item["path"] for item in manifest["artifacts"]]
    assert transport.MANIFEST_NAME not in paths
    transport.write_manifest(tmp_path, manifest)
    loaded = transport.load_manifest(tmp_path)
    assert loaded["entrypoint"] == "main.jsonl"

    (tmp_path / "main.jsonl").write_text("{}\n", encoding="utf-8")
    entry, name = transport.resolve_entrypoint(tmp_path)
    assert entry.name == "main.jsonl"
    assert name == "main.jsonl"


def test_resolve_entrypoint_requires_uniqueness(tmp_path):
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)


# --------------------------------------------------------------------------- #
# 安全解压
# --------------------------------------------------------------------------- #

def test_safe_extract_rejects_zip_slip(tmp_path):
    payload = fixtures.zip_bytes({"../escape.jsonl": b"{}\n"})
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(io.BytesIO(payload), tmp_path / "out")
    assert not (tmp_path / "escape.jsonl").exists()


def test_safe_extract_rejects_absolute_and_duplicate(tmp_path):
    absolute = fixtures.zip_bytes({"/etc/passwd": b"x"})
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(io.BytesIO(absolute), tmp_path / "a")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("media/demo.png", b"1")
        archive.writestr("media/DEMO.png", b"2")
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(io.BytesIO(buffer.getvalue()), tmp_path / "b")


def test_safe_extract_enforces_limits(tmp_path):
    payload = fixtures.zip_bytes({"a.jsonl": b"{}\n", "b.jsonl": b"{}\n"})
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(io.BytesIO(payload), tmp_path / "a", max_files=1)
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(io.BytesIO(payload), tmp_path / "b",
                                  max_unpacked_bytes=1)


def test_safe_extract_requires_empty_destination(tmp_path):
    destination = tmp_path / "out"
    destination.mkdir(parents=True)
    (destination / "existing.txt").write_text("x", encoding="utf-8")
    payload = fixtures.zip_bytes({"a.jsonl": b"{}\n"})
    with pytest.raises(ValueError):
        io_utils.safe_extract_zip(io.BytesIO(payload), destination)


# --------------------------------------------------------------------------- #
# 路径与 uniqueId 安全
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["", "  ", ".", "..", "a/b", "a\\b", "C:", "CON",
                                 "aux.jsonl", "trailing.", "has\x00null"])
def test_validate_unique_id_rejects(bad):
    with pytest.raises(ValueError):
        io_utils.validate_unique_id(bad)


@pytest.mark.parametrize("good", ["task_1", "task_1_file_2", "T-9_f1", "a.b_f1"])
def test_validate_unique_id_accepts(good):
    assert io_utils.validate_unique_id(good) == good


def test_resolve_in_task_blocks_cross_task(tmp_path):
    task = tmp_path / "task_a"
    task.mkdir(parents=True)
    sibling = tmp_path / "task_b" / "input" / "demo.jsonl"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        io_utils.resolve_in_task(task, str(sibling))
    with pytest.raises(ValueError):
        io_utils.resolve_in_task(task, "../task_b/input/demo.jsonl")


def test_task_lock_blocks_second_writer(tmp_path):
    with io_utils.TaskLock(tmp_path, "task_locked"):
        with pytest.raises(io_utils.TaskBusyError):
            with io_utils.TaskLock(tmp_path, "task_locked"):
                pass
    # 释放后可以再次加锁
    with io_utils.TaskLock(tmp_path, "task_locked"):
        pass


def test_next_run_id_increments(tmp_path):
    operator_dir = tmp_path / "op"
    assert io_utils.next_run_id(operator_dir) == "run001"
    (operator_dir / "run001").mkdir(parents=True)
    (operator_dir / "run003").mkdir(parents=True)
    assert io_utils.next_run_id(operator_dir) == "run002"


def test_is_within_uses_resolve_not_prefix(tmp_path):
    root = tmp_path / "root"
    root.mkdir(parents=True)
    prefix_sibling = tmp_path / "root_sibling"
    prefix_sibling.mkdir(parents=True)
    assert io_utils.is_within(io_utils.resolved_absolute(root),
                              io_utils.resolved_absolute(root / "a")) is True
    assert io_utils.is_within(io_utils.resolved_absolute(root),
                              io_utils.resolved_absolute(prefix_sibling)) is False


def test_jsonl_writer_and_decoded_lines(tmp_path):
    path = tmp_path / "demo.jsonl"
    count = io_utils.write_jsonl(path, [{"a": 1}, {"a": 2}])
    assert count == 2
    lines = list(io_utils.iter_decoded_lines(path))
    assert lines == ['{"a": 1}', '{"a": 2}']

    bad = tmp_path / "bad.jsonl"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(UnicodeDecodeError):
        list(io_utils.iter_decoded_lines(bad))


def test_atomic_write_json_replaces(tmp_path):
    target = tmp_path / "lineage.json"
    io_utils.atomic_write_json(target, {"a": 1})
    io_utils.atomic_write_json(target, {"a": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
