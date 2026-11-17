"""媒体与血缘可搬运验收（DEVELOPMENT_CONTRACT §4、§6.1、§6.2）。

覆盖：
- 中间血缘里的临时 ``outputFile`` 能继续作为同一任务内下一次 ``/predict`` 的 ``inputFile``；
- 阶段末文件内容对象恢复到全新空目录后，记录引用的媒体逐字节一致；
- 文件对象路径均为包内相对安全路径，阶段末血缘不泄漏本容器绝对路径；
- 打包/恢复对越界路径、缺失文件、重复路径、非空目标与字节超限明确失败。
"""
from __future__ import annotations

# [v4.2] 血缘/运输输出已停用：本测试保留存档、不再执行。
import pytest

pytestmark = pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")

import json
from pathlib import Path

import pytest

from utils import transport

from conftest import read_jsonl

UID = "demoS2_f1"
ENTRY = "demo.jsonl"


def _predict(client, uid=UID, last=False, input_file=None):
    body = {"uniqueId": uid, "isLastOperator": last,"config":{"enabled":True}}
    if input_file is not None:
        body["inputFile"] = input_file
    return client.post("/predict", json=body)


@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用：中间血缘临时路径传递形态已停用（代码注释保留、暂不删除）")
def test_intermediate_lineage_path_feeds_next_predict(client, output_root, stage):
    stage(output_root, UID, ENTRY)
    first = _predict(client, last=False)
    assert first.status_code == 200, first.text
    node = first.json()["data"]["lineageFile"]
    intermediate = Path(node["outputFile"])
    assert intermediate.is_file()
    assert intermediate.name == "03_text_cleaned.jsonl"

    # 中间血缘的临时输出路径可作为下一次调用的 inputFile（阶段内传递）
    second = _predict(client, last=True, input_file=str(intermediate))
    assert second.status_code == 200, second.text
    data = second.json()["data"]
    assert data["outputFile"]["entrypoint"] == "03_text_cleaned.jsonl"
    assert data["lineageFile"][0]["inputFile"] == str(intermediate)


def test_stage_end_object_restores_media_and_matches_source(client, output_root, stage,
                                                            sample_dir, tmp_path):
    stage(output_root, UID, ENTRY)
    response = _predict(client, last=True)
    assert response.status_code == 200, response.text
    payload = response.json()["data"]["outputFile"]

    restored = tmp_path / "restored"
    entry = transport.restore_file_object(payload, restored)
    records = read_jsonl(entry)
    assert len(records) == 7

    media_checked = 0
    for record in records:
        for field in ("image", "video", "audio"):
            value = record.get(field)
            if isinstance(value, str):
                target = restored / value
                assert target.is_file(), value
                assert (transport.file_sha256(target)
                        == transport.file_sha256(sample_dir / value)), value
                media_checked += 1
    assert media_checked >= 1

    # 旁路清单与自描述清单都在包内
    files = {item["path"]: item for item in payload["files"]}
    assert {"03_text_cleaned.jsonl", "03_rejects.jsonl", "manifest.json"} <= set(files)
    manifest = json.loads(files["manifest.json"]["content"])
    assert manifest["format"] == transport.MANIFEST_FORMAT
    assert manifest["entrypoint"] == "03_text_cleaned.jsonl"
    assert all(item["encoding"] == "utf-8"
               for item in payload["files"] if item["path"].endswith(".jsonl"))
    assert all(item["encoding"] == "base64"
               for item in payload["files"] if item["path"].startswith("media/"))


def test_file_object_paths_are_pack_relative(client, output_root, stage):
    stage(output_root, UID, ENTRY)
    data = _predict(client, last=True).json()["data"]
    for item in data["outputFile"]["files"]:
        assert "\\" not in item["path"]
        path = Path(item["path"])
        assert not path.is_absolute()
        assert ".." not in path.parts
    # [v4.2] 血缘输出已停用：响应不再包含 lineageFile
    assert "lineageFile" not in data


def test_pack_rejects_escaping_and_missing_paths(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "03_text_cleaned.jsonl").write_text("{}\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    for bad in ("../outside.txt", str(outside), "/abs.jsonl", "media/../../outside.txt"):
        with pytest.raises(transport.TransportError):
            transport.pack_file_object(run_dir, "03_text_cleaned.jsonl",
                                       ["03_text_cleaned.jsonl", bad], 1 << 20)
    with pytest.raises(transport.TransportError):
        transport.pack_file_object(run_dir, "03_text_cleaned.jsonl", ["missing.jsonl"], 1 << 20)


def test_pack_enforces_response_byte_limit(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "03_text_cleaned.jsonl").write_text('{"content": "x"}\n' * 200,
                                                  encoding="utf-8")
    with pytest.raises(transport.OutputTooLargeError):
        transport.pack_file_object(run_dir, "03_text_cleaned.jsonl",
                                   ["03_text_cleaned.jsonl"], 256)


def test_restore_rejects_non_empty_destination_and_duplicate_paths(tmp_path):
    payload = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
               "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"}]}
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(payload, occupied)

    duplicate = {"format": transport.FILE_OBJECT_FORMAT, "entrypoint": "a.jsonl",
                 "files": [{"path": "a.jsonl", "encoding": "utf-8", "content": "{}\n"},
                           {"path": "A.jsonl", "encoding": "utf-8", "content": "{}\n"}]}
    with pytest.raises(transport.TransportError):
        transport.restore_file_object(duplicate, tmp_path / "dup")
