"""HTTP 接口测试（TestClient）：三接口信封、阶段形态、路径安全与任务隔离。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
from tests.conftest import ENTRY_NAME, make_record, media_bytes, write_jsonl
from utils import intake as it


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    return TestClient(app_module.app), tmp_path / "out"


def rows(count: int = 3, **kwargs):
    return [make_record("api-%02d" % index, **kwargs) for index in range(count)]


def jsonl_bytes(records) -> bytes:
    return ("\n".join(json.dumps(item, ensure_ascii=False) for item in records)
            + "\n").encode("utf-8")


def build_zip(records, entry_name: str = ENTRY_NAME,
              extra: dict = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(entry_name, jsonl_bytes(records))
        for name, payload in (extra or {}).items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def upload(client, unique_id: str, payload: bytes, filename: str = "bundle.zip"):
    return client.post("/uploadFile?uniqueId=%s" % unique_id,
                       files={"file": (filename, payload, "application/octet-stream")})


def predict(client, unique_id: str, is_last: bool = False, **extra):
    body = {"uniqueId": unique_id, "isLastOperator": is_last}
    body.update(extra)
    return client.post("/predict", json=body).json()


# --------------------------------------------------------------------------- #
# 接口存在性
# --------------------------------------------------------------------------- #

def test_health_declares_three_interfaces(client):
    http, _root = client
    body = http.get("/health").json()
    assert body["operator"] == it.OPERATOR_NAME
    assert body["operatorLabel"] == it.OPERATOR_LABEL
    assert body["operatorNumber"] == 1
    assert body["stageId"] == "S1"
    assert body["interfaces"] == ["/uploadFile", "/predict", "/removeFile"]
    assert body["downloadFile"] is False


def test_openapi_exposes_only_three_business_routes(client):
    http, _root = client
    routes = set(http.get("/openapi.json").json()["paths"])
    # 2026-10-22 契约统一：/health 保留可用但不进 OpenAPI 文档，openapi 只含三业务接口
    assert routes == {"/uploadFile", "/predict", "/removeFile"}


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #

def test_upload_returns_envelope_and_entrypoint(client):
    http, _root = client
    body = upload(http, "api_up_1", build_zip(rows())).json()
    assert body["code"] == 0
    assert body["data"]["uniqueId"] == "api_up_1"
    assert body["data"]["entrypoint"] == ENTRY_NAME
    assert body["data"]["fileCount"] == 1


def test_upload_zip_with_media_files(client):
    http, _root = client
    payload = build_zip(rows(1), extra={"media/api-00.png": media_bytes("png")})
    body = upload(http, "api_up_media", payload).json()
    assert body["code"] == 0
    assert body["data"]["fileCount"] == 2
    assert "media/api-00.png" in body["data"]["files"]


def test_upload_multiple_files(client):
    http, _root = client
    response = http.post(
        "/uploadFile?uniqueId=api_multi",
        files=[("files", (ENTRY_NAME, jsonl_bytes(rows(1)), "application/json")),
               ("files", ("media/a.png", media_bytes("png"), "image/png"))])
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["fileCount"] == 2
    assert body["data"]["entrypoint"] == ENTRY_NAME


def test_upload_multiple_jsonl_is_rejected_as_ambiguous(client):
    http, _root = client
    response = http.post(
        "/uploadFile?uniqueId=api_multi_amb",
        files=[("files", ("a.jsonl", jsonl_bytes(rows(1)), "application/json")),
               ("files", ("b.jsonl", jsonl_bytes(rows(1)), "application/json"))])
    assert response.json()["code"] == 1


def test_upload_empty_payload_is_rejected(client):
    http, _root = client
    body = upload(http, "api_empty", b"").json()
    assert body["code"] == 1


def test_upload_zip_slip_is_rejected_and_not_written(client):
    http, root = client
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.jsonl", "{}\n")
    body = upload(http, "api_slip", buffer.getvalue()).json()
    assert body["code"] == 1
    assert not (root / "escape.jsonl").exists()


@pytest.mark.parametrize("bad", ["..%2Fescape", "a%2Fb", "CON", ""])
def test_invalid_unique_id_is_rejected(client, bad):
    http, _root = client
    response = upload(http, bad, jsonl_bytes(rows(1)))
    assert response.status_code == 200
    assert response.json()["code"] == 1


# --------------------------------------------------------------------------- #
# 推理：阶段首 / 阶段内 / 阶段末
# --------------------------------------------------------------------------- #

def test_predict_stage_first_without_input_file(client):
    """阶段首算子请求省略 inputFile，入口取任务 input/ 下的唯一 JSONL。"""
    http, _root = client
    upload(http, "api_first", build_zip(rows(3)))
    body = predict(http, "api_first")
    assert body["code"] == 0
    # [v4.2] 形态二统一响应：operator/status/outputFile/result，去 lineageFile
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    node = body["data"]["result"]
    assert body["data"]["operator"] == it.OPERATOR_NAME
    assert node["uniqueId"] == "api_first"
    assert body["data"]["outputFile"]["format"] == "crossmodal-files-v1"
    assert node["batch_id"] == "BATCH-20261001-01"
    assert node["accepted"] == 3
    assert node["statistics"]["registered"] == 3
    assert node["statistics"]["conservation_ok"] is True
    assert body["data"]["status"] == "success"


def test_predict_intermediate_and_stage_end_shapes(client):
    http, _root = client
    upload(http, "api_forms", build_zip(rows(2)))
    middle = predict(http, "api_forms", is_last=False)
    assert set(middle["data"]) == {"operator", "status", "outputFile", "result"}
    assert middle["data"]["outputFile"]["format"] == "crossmodal-files-v1"

    last = predict(http, "api_forms", is_last=True)
    # [v4.2] isLastOperator 保留严格校验，但不再改变响应形态
    assert set(last["data"]) == {"operator", "status", "outputFile", "result"}
    file_object = last["data"]["outputFile"]
    assert file_object["format"] == "crossmodal-files-v1"
    assert file_object["entrypoint"] == it.MAIN_NAME
    names = [item["path"] for item in file_object["files"]]
    for name in (it.MAIN_NAME, it.ORIGIN_QUARANTINE_NAME, it.SAMPLE_QUARANTINE_NAME,
                 it.RETURN_NAME, it.REGISTRY_NAME, "manifest.json"):
        assert name in names, name
    assert "lineageFile" not in last["data"]


def test_stage_end_file_object_can_be_restored_and_chained(client, tmp_path):
    from utils import transport

    http, _root = client
    upload(http, "api_chain", build_zip(rows(2)))
    last = predict(http, "api_chain", is_last=True)
    restored = transport.restore_file_object(last["data"]["outputFile"],
                                            tmp_path / "restored")
    assert restored.name == it.MAIN_NAME


def test_predict_with_chained_input_file_inside_task(client):
    http, root = client
    upload(http, "api_prev", build_zip(rows(1)))
    predict(http, "api_prev", is_last=False)
    produced = sorted(root.rglob(it.MAIN_NAME))
    assert produced
    body = predict(http, "api_prev", inputFile=str(produced[-1]))
    assert body["code"] == 0
    assert body["data"]["result"]["statistics"]["registered"] == 1


def test_predict_rejects_cross_task_input_file(client):
    http, root = client
    upload(http, "api_iso_a", build_zip(rows(1)))
    upload(http, "api_iso_b", build_zip(rows(1)))
    foreign = root / "api_iso_b" / "input" / ENTRY_NAME
    assert foreign.is_file()
    body = predict(http, "api_iso_a", inputFile=str(foreign))
    assert body["code"] == 1
    assert "任务" in body["msg"]


def test_predict_without_upload_fails_with_envelope(client):
    http, _root = client
    body = predict(http, "api_never_uploaded")
    assert body["code"] == 1
    assert body["data"] == {}


def test_predict_rejects_non_boolean_is_last_operator(client):
    http, _root = client
    upload(http, "api_bool", build_zip(rows(1)))
    for value in ("false", 1, None, "true"):
        response = http.post("/predict", json={"uniqueId": "api_bool",
                                               "isLastOperator": value})
        assert response.status_code == 422
        assert response.json()["code"] == 1


def test_two_unique_ids_are_isolated(client):
    http, root = client
    upload(http, "api_iso_1", build_zip(rows(2)))
    upload(http, "api_iso_2", build_zip(rows(4, ext="wav", modality="audio")))
    first = predict(http, "api_iso_1", is_last=True)
    second = predict(http, "api_iso_2", is_last=True)
    assert first["data"]["result"]["statistics"]["registered"] == 2
    assert second["data"]["result"]["statistics"]["registered"] == 4
    assert first["data"]["result"]["uniqueId"] == "api_iso_1"
    assert second["data"]["result"]["uniqueId"] == "api_iso_2"
    assert (root / "api_iso_1").is_dir() and (root / "api_iso_2").is_dir()


def test_disabled_override_takes_effect(client):
    http, _root = client
    upload(http, "api_disabled", build_zip(rows(3)))
    body = predict(http, "api_disabled", config={"enabled": False})
    assert body["code"] == 0
    assert body["data"]["status"] == "disabled"
    assert body["data"]["result"]["statistics"]["registered"] == 0


# --------------------------------------------------------------------------- #
# 配置安全
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("config", [
    {"sources": ["collection_platform"]},
    {"allowed_license": "不可训练"},
    {"file_formats": {"image": ["gif"]}},
    {"transport": {"format": "other"}},
    {"enabled": "true"},
    {"nope": 1},
    {"file_check_mode": "turbo"},
])
def test_invalid_or_frozen_config_is_rejected(client, config):
    http, _root = client
    upload(http, "api_cfg", build_zip(rows(1)))
    body = predict(http, "api_cfg", config=config)
    assert body["code"] == 1, config


def test_whitelist_mode_override_is_accepted(client):
    http, _root = client
    upload(http, "api_wl", build_zip(rows(1)))
    body = predict(http, "api_wl", config={"file_check_mode": "whitelist",
                                           "dedupe": False})
    assert body["code"] == 0
    # [v4.2] 血缘已停用：配置快照不再随响应返回，改由统计字段验证覆盖生效
    stats = body["data"]["result"]["statistics"]
    assert stats["file_check_mode"] == "whitelist"
    assert stats["conservation_ok"] is True


# --------------------------------------------------------------------------- #
# 其它
# --------------------------------------------------------------------------- #

def test_download_file_is_not_implemented(client):
    http, _root = client
    response = http.get("/downloadFile?uniqueId=x")
    assert response.status_code == 404
    assert response.json()["code"] == 1
    assert response.json()["data"] == {}


def test_remove_file_is_idempotent_and_isolated(client):
    http, root = client
    upload(http, "api_rm_a", build_zip(rows(1)))
    upload(http, "api_rm_b", build_zip(rows(1)))
    first = http.post("/removeFile?uniqueId=api_rm_a").json()
    second = http.post("/removeFile?uniqueId=api_rm_a").json()
    assert first["code"] == 0 and first["data"]["removed"] is True
    assert second["code"] == 0 and second["data"]["removed"] is False
    assert not (root / "api_rm_a").exists()
    assert (root / "api_rm_b").is_dir()


def test_remove_file_rejects_illegal_unique_id(client):
    http, _root = client
    body = http.post("/removeFile?uniqueId=..%2Fescape").json()
    assert body["code"] == 1


def test_non_utf8_input_line_is_quarantined_not_fatal(client):
    http, _root = client
    payload = ("坏行\n").encode("gbk") + jsonl_bytes(rows(1))
    upload(http, "api_enc", payload, filename=ENTRY_NAME)
    body = predict(http, "api_enc")
    assert body["code"] == 0
    stats = body["data"]["result"]["statistics"]
    assert stats["registered"] == 1
    assert stats["rejected"] == 1
    assert stats["reject_reasons"] == {"invalid_utf8": 1}
    assert stats["origin_quarantined"] == 0
    assert stats["conservation_ok"] is True


def test_single_jsonl_upload_gets_manifest(client):
    http, _root = client
    body = upload(http, "api_single", jsonl_bytes(rows(2)), filename=ENTRY_NAME).json()
    assert body["code"] == 0
    assert body["data"]["entrypoint"] == ENTRY_NAME


def test_json_upload_without_entrypoint_like_name(client):
    http, _root = client
    body = upload(http, "api_other_name", jsonl_bytes(rows(1)),
                  filename="batch_list.jsonl").json()
    assert body["code"] == 0
    assert body["data"]["entrypoint"] == "batch_list.jsonl"


def test_uploaded_media_is_used_for_local_file_check(client):
    http, _root = client
    record = make_record("api_local", remote=False, ext="png")
    payload = build_zip([record], extra={"media/api_local.png": media_bytes("png")})
    upload(http, "api_local_task", payload)
    body = predict(http, "api_local_task")
    stats = body["data"]["result"]["statistics"]
    assert stats["registered"] == 1
    assert stats["local_verified"] == 1


def test_output_written_only_under_unique_id_dir(client):
    http, root = client
    upload(http, "api_scope", build_zip(rows(1)))
    predict(http, "api_scope", is_last=True)
    produced = list(root.rglob(it.MAIN_NAME))
    assert produced
    for path in produced:
        assert path.relative_to(root).parts[0] == "api_scope"


def test_health_and_predict_report_real_counts(client):
    http, _root = client
    upload(http, "api_counts", build_zip([make_record("c-1"),
                                          make_record("c-2", ext="gif"),
                                          make_record("c-3", license="不可训练")]))
    node = predict(http, "api_counts")["data"]["result"]
    stats = node["statistics"]
    assert (stats["registered"], stats["origin_quarantined"],
            stats["sample_quarantined"]) == (1, 1, 1)
    assert stats["objects"] == 3
    assert stats["conservation_failures"] == []


def test_form2_detection_operator_id_conflict_rejected(client):
    """[D3] 形态二入口：detection 与 operatorId 同时出现且归一化后不一致 → 参数错误。"""
    http, _out = client
    response = http.post("/predict", params={"detection": "m01", "operatorId": "m02"},
                         json={"uniqueId": "x", "isLastOperator": False})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1 and "必须一致" in body["msg"]
