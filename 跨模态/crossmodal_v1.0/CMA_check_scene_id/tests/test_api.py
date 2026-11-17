"""HTTP 三接口测试（TestClient）：信封、两种 isLastOperator 形态、隔离与安全。

正式接口只有 /uploadFile、/predict、/removeFile；/downloadFile 必须 404。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from utils import transport
from tests.support import count_lines, read_jsonl, zip_directory


def envelope(response):
    body = response.json()
    assert set(body) == {"code", "msg", "data"}
    return body


# ------------------------------------------------------------------- 上传
def test_upload_returns_entrypoint_and_file_count(upload):
    body = envelope(upload("api_task_a"))
    assert body["code"] == 0
    assert body["data"]["uniqueId"] == "api_task_a"
    assert body["data"]["entrypoint"] == "demo.jsonl"
    assert body["data"]["files"] >= 10
    assert Path(body["data"]["inputFile"]).is_file()


def test_upload_rejects_zip_slip(client, tmp_path):
    import zipfile

    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(str(archive), "w") as handle:
        handle.writestr("../evil.jsonl", "{}\n")
    with open(str(archive), "rb") as stream:
        response = client.post("/uploadFile", params={"uniqueId": "evil_task"},
                               files={"file": ("bundle.zip", stream, "application/zip")})
    body = envelope(response)
    assert body["code"] == 1
    assert response.status_code == 400


def test_upload_rejects_illegal_unique_id(client, sample_dir, tmp_path):
    archive = tmp_path / "ok.zip"
    zip_directory(sample_dir, archive)
    with open(str(archive), "rb") as stream:
        response = client.post("/uploadFile", params={"uniqueId": "../escape"},
                               files={"file": ("bundle.zip", stream, "application/zip")})
    assert envelope(response)["code"] == 1
    assert response.status_code == 422


# --------------------------------------------------------------- 阶段内中间
def test_predict_stage_first_omits_input_file(upload, client):
    upload("api_task_b")
    response = client.post("/predict", json={"uniqueId": "api_task_b",
                                             "isLastOperator": False})
    body = envelope(response)
    assert body["code"] == 0
    # [v4.2] 形态二统一响应：operator/status/outputFile/result，去 lineageFile
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert body["data"]["operator"] == "scene_id_verification"
    node = body["data"]["result"]
    assert node["uniqueId"] == "api_task_b"
    assert body["data"]["outputFile"]["format"] == "crossmodal-files-v1"
    assert node["statistics"]["conservation_ok"] is True


def test_predict_intermediate_only_returns_lineage(upload, client):
    upload("api_task_c")
    body = envelope(client.post("/predict", json={
        "uniqueId": "api_task_c", "isLastOperator": False,
        "config": {"enabled": True}}))
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in body["data"]
    assert body["data"]["result"]["statistics"]["scene_groups_total"] == 4


def test_predict_chained_input_file_continues_stage(upload, client, output_root):
    upload("api_task_d")
    first = envelope(client.post("/predict", json={
        "uniqueId": "api_task_d", "isLastOperator": False,
        "config": {"enabled": True}}))
    produced = sorted(output_root.rglob("02_scene_verified.jsonl"))
    assert produced
    previous = str(produced[-1])
    second = envelope(client.post("/predict", json={
        "uniqueId": "api_task_d", "isLastOperator": False, "inputFile": previous,
        "config": {"enabled": True}}))
    assert second["code"] == 0
    assert second["data"]["result"]["runId"] != first["data"]["result"]["runId"]


def test_predict_without_upload_and_without_input_file(upload, client):
    response = client.post("/predict", json={"uniqueId": "never_uploaded",
                                             "isLastOperator": False})
    body = envelope(response)
    assert body["code"] == 1
    assert response.status_code == 400


# --------------------------------------------------------------- 阶段末
def test_predict_stage_last_returns_file_object_and_stage_lineage(upload, client):
    upload("api_task_e")
    body = envelope(client.post("/predict", json={
        "uniqueId": "api_task_e", "isLastOperator": True,
        "config": {"enabled": True}}))
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    payload = body["data"]["outputFile"]
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["entrypoint"] == "02_scene_verified.jsonl"
    assert "lineageFile" not in body["data"]
    assert body["data"]["result"]["uniqueId"] == "api_task_e"


def test_stage_last_file_object_restores_all_referenced_media(upload, client, tmp_path):
    upload("api_task_f")
    body = envelope(client.post("/predict", json={
        "uniqueId": "api_task_f", "isLastOperator": True,
        "config": {"enabled": True}}))
    payload = body["data"]["outputFile"]
    restored = transport.restore_file_object(payload, tmp_path / "restored")
    assert restored.name == "02_scene_verified.jsonl"
    records = read_jsonl(restored)
    assert len(records) == 11
    referenced = set()
    for record in records:
        for name in ("image", "audio", "video"):
            value = record.get(name)
            path = value.get("path") if isinstance(value, dict) else value
            if isinstance(path, str) and path.startswith("media/"):
                referenced.add(path)
    # 存在的媒体必须随包恢复到同一相对路径
    for path in referenced:
        if path == "media/img_missing.png":
            continue
        assert (restored.parent / path).is_file(), path
    # 源文件缺失的媒体只记数，绝不伪造空文件
    assert not (restored.parent / "media" / "img_missing.png").exists()
    assert body["data"]["result"]["statistics"]["media_missing"] == 1
    assert (restored.parent / "manifest.json").is_file()


# --------------------------------------------------------------- 参数校验
@pytest.mark.parametrize("payload", [
    {"isLastOperator": False},
    {"uniqueId": "api_task_g"},
    {"uniqueId": "api_task_g", "isLastOperator": "false"},
    {"uniqueId": "api_task_g", "isLastOperator": 0},
    {"uniqueId": "api_task_g", "isLastOperator": False, "unknown": 1},
])
def test_predict_rejects_invalid_request_body(client, payload):
    response = client.post("/predict", json=payload)
    body = envelope(response)
    assert body["code"] == 1
    assert response.status_code == 422


def test_predict_rejects_illegal_unique_id(client):
    response = client.post("/predict", json={"uniqueId": "a/b", "isLastOperator": False})
    assert envelope(response)["code"] == 1
    assert response.status_code == 422


def test_predict_rejects_readonly_config_override(upload, client):
    upload("api_task_h")
    response = client.post("/predict", json={
        "uniqueId": "api_task_h", "isLastOperator": False,
        "config": {"bind_once": False}})
    body = envelope(response)
    assert body["code"] == 1
    assert response.status_code == 200
    assert "配置" in body["msg"]


def test_predict_rejects_unknown_config_key(upload, client):
    upload("api_task_i")
    response = client.post("/predict", json={
        "uniqueId": "api_task_i", "isLastOperator": False, "config": {"nope": 1}})
    assert envelope(response)["code"] == 1


def test_predict_rejects_out_of_range_min_modalities(upload, client):
    upload("api_task_j")
    response = client.post("/predict", json={
        "uniqueId": "api_task_j", "isLastOperator": False,
        "config": {"min_modalities": 9}})
    assert envelope(response)["code"] == 1


def test_predict_rejects_cross_task_input_file(upload, client, output_root):
    upload("api_task_k")
    upload("api_task_l")
    envelope(client.post("/predict", json={
        "uniqueId": "api_task_k", "isLastOperator": False, "config": {"enabled": True}}))
    produced = sorted(output_root.rglob("02_scene_verified.jsonl"))
    assert produced
    foreign = str(produced[-1])
    response = client.post("/predict", json={
        "uniqueId": "api_task_l", "isLastOperator": False, "inputFile": foreign})
    body = envelope(response)
    assert body["code"] == 1
    assert response.status_code == 400


def test_predict_rejects_traversal_input_file(upload, client):
    upload("api_task_m")
    response = client.post("/predict", json={
        "uniqueId": "api_task_m", "isLastOperator": False,
        "inputFile": "../../secret.jsonl"})
    assert envelope(response)["code"] == 1


# --------------------------------------------------------------- 任务隔离
def test_two_unique_ids_do_not_pollute_each_other(upload, client, output_root):
    upload("iso_task_1")
    upload("iso_task_2")
    first = envelope(client.post("/predict", json={
        "uniqueId": "iso_task_1", "isLastOperator": False,
        "config": {"enabled": True, "scene_id_source": "generate"}}))
    second = envelope(client.post("/predict", json={
        "uniqueId": "iso_task_2", "isLastOperator": False}))
    assert first["data"]["result"]["statistics"]["supplemented_scene_ids"] == 4
    assert second["data"]["result"]["statistics"]["supplemented_scene_ids"] == 0
    assert (output_root / "iso_task_1").is_dir()
    assert (output_root / "iso_task_2").is_dir()
    assert second["data"]["result"]["uniqueId"] == "iso_task_2"


def test_download_file_is_not_registered(client):
    response = client.post("/downloadFile", params={"uniqueId": "x_f1"})
    body = envelope(response)
    assert response.status_code == 404
    assert body["code"] == 1


def test_openapi_exposes_exactly_three_business_paths(client):
    payload = client.get("/openapi.json").json()
    assert set(payload["paths"]) == {"/uploadFile", "/predict", "/removeFile"}


# --------------------------------------------------------------- 清理
def test_remove_file_is_idempotent(upload, client, output_root):
    upload("clean_task")
    first = envelope(client.post("/removeFile", params={"uniqueId": "clean_task"}))
    assert first["code"] == 0
    assert first["data"]["removed"] is True
    assert not (output_root / "clean_task").exists()
    second = envelope(client.post("/removeFile", params={"uniqueId": "clean_task"}))
    assert second["code"] == 0
    assert second["data"]["removed"] is False


def test_remove_file_does_not_touch_other_tasks(upload, client, output_root):
    upload("keep_task")
    upload("drop_task")
    envelope(client.post("/removeFile", params={"uniqueId": "drop_task"}))
    assert (output_root / "keep_task").is_dir()
    assert not (output_root / "drop_task").exists()


def test_remove_file_rejects_illegal_unique_id(client):
    response = client.post("/removeFile", params={"uniqueId": "../escape"})
    assert envelope(response)["code"] == 1
    assert response.status_code == 422


def test_remove_file_does_not_delete_operator_sources(client, operator_dir):
    client.post("/removeFile", params={"uniqueId": "whatever_f1"})
    assert (operator_dir / "inference.py").is_file()
    assert (operator_dir / "configs" / "config.yaml").is_file()


# --------------------------------------------------------------- 业务结果
def test_predict_records_real_scene_verification(upload, client):
    upload("api_task_n")
    body = envelope(client.post("/predict", json={
        "uniqueId": "api_task_n", "isLastOperator": True,
        "config": {"enabled": True, "min_modalities": 2}}))
    files = {item["path"]: item["content"] for item in body["data"]["outputFile"]["files"]}
    records = [line for line in files["02_scene_verified.jsonl"].splitlines() if line.strip()]
    assert len(records) == 11
    groups = [line for line in files["02_scene_groups.jsonl"].splitlines() if line.strip()]
    assert len(groups) == 4
    pairing = [line for line in files["02_routed_to_pairing.jsonl"].splitlines() if line.strip()]
    assert len(pairing) == 7
    assert body["data"]["result"]["statistics"]["conservation_ok"] is True
    assert body["data"]["result"]["statistics"]["media_carried"] == 10


# ------------------------------------------------------------------- 健康检查
def test_health_reports_identity(client):
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["operator"] == "scene_id_verification"
    assert body["operatorNumber"] == 2
    assert body["stageId"] == "S1"
    assert body["interfaces"] == ["/uploadFile", "/predict", "/removeFile"]
    assert body["downloadFile"] is False
    assert body["model"] is None


def test_form2_detection_operator_id_conflict_rejected(client):
    """[D3] 形态二入口：detection 与 operatorId 同时出现且归一化后不一致 → 参数错误。"""
    response = client.post("/predict", params={"detection": "m02", "operatorId": "m03"},
                           json={"uniqueId": "x", "isLastOperator": False})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1 and "必须一致" in body["msg"]
