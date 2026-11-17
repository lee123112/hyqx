"""三接口测试（TestClient）：信封、阶段中间/末返回形态、路径安全与任务隔离。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import build_package_zip, default_records
from utils import sampling, transport

import app as app_module

ALL_ROUTES = ("image_text", "audio_text", "video_text")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    output_root = tmp_path / "api_output"
    output_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OUTPUT_DIR", str(output_root))
    with TestClient(app_module.app) as test_client:
        yield test_client


def api_output_root() -> Path:
    return Path(app_module.inference.default_output_dir())


def make_zip(tmp_path: Path, name: str) -> Path:
    return build_package_zip(tmp_path, name, records=default_records(),
                             routes=ALL_ROUTES, isolation_ids=("cm-9912",))


def _upload(client, unique_id: str, zip_path: Path):
    with open(zip_path, "rb") as handle:
        return client.post("/uploadFile", params={"uniqueId": unique_id},
                           files={"file": (zip_path.name, handle, "application/zip")})


def test_health_declares_three_endpoints(client):
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["formal_operator_number"] == 26
    assert body["data"]["downloadFile"] == "not_registered"
    assert body["data"]["endpoints"] == ["/uploadFile", "/predict", "/removeFile"]
    assert body["data"]["model"] is None


def test_upload_and_intermediate_prediction(client, tmp_path):
    """[v4.2] 阶段内响应统一为 outputFile + result，不再返回 lineageFile。"""
    upload = _upload(client, "api_one", make_zip(tmp_path, "a.zip"))
    assert upload.status_code == 200 and upload.json()["code"] == 0
    assert upload.json()["data"]["entrypoint"] == "train/samples.jsonl"

    response = client.post("/predict", json={"uniqueId": "api_one",
                                             "isLastOperator": False})
    body = response.json()
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert body["data"]["outputFile"]["format"] == transport.FILE_OBJECT_FORMAT
    summary = body["data"]["result"]
    assert summary["uniqueId"] == "api_one"
    assert summary["statistics"]["sampled_records"] == 16
    assert summary["delivery_sample"] == sampling.EXCEL_NAME


def test_final_prediction_returns_file_object(client, tmp_path):
    _upload(client, "api_final", make_zip(tmp_path, "b.zip"))
    response = client.post("/predict", json={"uniqueId": "api_final",
                                             "isLastOperator": True})
    body = response.json()
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in body["data"]
    file_object = body["data"]["outputFile"]
    assert file_object["format"] == transport.FILE_OBJECT_FORMAT
    assert file_object["entrypoint"] == sampling.MAIN_NAME
    paths = [item["path"] for item in file_object["files"]]
    assert sampling.EXCEL_NAME in paths
    assert sampling.REPORT_NAME in paths
    assert "manifest.json" in paths
    assert body["data"]["result"]["routes"]["image_text"]["max_count"] == 10


def test_final_file_object_restores_excel_and_rows(client, tmp_path):
    _upload(client, "api_restore", make_zip(tmp_path, "c.zip"))
    body = client.post("/predict", json={"uniqueId": "api_restore",
                                         "isLastOperator": True}).json()
    dest = tmp_path / "restored"
    entry = transport.restore_file_object(body["data"]["outputFile"], dest)
    rows = [json.loads(line) for line in
            entry.read_text(encoding="utf-8").strip().splitlines()]
    assert len(rows) == 16
    excel = dest / sampling.EXCEL_NAME
    assert excel.is_file()
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.load_workbook(excel)
    assert book["交付样例"].max_row == 17


def test_stage_first_request_without_upload_fails(client):
    response = client.post("/predict", json={"uniqueId": "api_missing",
                                             "isLastOperator": False})
    assert response.status_code == 400
    assert response.json()["code"] == 1


@pytest.mark.parametrize("payload", [
    {"uniqueId": "api_one"},
    {"uniqueId": "api_one", "isLastOperator": "false"},
    {"uniqueId": "api_one", "isLastOperator": 1},
    {"uniqueId": "api_one", "isLastOperator": None},
    {"uniqueId": "../escape", "isLastOperator": False},
    {"uniqueId": "", "isLastOperator": False},
    {"uniqueId": "api_one", "isLastOperator": False, "unknown": 1},
])
def test_invalid_requests_return_envelope(client, payload):
    response = client.post("/predict", json=payload)
    body = response.json()
    assert response.status_code in (400, 422)
    assert body["code"] == 1 and body["msg"]
    assert set(body) == {"code", "msg", "data"}


def test_input_file_outside_task_is_rejected(client, tmp_path):
    _upload(client, "api_path", make_zip(tmp_path, "d.zip"))
    response = client.post("/predict", json={
        "uniqueId": "api_path", "isLastOperator": False,
        "inputFile": "../api_path_other/train/samples.jsonl"})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_chained_input_file_from_intermediate_response(client, tmp_path):
    """[v4.2] 链路续跑：把上一响应 outputFile 还原到本任务 input 后再传 inputFile。"""
    _upload(client, "api_chain", make_zip(tmp_path, "e.zip"))
    first = client.post("/predict", json={"uniqueId": "api_chain",
                                          "isLastOperator": False}).json()
    assert first["code"] == 0
    task_input = api_output_root() / "api_chain" / "input"
    entry = transport.restore_file_object(first["data"]["outputFile"], task_input)
    relative = entry.relative_to(api_output_root() / "api_chain").as_posix()
    second = client.post("/predict", json={"uniqueId": "api_chain",
                                           "isLastOperator": True,
                                           "inputFile": relative})
    assert second.json()["code"] == 0
    assert second.json()["data"]["outputFile"]["format"] == \
        transport.FILE_OBJECT_FORMAT


def test_overrides_are_restricted_and_config_alias(client, tmp_path):
    _upload(client, "api_ovr", make_zip(tmp_path, "f.zip"))
    ok = client.post("/predict", json={"uniqueId": "api_ovr", "isLastOperator": False,
                                       "config": {"max_samples_per_route": 2}})
    assert ok.json()["code"] == 0
    assert ok.json()["data"]["result"]["statistics"]["sampled_records"] == 2 + 2 + 2
    bad = client.post("/predict", json={"uniqueId": "api_ovr", "isLastOperator": False,
                                        "overrides": {"format": "csv"}})
    assert bad.status_code == 400 and bad.json()["code"] == 1


def test_two_tasks_are_isolated(client, tmp_path):
    _upload(client, "iso_one", make_zip(tmp_path, "g1.zip"))
    _upload(client, "iso_two", make_zip(tmp_path, "g2.zip"))
    first = client.post("/predict", json={"uniqueId": "iso_one",
                                          "isLastOperator": False}).json()
    second = client.post("/predict", json={"uniqueId": "iso_two",
                                           "isLastOperator": False}).json()
    assert first["data"]["result"]["uniqueId"] == "iso_one"
    assert second["data"]["result"]["uniqueId"] == "iso_two"
    assert (api_output_root() / "iso_one").is_dir()
    assert (api_output_root() / "iso_two").is_dir()


def test_zip_slip_is_rejected(client, tmp_path):
    with zipfile.ZipFile(tmp_path / "slip.zip", "w") as bundle:
        bundle.writestr("../escape.jsonl", "{}")
        bundle.writestr("train/samples.jsonl", "{}\n")
    response = _upload(client, "api_slip", tmp_path / "slip.zip")
    assert response.status_code == 422
    assert response.json()["code"] == 1
    assert not (api_output_root() / "escape.jsonl").exists()


def test_upload_rejects_empty_and_unsupported(client):
    empty = client.post("/uploadFile", params={"uniqueId": "api_empty"},
                        files={"file": ("empty.zip", b"", "application/zip")})
    assert empty.status_code == 422 and empty.json()["code"] == 1
    other = client.post("/uploadFile", params={"uniqueId": "api_other"},
                        files={"file": ("a.txt", b"hello", "text/plain")})
    assert other.status_code == 422 and other.json()["code"] == 1


def test_download_file_is_not_registered(client):
    response = client.get("/downloadFile", params={"uniqueId": "x_f1"})
    assert response.status_code == 404


def test_remove_file_is_idempotent_and_scoped(client, tmp_path):
    _upload(client, "rm_one", make_zip(tmp_path, "h1.zip"))
    _upload(client, "rm_two", make_zip(tmp_path, "h2.zip"))
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
    # 2026-10-22 契约统一：/health 保留可用但不进 OpenAPI 文档，openapi 只含三业务接口
    assert set(schema["paths"]) == {"/uploadFile", "/predict", "/removeFile"}


def test_direct_input_directory_can_be_used_without_upload(client, tmp_path):
    """未上传 zip 时，工程侧可把交付包直接放进任务 input 目录。"""
    from conftest import build_package, package_root_of

    entry = build_package(tmp_path / "direct", records=default_records(),
                          routes=ALL_ROUTES, isolation_ids=("cm-9912",))
    root = package_root_of(entry)
    task_input = api_output_root() / "api_direct" / "input"
    task_input.mkdir(parents=True, exist_ok=True)
    for path in root.rglob("*"):
        if path.is_file():
            target = task_input / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    body = client.post("/predict", json={"uniqueId": "api_direct",
                                         "isLastOperator": False}).json()
    assert body["code"] == 0
    assert body["data"]["result"]["statistics"]["sampled_records"] == 16
    assert json.loads(json.dumps(body, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# [v4.2] 形态一：单文件流（multipart/form-data）
# --------------------------------------------------------------------------- #

def _post_form1(client, unique_id: str, zip_path: Path, **data: Any):
    with open(zip_path, "rb") as handle:
        return client.post("/predict", params={"detection": "M26",
                                               "uniqueId": unique_id},
                           files={"file": (zip_path.name, handle,
                                           "application/zip")},
                           data=data)


def test_single_file_stream_generates_samples(client, tmp_path):
    zip_path = make_zip(tmp_path, "f1.zip")
    body = _post_form1(client, "api_form1", zip_path,
                       per_route_max="2", sampling_seed="7").json()
    assert body["status"] == 1, body.get('package_error', body)
    assert body['outputFile']['mode'] == 'path-reference'
    assert Path(body['outputFile']['zip']).is_file()
    assert body["delivery_sample"] == sampling.EXCEL_NAME
    assert body["routes"]["image_text"]["sample_count"] == 2
    assert body["routes"]["image_text"]["max_count"] == 2
    assert body["source_package_version"] == "v1.0.0"
    assert body["process_status"] == "success"


def test_single_file_stream_pending_package_is_not_generatable(client, tmp_path):
    """待发布包不得生成样例：形态一必须返回 status=0 且不给样例引用。"""
    zip_path = build_package_zip(tmp_path, "pending.zip", records=default_records(),
                                 routes=ALL_ROUTES, isolation_ids=("cm-9912",),
                                 version_state="pending_release", status="待发布")
    body = _post_form1(client, "api_form1_pending", zip_path).json()
    assert body["status"] == 0
    assert "delivery_sample" not in body
    assert body["process_status"] == "degraded"
    assert body["source_package_version"] == "v1.0.0"
    assert body["msg"]


def test_single_file_stream_detection_mismatch_and_missing_file(client, tmp_path):
    zip_path = make_zip(tmp_path, "f2.zip")
    with open(zip_path, "rb") as handle:
        mismatch = client.post("/predict", params={"detection": "M25"},
                               files={"file": (zip_path.name, handle,
                                               "application/zip")}).json()
    assert mismatch["status"] == 0 and "detection" in mismatch["msg"]
    missing = client.post("/predict", params={"detection": "M26"},
                          files={"other": ("x.txt", b"x", "text/plain")}).json()
    assert missing["status"] == 0 and "file" in missing["msg"]


def test_form2_detection_operator_id_conflict_rejected(client):
    """[D3] 形态二入口：detection 与 operatorId 同时出现且归一化后不一致 → 参数错误。"""
    response = client.post("/predict", params={"detection": "m26", "operatorId": "m25"},
                           json={"uniqueId": "x", "isLastOperator": False})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1 and "必须一致" in body["msg"]
