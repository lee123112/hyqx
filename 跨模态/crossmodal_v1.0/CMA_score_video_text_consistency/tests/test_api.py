"""三接口测试（``POST /uploadFile`` / ``POST /predict`` / ``POST /removeFile``）。

使用 FastAPI ``TestClient``（真实 ASGI 应用，非 mock）。默认路线关闭，
因此本文件不需要本地 CLIP 权重；开启路线的降级路径单独验证。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from fixtures import (default_frames, default_media, make_flat_record,  # noqa: E402
                      make_pair, zip_bytes)


@pytest.fixture()
def client(tmp_path, monkeypatch, config_file):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("OPERATOR_CONFIG", str(config_file))
    return TestClient(app_module.app)


def payload_bytes(records) -> bytes:
    return "".join(json.dumps(item, ensure_ascii=False) + "\n"
                   for item in records).encode("utf-8")


def upload(client, unique_id, records, media=None, name="demo.jsonl"):
    members = {name: payload_bytes(records)}
    members.update(media or default_media())
    return client.post("/uploadFile?uniqueId=%s" % unique_id,
                       files={"file": ("bundle.zip", zip_bytes(members),
                                       "application/zip")})


def upload_single(client, unique_id, records, name="demo.jsonl"):
    return client.post("/uploadFile?uniqueId=%s" % unique_id,
                       files={"file": (name, payload_bytes(records), "application/jsonl")})


# --------------------------------------------------------------------------- #
# 信封与上传
# --------------------------------------------------------------------------- #

def test_upload_zip_returns_envelope(client):
    response = upload(client, "task_file", [make_flat_record("s-01", "描述", default_frames(2))])
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["entrypoint"] == "demo.jsonl"
    assert "frames/frame_0001.png" in body["data"]["files"]


def test_upload_single_file(client):
    response = upload_single(client, "task_file",
                             [make_flat_record("s-01", "描述", default_frames(2))])
    assert response.status_code == 200
    assert response.json()["code"] == 0


def test_upload_empty_content_is_rejected(client):
    response = client.post("/uploadFile?uniqueId=task_file",
                           files={"file": ("empty.jsonl", b"", "application/jsonl")})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_upload_zip_slip_is_rejected_without_writing(client, tmp_path):
    response = client.post(
        "/uploadFile?uniqueId=task_evil",
        files={"file": ("evil.zip", zip_bytes({"../escape.jsonl": b"{}"}),
                        "application/zip")})
    assert response.json()["code"] == 1
    assert not (tmp_path / "escape.jsonl").exists()


@pytest.mark.parametrize("unique_id", ["..", "a/b", "a\\b", "con", ""])
def test_upload_invalid_unique_id(client, unique_id):
    response = upload(client, unique_id, [make_flat_record("s-01", "描述", default_frames(2))])
    assert response.status_code == 422
    assert response.json()["code"] == 1


# --------------------------------------------------------------------------- #
# /predict 两种返回形态
# --------------------------------------------------------------------------- #

def test_predict_intermediate_returns_output_file_and_result(client):
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    upload(client, "task_file", records)
    response = client.post("/predict", json={"uniqueId": "task_file",
                                             "isLastOperator": False})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert body["data"]["status"] == "success"
    assert body["data"]["outputFile"]["format"] == "crossmodal-files-v1"
    assert body["data"]["result"]["entrypoint"] == "14_passthrough.jsonl"
    assert body["data"]["result"]["statistics"]["skipped_records"] == 1


def test_predict_last_operator_returns_file_object_and_result(client):
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    upload(client, "task_file", records)
    response = client.post("/predict", json={"uniqueId": "task_file",
                                             "isLastOperator": True})
    body = response.json()
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    output = body["data"]["outputFile"]
    assert output["format"] == "crossmodal-files-v1"
    assert output["entrypoint"] == "14_passthrough.jsonl"
    paths = {item["path"] for item in output["files"]}
    assert "14_passthrough.jsonl" in paths
    assert "manifest.json" in paths
    assert "14_sample_quarantine.jsonl" in paths
    assert "lineageFile" not in body["data"]
    assert "lineageFile" not in body["data"]["result"]


def test_predict_enabled_stage_file_object_carries_frames(client):
    """启用路线（本机无权重 → 显式降级）：阶段末文件对象搬运被引用抽帧。"""
    upload(client, "task_frames", [make_flat_record("s-f", "描述", default_frames(2))])
    response = client.post("/predict", json={
        "uniqueId": "task_frames", "isLastOperator": True,
        "config": {"enabled": True, "allow_degraded": True}})
    body = response.json()
    assert body["code"] == 0
    output = body["data"]["outputFile"]
    assert output["entrypoint"] == "14_video_text_scores.jsonl"
    paths = {item["path"] for item in output["files"]}
    assert any(path.startswith("frames/") for path in paths)
    assert "14_pending.jsonl" in paths
    assert body["data"]["result"]["statistics"]["frames_carried"] == 2


def test_predict_stage_start_without_input_file_uses_uploaded_entrypoint(client):
    """阶段首不传 inputFile：从任务输入目录解析入口。"""
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    upload_single(client, "task_file", records)
    response = client.post("/predict", json={"uniqueId": "task_file",
                                             "isLastOperator": False})
    assert response.json()["code"] == 0
    assert response.json()["data"]["result"]["statistics"]["input_records"] == 1


@pytest.mark.parametrize("is_last", ["false", "true", 1, None])
def test_predict_requires_strict_bool(client, is_last):
    upload(client, "task_file", [make_flat_record("s-01", "描述", default_frames(2))])
    response = client.post("/predict", json={"uniqueId": "task_file",
                                             "isLastOperator": is_last})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_predict_missing_unique_id_is_rejected(client):
    response = client.post("/predict", json={"isLastOperator": False})
    assert response.status_code == 422


def test_predict_input_file_outside_task_is_rejected(client, tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(make_flat_record("s-01", "描述", default_frames(2))),
                       encoding="utf-8")
    upload(client, "task_file", [make_flat_record("s-01", "描述", default_frames(2))])
    response = client.post("/predict", json={"uniqueId": "task_file",
                                            "isLastOperator": False,
                                            "inputFile": str(outside)})
    assert response.status_code == 400
    assert response.json()["code"] == 1
    assert "InputError" in response.json()["msg"]


def test_predict_unknown_config_key_is_rejected(client):
    upload(client, "task_file", [make_flat_record("s-01", "描述", default_frames(2))])
    response = client.post("/predict", json={"uniqueId": "task_file",
                                            "isLastOperator": False,
                                            "config": {"clip_model": "other"}})
    assert response.status_code == 400
    assert "ConfigError" in response.json()["msg"]


# --------------------------------------------------------------------------- #
# 启用路线的降级 / 失败（本机无 CLIP 权重）
# --------------------------------------------------------------------------- #

def test_enabled_route_without_weights_fails_explicitly(client):
    upload(client, "task_file", [make_flat_record("s-01", "描述", default_frames(2))])
    response = client.post("/predict", json={"uniqueId": "task_file",
                                            "isLastOperator": False,
                                            "config": {"enabled": True}})
    assert response.status_code == 400
    assert response.json()["code"] == 1
    assert "ModelUnavailable" in response.json()["msg"]


def test_enabled_route_with_degrade_returns_no_scores(client):
    upload(client, "task_file", [make_flat_record("s-01", "描述", default_frames(2))])
    response = client.post("/predict", json={
        "uniqueId": "task_file", "isLastOperator": False,
        "config": {"enabled": True, "allow_degraded": True}})
    body = response.json()
    assert body["code"] == 0
    stats = body["data"]["result"]["statistics"]
    assert stats["degraded"] is True
    assert stats["model_unavailable"] == 1
    assert stats["passed_records"] == 0


# --------------------------------------------------------------------------- #
# 任务隔离 / 清理 / 未注册路径
# --------------------------------------------------------------------------- #

def test_two_unique_ids_are_isolated(client):
    upload(client, "task_a", [make_flat_record("s-a", "描述A", default_frames(2))])
    upload(client, "task_b", [make_flat_record("s-b1", "描述B", default_frames(2)),
                              make_flat_record("s-b2", "描述B2", default_frames(2))])
    first = client.post("/predict", json={"uniqueId": "task_a", "isLastOperator": False})
    second = client.post("/predict", json={"uniqueId": "task_b", "isLastOperator": False})
    assert first.json()["data"]["result"]["statistics"]["input_records"] == 1
    assert second.json()["data"]["result"]["statistics"]["input_records"] == 2
    assert first.json()["data"]["result"]["uniqueId"] == "task_a"
    assert second.json()["data"]["result"]["uniqueId"] == "task_b"


def test_remove_file_is_idempotent_and_scoped(client, tmp_path):
    upload(client, "task_a", [make_flat_record("s-a", "描述A", default_frames(2))])
    upload(client, "task_b", [make_flat_record("s-b", "描述B", default_frames(2))])
    first = client.post("/removeFile?uniqueId=task_a")
    second = client.post("/removeFile?uniqueId=task_a")
    assert first.status_code == 200 and first.json()["data"]["removed"] is True
    assert second.status_code == 200 and second.json()["data"]["removed"] is False
    output_root = Path(app_module.output_root())
    assert not (output_root / "task_a").exists()
    assert (output_root / "task_b").exists()


def test_download_file_is_not_registered(client):
    assert client.get("/downloadFile?uniqueId=task_a").status_code == 404
    assert client.post("/downloadFile?uniqueId=task_a").status_code == 404


def test_predict_without_upload_is_rejected(client):
    response = client.post("/predict", json={"uniqueId": "task_missing",
                                            "isLastOperator": False})
    assert response.status_code == 400
    assert "InputError" in response.json()["msg"]


def test_form2_detection_operator_id_conflict_rejected(client):
    """[D3] 形态二入口：detection 与 operatorId 同时出现且归一化后不一致 → 参数错误。"""
    response = client.post("/predict", params={"detection": "m14", "operatorId": "m15"},
                           json={"uniqueId": "x", "isLastOperator": False})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1 and "必须一致" in body["msg"]
