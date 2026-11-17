"""HTTP 三接口测试（表1 #18）：信封、阶段中间/末返回形态、任务隔离与路径安全。"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from conftest import StubEncoder, box, png_bytes, record  # noqa: E402

import app as app_module  # noqa: E402


STUB = StubEncoder(text_angles={"金毛犬": 0.0, "草地": 0.0},
                   box_angles={"b1": 0.0, "b2": 0.0})


@pytest.fixture()
def client(tmp_path, monkeypatch, enabled_config_file):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("OPERATOR_CONFIG", str(enabled_config_file))
    app_module.set_encoder_provider(lambda unique_id, overrides: STUB)
    from fastapi.testclient import TestClient
    yield TestClient(app_module.app)
    app_module.set_encoder_provider(None)


def make_zip(records, media=True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in records)
        archive.writestr("demo.jsonl", payload + "\n")
        if media:
            archive.writestr("media/shot_001.png", png_bytes())
    return buffer.getvalue()


def upload(client, unique_id, payload):
    return client.post("/uploadFile?uniqueId=%s" % unique_id,
                       files={"file": ("bundle.zip", payload, "application/zip")})


def predict(client, unique_id, is_last=False, **extra):
    body = {"uniqueId": unique_id, "isLastOperator": is_last}
    body.update(extra)
    return client.post("/predict", json=body)


# --------------------------------------------------------------------------- #
# 信封与上传
# --------------------------------------------------------------------------- #

def test_upload_returns_envelope(client):
    payload = make_zip([record("s1", text="金毛犬 草地", regions=[box("b1"), box("b2")])])
    response = upload(client, "task_a", payload)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["uniqueId"] == "task_a"
    assert body["data"]["entrypoint"] == "demo.jsonl"
    assert "media/shot_001.png" in body["data"]["mediaFiles"]


def test_upload_rejects_invalid_unique_id(client):
    response = upload(client, "../escape", make_zip([record("s1")]))
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_upload_rejects_empty_body(client):
    response = client.post("/uploadFile?uniqueId=task_a",
                           files={"file": ("bundle.zip", b"", "application/zip")})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_upload_rejects_zip_slip(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.jsonl", "{}\n")
    response = upload(client, "task_slip", buffer.getvalue())
    assert response.status_code == 422
    assert response.json()["code"] == 1


# --------------------------------------------------------------------------- #
# /predict 返回形态
# --------------------------------------------------------------------------- #

def test_predict_intermediate_returns_output_file(client):
    payload = make_zip([record("s1", text="金毛犬 草地", regions=[box("b1"), box("b2")])])
    upload(client, "task_mid", payload)
    response = predict(client, "task_mid", is_last=False)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    # [v4.2] 形态二统一响应：非阶段末也返回 outputFile（crossmodal-files-v1），去 lineageFile
    assert set(data.keys()) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in data
    assert data["operator"] == "phrase_region_annotation"
    assert data["status"] == "success"
    assert data["outputFile"]["format"] == "crossmodal-files-v1"
    assert data["result"]["uniqueId"] == "task_mid"
    assert data["result"]["statistics"]["prefilled_records"] == 1


def test_predict_stage_end_returns_file_object(client):
    payload = make_zip([record("s1", text="金毛犬 草地", regions=[box("b1"), box("b2")])])
    upload(client, "task_end", payload)
    response = predict(client, "task_end", is_last=True)
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert set(data.keys()) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in data
    file_object = data["outputFile"]
    assert file_object["format"] == "crossmodal-files-v1"
    assert file_object["entrypoint"] == "18_phrase_region_annotations.jsonl"
    paths = {item["path"] for item in file_object["files"]}
    assert "18_phrase_region_annotations.jsonl" in paths
    assert "manifest.json" in paths
    assert any(path.startswith("media/") for path in paths)


def test_predict_disabled_route_passthrough(client, tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_CONFIG",
                       str(Path(__file__).resolve().parents[1]
                           / "configs" / "config.yaml"))
    payload = make_zip([record("s1", text="金毛犬", regions=[box("b1")])])
    upload(client, "task_off", payload)
    response = predict(client, "task_off", is_last=True)
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["outputFile"]["entrypoint"] == "18_passthrough.jsonl"
    rows = [item for item in body["data"]["outputFile"]["files"]
            if item["path"].endswith(".jsonl")]
    node = json.loads(rows[0]["content"])["processing"]["18"]
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    assert node["process_status"] == "skipped"
    assert node["status"] == 1


def test_predict_requires_upload_first(client):
    response = predict(client, "task_missing", is_last=False)
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_rejects_non_boolean_is_last_operator(client):
    payload = make_zip([record("s1", regions=[box("b1")])])
    upload(client, "task_bool", payload)
    response = client.post("/predict",
                           json={"uniqueId": "task_bool", "isLastOperator": "false"})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_predict_rejects_cross_task_input_file(client):
    payload = make_zip([record("s1", regions=[box("b1")])])
    upload(client, "task_x", payload)
    upload(client, "task_y", payload)
    response = predict(client, "task_y", is_last=False,
                       inputFile=str(Path.cwd() / ".." / "other.jsonl"))
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_predict_accepts_intermediate_output_file(client, tmp_path):
    payload = make_zip([record("s1", text="金毛犬", regions=[box("b1")])])
    upload(client, "task_chain", payload)
    first = predict(client, "task_chain", is_last=False).json()
    assert first["code"] == 0
    # [v4.2] 非阶段末也返回文件内容对象；下一算子 inputFile 取任务目录内既有输出文件
    task = tmp_path / "output" / "task_chain"
    outputs = sorted(task.rglob("18_*.jsonl"))
    assert outputs
    second = predict(client, "task_chain", is_last=False,
                     inputFile=str(outputs[0])).json()
    assert second["code"] == 0
    assert second["data"]["result"]["statistics"]["input_records"] == 1


# --------------------------------------------------------------------------- #
# 任务隔离与删除
# --------------------------------------------------------------------------- #

def test_two_tasks_are_isolated(client):
    payload = make_zip([
        record("s1", text="金毛犬 草地", regions=[box("b1"), box("b2")]),
        record("s2", text="金毛犬", regions=[box("b1")]),
    ])
    upload(client, "iso_a", payload)
    upload(client, "iso_b", payload)
    first = predict(client, "iso_a", is_last=True).json()
    second = predict(client, "iso_b", is_last=True).json()
    assert first["data"]["outputFile"]["entrypoint"] != ""
    # [v4.2] 任务隔离以 result 元数据核对（不再有 lineageFile）
    assert first["data"]["result"]["uniqueId"] == "iso_a"
    assert second["data"]["result"]["uniqueId"] == "iso_b"
    # 各自统计独立
    assert first["data"]["result"]["statistics"]["input_records"] == 2
    assert second["data"]["result"]["statistics"]["input_records"] == 2


def test_remove_file_is_idempotent(client):
    payload = make_zip([record("s1", regions=[box("b1")])])
    upload(client, "task_rm", payload)
    first = client.post("/removeFile?uniqueId=task_rm")
    assert first.status_code == 200
    assert first.json()["code"] == 0
    assert first.json()["data"]["removed"] is True
    second = client.post("/removeFile?uniqueId=task_rm")
    assert second.status_code == 200
    assert second.json()["data"]["removed"] is False


def test_remove_file_rejects_invalid_unique_id(client):
    response = client.post("/removeFile?uniqueId=..%2Fescape")
    assert response.status_code == 422
    assert response.json()["code"] == 1


# --------------------------------------------------------------------------- #
# 不存在的接口
# --------------------------------------------------------------------------- #

def test_download_file_is_not_registered(client):
    response = client.post("/downloadFile?uniqueId=task_a")
    assert response.status_code == 404
    assert response.json()["code"] == 1
    assert "downloadFile" not in {route.path for route in app_module.app.routes}


def test_openapi_declares_only_three_business_routes(client):
    paths = set(client.get("/openapi.json").json()["paths"].keys())
    # 2026-10-22 契约统一：/health 保留可用但不进 OpenAPI 文档，openapi 只含三业务接口
    assert paths == {"/uploadFile", "/predict", "/removeFile"}


def test_health_reports_route_state(client):
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["operator"] == "phrase_region_annotation"
    assert body["data"]["formal_operator_number"] == 18
    assert body["data"]["route_enabled"] is True
    assert body["data"]["min_clip_score"] == 0.25
    assert body["data"]["downloadFile"] == "not_registered"


def test_detection_alias_registry_operator_id(client):
    """[D2] map.json 登记表英文 operatorId 别名可寻址（走到缺文件分支即证明别名放行）。"""
    response = client.post("/predict", params={"detection": "phrase_region_annotation"},
                           files={"other": ("note.txt", b"hi", "text/plain")})
    assert response.json()["msg"] == "缺少上传文件 file"
