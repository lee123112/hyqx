"""传输适配层测试：文件内容对象、运行清单与入口定位。"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import json
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tests import fixtures  # noqa: E402
from utils import transport  # noqa: E402


def _pack(directory: Path, entrypoint: str, paths):
    return transport.pack_file_object(directory, entrypoint, paths)


# ---------------------------------------------------------------- 清单

def test_manifest_round_trip_and_dedup(tmp_path):
    (tmp_path / "07_candidates.jsonl").write_text("{}\n", encoding="utf-8")
    manifest = transport.build_manifest("07_candidates.jsonl", [
        {"path": "07_candidates.jsonl", "role": "main"},
        {"path": "07_candidates.jsonl", "role": "main"},        # 重复路径去重
        {"path": "manifest.json", "role": "sidecar"},           # 清单自身不入列
        {"path": "04_images/a.png", "role": "media"},
    ])
    assert manifest["format"] == transport.MANIFEST_FORMAT
    assert [item["path"] for item in manifest["artifacts"]] == [
        "07_candidates.jsonl", "04_images/a.png"]

    path = transport.write_manifest(tmp_path, manifest)
    assert path.name == "manifest.json"
    loaded = transport.load_manifest(tmp_path)
    assert loaded == manifest
    assert transport.resolve_entrypoint(tmp_path) == (
        tmp_path / "07_candidates.jsonl", "07_candidates.jsonl")


def test_manifest_invalid_format_rejected(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"format": "other-v1", "entrypoint": "x.jsonl"}), encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.load_manifest(tmp_path)


def test_resolve_entrypoint_without_manifest(tmp_path):
    """无 manifest 时只接受唯一 JSONL 候选；算子产物需靠 manifest 或显式路径。"""
    (tmp_path / "07_candidates.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "07_rejects.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)

    (tmp_path / "07_rejects.jsonl").unlink()
    path, relative = transport.resolve_entrypoint(tmp_path)
    assert relative == "07_candidates.jsonl"

    (tmp_path / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    path, relative = transport.resolve_entrypoint(tmp_path)
    assert relative == "demo.jsonl"  # 规范名优先


def test_resolve_entrypoint_inside_dot_directory(tmp_path):
    """上传暂存目录含点目录（如 ``<root>/.tmp/upload_*/content``）时仍能定位入口。

    回归：旧实现用绝对 ``path.parts`` 过滤隐藏文件，只要 base 自身落在点目录下就会把
    唯一候选一起过滤掉（只有带 manifest.json 的包会掩盖该缺陷）。
    """
    base = tmp_path / ".tmp" / "upload_abc" / "content"
    base.mkdir(parents=True)
    (base / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    path, relative = transport.resolve_entrypoint(base)
    assert relative == "demo.jsonl"
    assert path == base / "demo.jsonl"


# ---------------------------------------------------------------- 文件内容对象

def test_pack_and_restore_round_trip(tmp_path):
    """阶段末产物在全新目录恢复后，清单与全部媒体都必须可读。"""
    out = tmp_path / "out"
    fixtures.copy_sample_into(fixtures.upload_dir(out, "cm07_transport"))
    import inference

    result = inference.run(None, "cm07_transport", output_root=str(out))
    run_dir = Path(result["runDir"])
    payload = inference.stage_file_object(result)

    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "07_candidates.jsonl"
    encoded = {item["path"]: item["encoding"] for item in payload["files"]}
    assert encoded["07_candidates.jsonl"] == "utf-8"
    assert encoded["04_images/demo_07_a1_en.png"] == "base64"
    assert encoded["06_audio/demo_07_v1.wav"] == "base64"

    restore_dir = tmp_path / "restored"
    entry = transport.restore_file_object(payload, restore_dir)
    assert entry == restore_dir / "07_candidates.jsonl"

    records = [json.loads(line) for line in
               entry.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(records) == 5
    for record in records:
        for modality in ("image", "video", "audio"):
            value = record.get(modality)
            if isinstance(value, dict) and isinstance(value.get("path"), str):
                assert (restore_dir / value["path"]).is_file(), value["path"]
    # 字节完全一致（媒体搬运不重新编码）
    for name in ("04_images/demo_07_a1_en.png", "06_audio/demo_07_v1.wav"):
        assert transport.file_sha256(restore_dir / name) == \
            transport.file_sha256(run_dir / name)


def test_pack_requires_entrypoint_and_rejects_missing_file(tmp_path):
    (tmp_path / "07_candidates.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("x", encoding="utf-8")

    with pytest.raises(transport.TransportError):
        _pack(tmp_path, "07_candidates.jsonl", ["ok.txt"])       # 缺入口
    with pytest.raises(transport.TransportError):
        _pack(tmp_path, "07_candidates.jsonl", ["07_candidates.jsonl", "absent.txt"])
    with pytest.raises(transport.TransportError):
        _pack(tmp_path, "07_candidates.jsonl", ["../escape.txt"])


def test_output_too_large_is_not_truncated(tmp_path):
    (tmp_path / "07_candidates.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(
            tmp_path, "07_candidates.jsonl", ["07_candidates.jsonl"],
            max_response_bytes=10)


def test_encode_file_rejects_non_utf8_text(tmp_path):
    broken = tmp_path / "notes.txt"
    broken.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(transport.TransportError):
        transport.encode_file("notes.txt", broken)


@pytest.mark.parametrize("payload,message", [
    ({"format": "other"}, "format"),
    ({"format": transport.FILE_OBJECT_FORMAT, "files": []}, "files"),
    ({"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
      "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"},
                {"path": "A.jsonl", "encoding": "utf-8", "content": "{}\n"}]}, "重复"),
    ({"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
      "files": [{"path": "a.jsonl", "encoding": "hex", "content": "00"}]}, "编码"),
    ({"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
      "files": [{"path": "../a.jsonl", "encoding": "utf-8", "content": "{}\n"}]}, "路径"),
    ({"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "b.jsonl",
      "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"}]}, "入口"),
])
def test_restore_rejects_invalid_objects(tmp_path, payload, message):
    with pytest.raises(transport.TransportError) as excinfo:
        transport.restore_file_object(payload, tmp_path / "dest")
    assert excinfo.value.args[0]


def test_restore_requires_empty_destination(tmp_path):
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "occupied.txt").write_text("x", encoding="utf-8")
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
               "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"}]}
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, destination)
