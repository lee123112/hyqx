"""传输适配层与血缘可搬运性测试（DEVELOPMENT_CONTRACT §3/§4）。

不依赖业务算法，直接验证：
- ``crossmodal-files-v1`` 内容对象的编码、打包与严格恢复；
- ``crossmodal-manifest-v1`` 清单构造与入口定位；
- 路径逃逸/重复/缺失等越界拒绝；
- 阶段末血缘剥离临时绝对路径，中间血缘保留路径。
"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import json
from pathlib import Path

import pytest

from utils import transport
from utils.lineage import to_stage_lineage


def _write_payload(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "04_image_index.jsonl").write_text(
        json.dumps({"sample_id": "s1", "image": {"path": "04_images/s1.png"}},
                   ensure_ascii=False) + "\n",
        encoding="utf-8")
    (root / "04_images").mkdir(exist_ok=True)
    (root / "04_images" / "s1.png").write_bytes(b"\x89PNG\r\n\x1a\n fake-bytes")
    manifest = transport.build_manifest("04_image_index.jsonl", [
        {"path": "04_image_index.jsonl", "role": "main"},
        {"path": "04_images/s1.png", "role": "media"},
    ])
    transport.write_manifest(root, manifest)


# --------------------------------------------------------------------------- #
# 编码与内容对象
# --------------------------------------------------------------------------- #

def test_encode_text_uses_utf8_and_binary_uses_base64(tmp_path):
    text_path = tmp_path / "a.jsonl"
    text_path.write_text("示例 line\n", encoding="utf-8")
    encoded_text = transport.encode_file("a.jsonl", text_path)
    assert encoded_text["encoding"] == "utf-8"
    assert "示例" in encoded_text["content"]

    bin_path = tmp_path / "b.png"
    bin_path.write_bytes(bytes(range(256)))
    encoded_bin = transport.encode_file("b.png", bin_path)
    assert encoded_bin["encoding"] == "base64"
    import base64

    assert base64.b64decode(encoded_bin["content"]) == bytes(range(256))


def test_pack_and_restore_roundtrip_is_lossless(tmp_path):
    source = tmp_path / "run"
    _write_payload(source)
    manifest = transport.load_manifest(source)
    paths = transport.iter_manifest_paths(manifest) + [transport.MANIFEST_NAME]

    payload = transport.pack_file_object(source, "04_image_index.jsonl", paths)
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == "04_image_index.jsonl"

    restored = tmp_path / "restored"
    entry = transport.restore_file_object(payload, restored)
    assert entry == restored / "04_image_index.jsonl"
    assert (restored / "04_images" / "s1.png").read_bytes() == \
        (source / "04_images" / "s1.png").read_bytes()
    assert json.loads((restored / "manifest.json").read_text(encoding="utf-8"))["format"] == \
        transport.MANIFEST_FORMAT


def test_pack_requires_entrypoint_in_files(tmp_path):
    source = tmp_path / "run"
    _write_payload(source)
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(source, "04_image_index.jsonl", ["04_images/s1.png"])


def test_pack_raises_output_too_large_without_truncation(tmp_path):
    source = tmp_path / "run"
    _write_payload(source)
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(
            source, "04_image_index.jsonl", ["04_image_index.jsonl"], max_response_bytes=10)


def test_restore_rejects_non_empty_destination(tmp_path):
    source = tmp_path / "run"
    _write_payload(source)
    payload = transport.pack_file_object(
        source, "04_image_index.jsonl", ["04_image_index.jsonl"])
    destination = tmp_path / "occupied"
    destination.mkdir()
    (destination / "existing.txt").write_text("x", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, destination)


def test_restore_rejects_absolute_or_escape_paths(tmp_path):
    payload = {
        "format": transport.FILE_OBJECT_FORMAT,
        "entrypoint": "04_image_index.jsonl",
        "files": [
            {"path": "../escape.jsonl", "encoding": "utf-8", "content": ""},
            {"path": "04_image_index.jsonl", "encoding": "utf-8", "content": "{}"},
        ],
    }
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "restored")


def test_restore_rejects_duplicate_paths(tmp_path):
    payload = {
        "format": transport.FILE_OBJECT_FORMAT,
        "entrypoint": "a.jsonl",
        "files": [
            {"path": "a.jsonl", "encoding": "utf-8", "content": ""},
            {"path": "A.jsonl", "encoding": "utf-8", "content": ""},
        ],
    }
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, tmp_path / "restored")


# --------------------------------------------------------------------------- #
# 清单与入口定位
# --------------------------------------------------------------------------- #

def test_manifest_dedups_and_rejects_absolute_paths():
    manifest = transport.build_manifest("main.jsonl", [
        {"path": "main.jsonl", "role": "main"},
        {"path": "main.jsonl", "role": "main"},
        {"path": "media/a.png", "role": "media"},
    ])
    assert [item["path"] for item in manifest["artifacts"]] == ["main.jsonl", "media/a.png"]
    with pytest.raises(transport.TransportError):
        transport.build_manifest("main.jsonl", [{"path": "/abs/x.jsonl"}])
    with pytest.raises(transport.TransportError):
        transport.build_manifest("main.jsonl", [{"path": "../x.jsonl"}])


def test_resolve_entrypoint_prefers_manifest(tmp_path):
    _write_payload(tmp_path)
    entry, relative = transport.resolve_entrypoint(tmp_path)
    assert entry == tmp_path / "04_image_index.jsonl"
    assert relative == "04_image_index.jsonl"


def test_resolve_entrypoint_autodetects_single_jsonl(tmp_path):
    (tmp_path / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    entry, relative = transport.resolve_entrypoint(tmp_path)
    assert entry.name == "demo.jsonl" and relative == "demo.jsonl"


def test_resolve_entrypoint_rejects_ambiguous(tmp_path):
    (tmp_path / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.resolve_entrypoint(tmp_path)


def test_collect_payload_files_rejects_missing_and_escape(tmp_path):
    _write_payload(tmp_path)
    with pytest.raises(transport.TransportError):
        transport.collect_payload_files(tmp_path, ["nope.png"])
    with pytest.raises(transport.TransportError):
        transport.collect_payload_files(tmp_path, ["../../etc/passwd"])


# --------------------------------------------------------------------------- #
# 阶段血缘视图
# --------------------------------------------------------------------------- #

@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用：阶段血缘视图不再产出")
def test_to_stage_lineage_strips_temp_paths_and_keeps_identity():
    records = [{
        "operator": "image_standardization",
        "operator_version": "v1.0.0",
        "uniqueId": "demoS2_f1",
        "stageId": "S2",
        "inputFile": "/data/demoS2_f1/input/03_text_cleaned.jsonl",
        "outputFile": "G:/abs/path/run001/04_image_index.jsonl",
        "status": "success",
        "artifacts": [{"path": "G:/abs/path/run001/04_images/s1.png", "role": "media"}],
    }]
    staged = to_stage_lineage(records)
    assert "outputFile" not in staged[0]
    assert staged[0]["operator"] == "image_standardization"
    assert staged[0]["inputFile"].startswith("/data/")
    assert staged[0]["artifacts"][0]["path"] == "s1.png"
    # 不修改原记录（本地快照仍保留临时路径）
    assert records[0]["outputFile"].endswith("04_image_index.jsonl")
