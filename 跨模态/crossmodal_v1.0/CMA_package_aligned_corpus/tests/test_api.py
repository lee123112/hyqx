"""三接口测试（TestClient）：信封、阶段中间/末返回形态、路径安全与任务隔离。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import build_input_zip, build_scenario
from utils import delivery, transport

import app as app_module


@pytest.fixture()
def client(tmp_path, monkeypatch):
    output_root = tmp_path / "api_output"
    output_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OUTPUT_DIR", str(output_root))
    with TestClient(app_module.app) as test_client:
        yield test_client


def api_output_root() -> Path:
    """服务实际使用的输出根（与 app 内部同一解析函数）。"""
    return Path(app_module.inference.default_output_dir())


def test_form1_zip_returns_chainable_reference(client, tmp_path):
    package = build_input_zip(tmp_path, 'chain.zip', written_back=True)
    with package.open('rb') as stream:
        body = client.post('/predict?detection=M23&uniqueId=chain_zip',
            files={'file': ('input.zip', stream, 'application/zip')}).json()
    assert body['status'] == 1, body.get('package_error', body)
    assert body['outputFile']['mode'] == 'path-reference'
    assert Path(body['outputFile']['zip']).is_file()


def _upload(client, unique_id: str, zip_path: Path):
    with open(zip_path, "rb") as handle:
        return client.post("/uploadFile", params={"uniqueId": unique_id},
                           files={"file": (zip_path.name, handle, "application/zip")})


def test_health_declares_three_endpoints(client):
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["formal_operator_number"] == 23
    assert body["data"]["downloadFile"] == "not_registered"
    assert body["data"]["endpoints"] == ["/uploadFile", "/predict", "/removeFile"]


def test_upload_and_intermediate_prediction(client, tmp_path):
    upload = _upload(client, "api_task_one", build_input_zip(tmp_path, "a.zip",
                                                             written_back=True))
    assert upload.status_code == 200 and upload.json()["code"] == 0
    assert upload.json()["data"]["entrypoint"] == "samples.jsonl"

    response = client.post("/predict", json={"uniqueId": "api_task_one",
                                             "isLastOperator": False})
    body = response.json()
    assert body["code"] == 0
    # [v4.2] 响应统一：不再区分阶段末/非阶段末，lineageFile 已去除
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in body["data"]
    node = body["data"]["result"]
    assert node["outputFile"] and Path(node["outputFile"]).is_file()
    assert node["statistics"]["packaged_records"] == 10


def test_final_prediction_returns_file_object_without_lineage(client, tmp_path):
    _upload(client, "api_task_final", build_input_zip(tmp_path, "b.zip",
                                                      written_back=True))
    response = client.post("/predict", json={"uniqueId": "api_task_final",
                                             "isLastOperator": True})
    body = response.json()
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    file_object = body["data"]["outputFile"]
    assert file_object["format"] == transport.FILE_OBJECT_FORMAT
    assert file_object["entrypoint"] == delivery.MAIN_NAME
    paths = [item["path"] for item in file_object["files"]]
    assert delivery.PACKAGE_DELIVERABLE_NAME in paths
    assert "manifest.json" in paths
    assert delivery.EXCLUDED_NAME in paths
    assert delivery.MATERIALS_NAME in paths


def test_stage_first_request_without_upload_fails(client):
    response = client.post("/predict", json={"uniqueId": "api_missing",
                                             "isLastOperator": False})
    assert response.status_code == 400
    assert response.json()["code"] == 1


@pytest.mark.parametrize("payload", [
    {"uniqueId": "api_task_one"},
    {"uniqueId": "api_task_one", "isLastOperator": "false"},
    {"uniqueId": "api_task_one", "isLastOperator": 1},
    {"uniqueId": "api_task_one", "isLastOperator": None},
    {"uniqueId": "../escape", "isLastOperator": False},
    {"uniqueId": "", "isLastOperator": False},
    {"uniqueId": "api_task_one", "isLastOperator": False, "unknown": 1},
])
def test_invalid_requests_return_envelope(client, payload):
    response = client.post("/predict", json=payload)
    body = response.json()
    assert response.status_code in (400, 422)
    assert body["code"] == 1 and body["msg"]
    assert set(body) == {"code", "msg", "data"}


def test_missing_is_last_operator_is_rejected(client, tmp_path):
    _upload(client, "api_task_shape", build_input_zip(tmp_path, "c.zip"))
    response = client.post("/predict", json={"uniqueId": "api_task_shape"})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_input_file_outside_task_is_rejected(client, tmp_path):
    _upload(client, "api_task_path", build_input_zip(tmp_path, "d.zip"))
    response = client.post("/predict", json={
        "uniqueId": "api_task_path", "isLastOperator": False,
        "inputFile": "../api_task_shape/samples.jsonl"})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_chained_input_file_from_intermediate_response(client, tmp_path):
    _upload(client, "api_task_chain", build_input_zip(tmp_path, "e.zip",
                                                      written_back=True))
    first = client.post("/predict", json={"uniqueId": "api_task_chain",
                                          "isLastOperator": False}).json()
    output_file = first["data"]["result"]["outputFile"]
    relative = Path(output_file).relative_to(api_output_root() / "api_task_chain").as_posix()
    second = client.post("/predict", json={"uniqueId": "api_task_chain",
                                           "isLastOperator": True,
                                           "inputFile": relative})
    assert second.json()["code"] == 0
    assert second.json()["data"]["outputFile"]["format"] == transport.FILE_OBJECT_FORMAT


def test_config_field_is_alias_of_overrides(client, tmp_path):
    """技术设计 §4 的 `config` 字段与 `overrides` 等价，且同样受白名单限制。"""
    _upload(client, "api_task_config", build_input_zip(tmp_path, "c2.zip"))
    ok = client.post("/predict", json={"uniqueId": "api_task_config",
                                       "isLastOperator": False,
                                       "config": {"include_media": False}})
    assert ok.json()["code"] == 0
    bad = client.post("/predict", json={"uniqueId": "api_task_config",
                                        "isLastOperator": False,
                                        "config": {"exclude_isolated": False}})
    assert bad.status_code == 400
    assert bad.json()["code"] == 1


def test_overrides_are_restricted(client, tmp_path):
    _upload(client, "api_task_override", build_input_zip(tmp_path, "f.zip"))
    ok = client.post("/predict", json={"uniqueId": "api_task_override",
                                       "isLastOperator": False,
                                       "overrides": {"include_media": False}})
    assert ok.json()["code"] == 0
    bad = client.post("/predict", json={"uniqueId": "api_task_override",
                                        "isLastOperator": False,
                                        "overrides": {"exclude_isolated": False}})
    assert bad.status_code == 400
    assert bad.json()["code"] == 1


def test_two_tasks_are_isolated(client, tmp_path):
    _upload(client, "iso_one", build_input_zip(tmp_path, "g1.zip", written_back=True))
    _upload(client, "iso_two", build_input_zip(tmp_path, "g2.zip", written_back=False))
    first = client.post("/predict", json={"uniqueId": "iso_one",
                                          "isLastOperator": False}).json()
    second = client.post("/predict", json={"uniqueId": "iso_two",
                                           "isLastOperator": False}).json()
    assert first["data"]["result"]["uniqueId"] == "iso_one"
    assert second["data"]["result"]["uniqueId"] == "iso_two"
    # [v4.2] 血缘输出已停用：配置快照改由 run() 结果核对（见 test_operator.py）
    assert first["data"]["result"]["runId"] == "run001"
    assert second["data"]["result"]["runId"] == "run001"
    assert (api_output_root() / "iso_one").is_dir()
    assert (api_output_root() / "iso_two").is_dir()


def test_zip_slip_is_rejected(client, tmp_path):
    with __import__("zipfile").ZipFile(tmp_path / "slip.zip", "w") as bundle:
        bundle.writestr("../escape.jsonl", "{}")
        bundle.writestr("samples.jsonl", "{}\n")
    response = _upload(client, "api_slip", tmp_path / "slip.zip")
    assert response.status_code == 422
    assert response.json()["code"] == 1
    assert not (api_output_root() / "escape.jsonl").exists()


def test_download_file_is_not_registered(client):
    response = client.get("/downloadFile", params={"uniqueId": "x_f1"})
    assert response.status_code == 404


def test_remove_file_is_idempotent_and_scoped(client, tmp_path):
    _upload(client, "rm_one", build_input_zip(tmp_path, "h1.zip"))
    _upload(client, "rm_two", build_input_zip(tmp_path, "h2.zip"))
    first = client.post("/removeFile", params={"uniqueId": "rm_one"}).json()
    second = client.post("/removeFile", params={"uniqueId": "rm_one"}).json()
    assert first["code"] == 0 and first["data"]["removed"] is True
    assert second["code"] == 0 and second["data"]["removed"] is False
    assert (api_output_root() / "rm_two").is_dir()
    assert not (api_output_root() / "rm_one").exists()


def test_remove_file_rejects_invalid_unique_id(client):
    response = client.post("/removeFile", params={"uniqueId": "../escape"})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_openapi_exposes_only_three_business_endpoints(client):
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    # 2026-10-22 契约统一：/health 保留可用但不进 OpenAPI 文档，openapi 只含三业务接口
    assert paths == {"/uploadFile", "/predict", "/removeFile"}


def test_upload_rejects_empty_payload(client):
    response = client.post("/uploadFile", params={"uniqueId": "api_empty"},
                           files={"file": ("empty.zip", b"", "application/zip")})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_direct_json_input_can_be_inspected_without_http(client, tmp_path):
    """未上传 zip 时也可以直接把输入包目录放入任务 input（工程侧搬运场景）。"""
    entry = build_scenario(tmp_path / "direct", written_back=True)
    task_input = api_output_root() / "api_direct" / "input"
    task_input.mkdir(parents=True, exist_ok=True)
    for path in Path(entry).parent.rglob("*"):
        if path.is_file():
            target = task_input / path.relative_to(Path(entry).parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    body = client.post("/predict", json={"uniqueId": "api_direct",
                                         "isLastOperator": False}).json()
    assert body["code"] == 0
    assert body["data"]["result"]["statistics"]["packaged_records"] == 10
    assert json.loads(json.dumps(body, ensure_ascii=False))


def test_form2_detection_operator_id_conflict_rejected(client):
    """[D3] 形态二入口：detection 与 operatorId 同时出现且归一化后不一致 → 参数错误。"""
    response = client.post("/predict", params={"detection": "m23", "operatorId": "m24"},
                           json={"uniqueId": "x", "isLastOperator": False})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1 and "必须一致" in body["msg"]
