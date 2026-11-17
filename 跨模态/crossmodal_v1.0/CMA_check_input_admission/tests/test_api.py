"""三接口测试：``/uploadFile``、``/predict``、``/removeFile``（``/downloadFile`` 不存在）。

覆盖：响应信封、两种 ``isLastOperator`` 数据形状、严格 bool、未知字段、非法 uniqueId、
任务内路径边界、zip 安全解压、任务隔离与 ``/removeFile`` 幂等。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import inference
from tests import fixtures as fx
from utils.transport import restore_file_object

INTERMEDIATE_KEYS = {"operator", "status", "outputFile", "result"}


def upload_dir(client, unique_id: str, source_dir: Path, tmp_path: Path):
    # 归档文件名不能直接用 uniqueId：非法 id 可能带盘符/分隔符，写到本地会失败
    archive = fx.zip_directory(source_dir, tmp_path / ("upload_%s.zip" % uuid.uuid4().hex))
    with open(archive, "rb") as handle:
        return client.post("/uploadFile", params={"uniqueId": unique_id},
                           files={"file": ("upload.zip", handle, "application/zip")})


def prepare_source(tmp_path: Path, name: str = "src", records=None) -> Path:
    source = tmp_path / name
    fx.write_input_dir(source, records if records is not None else [fx.make_record("s-1")])
    return source


def test_upload_then_predict_intermediate_returns_lineage_only(client, tmp_path):
    source = prepare_source(tmp_path)
    response = upload_dir(client, "api_inter", source, tmp_path)
    assert response.status_code == 200
    assert response.json()["code"] == 0

    body = client.post("/predict", json={"uniqueId": "api_inter", "isLastOperator": False}).json()
    assert body["code"] == 0
    assert set(body["data"]) == INTERMEDIATE_KEYS
    assert body["data"]["operator"] == inference.OPERATOR
    assert body["data"]["status"] == "success"
    assert body["data"]["outputFile"]["format"] == "crossmodal-files-v1"
    node = body["data"]["result"]
    assert node["uniqueId"] == "api_inter"
    assert node["admission"] == "accepted"
    assert node["eligible_object_id"].startswith("eligible_")
    assert node["reason_code"] == "all_required_checks_passed"
    assert node["statistics"]["admitted"] == 1


def test_predict_stage_end_returns_file_object_and_stage_lineage(client, tmp_path):
    source = prepare_source(tmp_path)
    upload_dir(client, "api_end", source, tmp_path)

    body = client.post("/predict", json={"uniqueId": "api_end", "isLastOperator": True}).json()
    assert body["code"] == 0
    # [v4.2] 统一响应：outputFile + result，去 lineageFile
    assert set(body["data"]) == INTERMEDIATE_KEYS
    assert "lineageFile" not in body["data"]
    file_object = body["data"]["outputFile"]
    assert file_object["format"] == "crossmodal-files-v1"
    assert file_object["entrypoint"] == "10_admitted.jsonl"
    assert body["data"]["result"]["statistics"]["admitted"] == 1

    restored = tmp_path / "restored"
    entry = restore_file_object(file_object, restored)
    records = fx.read_jsonl(entry)
    assert [record["sample_id"] for record in records] == ["s-1"]
    for record in records:
        for key in ("image", "video", "audio"):
            value = record.get(key)
            if isinstance(value, dict) and value.get("path"):
                assert (restored / value["path"]).is_file(), value["path"]


def test_stage_first_without_input_file_uses_uploaded_entrypoint(client, tmp_path):
    source = prepare_source(tmp_path)
    upload_dir(client, "api_first", source, tmp_path)
    body = client.post("/predict", json={"uniqueId": "api_first", "isLastOperator": False}).json()
    assert body["code"] == 0
    assert body["data"]["result"]["statistics"]["admitted"] == 1


def test_predict_requires_strict_bool_is_last_operator(client, tmp_path):
    source = prepare_source(tmp_path)
    upload_dir(client, "api_bool", source, tmp_path)
    for payload in ({"uniqueId": "api_bool"},
                    {"uniqueId": "api_bool", "isLastOperator": "false"},
                    {"uniqueId": "api_bool", "isLastOperator": 0},
                    {"uniqueId": "api_bool", "isLastOperator": None}):
        response = client.post("/predict", json=payload)
        assert response.status_code == 422
        assert response.json()["code"] == 1
        assert response.json()["data"] == {}


def test_predict_rejects_unknown_field_and_bad_config(client, tmp_path):
    source = prepare_source(tmp_path)
    upload_dir(client, "api_unknown", source, tmp_path)
    response = client.post("/predict", json={"uniqueId": "api_unknown",
                                            "isLastOperator": False, "extra": 1})
    assert response.status_code == 422
    assert response.json()["code"] == 1

    response = client.post("/predict", json={"uniqueId": "api_unknown",
                                            "isLastOperator": False, "config": []})
    assert response.status_code == 422
    assert response.json()["code"] == 1

    response = client.post("/predict", json={"uniqueId": "api_unknown",
                                            "isLastOperator": False,
                                            "config": {"min_modalities": 1}})
    assert response.status_code == 200
    assert response.json()["code"] == 1
    assert "非法" in response.json()["msg"] and "min_modalities" in response.json()["msg"]


@pytest.mark.parametrize("bad_id", ["", "..", "../escape", "a/b", "a\\b", "con", "C:\\abs"])
def test_invalid_unique_id_rejected_by_upload_and_predict(client, tmp_path, bad_id):
    source = prepare_source(tmp_path, name="src_bad")
    response = upload_dir(client, bad_id, source, tmp_path)
    assert response.status_code == 422
    assert response.json()["code"] == 1

    response = client.post("/predict", json={"uniqueId": bad_id, "isLastOperator": False})
    assert response.status_code == 422
    assert response.json()["code"] == 1


def test_predict_rejects_input_file_outside_task(client, tmp_path):
    source = prepare_source(tmp_path, name="src_scope")
    upload_dir(client, "api_scope", source, tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(fx.make_record("s-out"), ensure_ascii=False),
                       encoding="utf-8")
    response = client.post("/predict", json={"uniqueId": "api_scope", "isLastOperator": False,
                                            "inputFile": str(outside)})
    assert response.status_code == 400
    assert response.json()["code"] == 1
    # 兄弟 uniqueId 的绝对前缀同样不允许
    response = client.post("/predict", json={"uniqueId": "api_scope", "isLastOperator": False,
                                            "inputFile": "../api_scope_evil/demo.jsonl"})
    assert response.status_code == 400
    assert response.json()["code"] == 1


def test_upload_rejects_zip_slip(client, tmp_path):
    archive = fx.make_zip(tmp_path / "slip.zip", {
        "demo.jsonl": b'{"sample_id": "s-1"}\n',
        "../evil.jsonl": b'{"sample_id": "evil"}\n',
    })
    with open(archive, "rb") as handle:
        response = client.post("/uploadFile", params={"uniqueId": "api_slip"},
                               files={"file": ("slip.zip", handle, "application/zip")})
    assert response.status_code == 200
    assert response.json()["code"] == 1
    assert not (tmp_path / "evil.jsonl").exists()


def test_upload_requires_file_and_reports_entrypoint(client, tmp_path):
    response = client.post("/uploadFile", params={"uniqueId": "api_nofile"})
    assert response.status_code == 422
    assert response.json()["code"] == 1

    source = prepare_source(tmp_path, name="src_up")
    body = upload_dir(client, "api_up", source, tmp_path).json()
    assert body["code"] == 0
    assert body["data"]["entrypoint"] == "demo.jsonl"


def test_download_file_is_not_registered(client):
    # 契约禁止 /downloadFile：GET/POST 都必须拿不到路由，且回统一信封
    for method in ("get", "post"):
        response = getattr(client, method)("/downloadFile")
        assert response.status_code == 404, method
        assert response.json()["code"] == 1
        assert response.json()["data"] == {}


def test_remove_file_is_idempotent_and_isolated(client, tmp_path, output_root):
    source_a = prepare_source(tmp_path, name="src_a", records=[fx.make_record("s-a")])
    source_b = prepare_source(tmp_path, name="src_b", records=[fx.make_record("s-b")])
    upload_dir(client, "api_keep", source_a, tmp_path)
    upload_dir(client, "api_drop", source_b, tmp_path)
    assert client.post("/predict", json={"uniqueId": "api_keep",
                                        "isLastOperator": False}).json()["code"] == 0
    assert client.post("/predict", json={"uniqueId": "api_drop",
                                        "isLastOperator": False}).json()["code"] == 0

    body = client.post("/removeFile", params={"uniqueId": "api_drop"}).json()
    assert body["code"] == 0
    assert not (output_root / "api_drop").exists()
    assert (output_root / "api_keep").is_dir()
    assert (output_root / "api_keep" / "input" / "demo.jsonl").is_file()

    # 幂等：重复删除仍返回成功信封
    again = client.post("/removeFile", params={"uniqueId": "api_drop"}).json()
    assert again["code"] == 0

    bad = client.post("/removeFile", params={"uniqueId": "../escape"})
    assert bad.status_code == 422
    assert bad.json()["code"] == 1


def test_two_unique_ids_do_not_pollute_each_other(client, tmp_path):
    source_a = prepare_source(tmp_path, name="src_iso_a", records=[fx.make_record("s-iso-a")])
    source_b = prepare_source(tmp_path, name="src_iso_b", records=[fx.make_record("s-iso-b")])
    upload_dir(client, "api_iso_a", source_a, tmp_path)
    upload_dir(client, "api_iso_b", source_b, tmp_path)

    body_a = client.post("/predict", json={"uniqueId": "api_iso_a",
                                          "isLastOperator": True}).json()
    body_b = client.post("/predict", json={"uniqueId": "api_iso_b",
                                          "isLastOperator": True}).json()
    entry_a = body_a["data"]["outputFile"]["files"][0]
    entry_b = body_b["data"]["outputFile"]["files"][0]
    assert entry_a["path"] == entry_b["path"] == "10_admitted.jsonl"
    assert "s-iso-a" in entry_a["content"] and "s-iso-b" not in entry_a["content"]
    assert "s-iso-b" in entry_b["content"] and "s-iso-a" not in entry_b["content"]
    assert body_a["data"]["outputFile"]["files"] != body_b["data"]["outputFile"]["files"]


def test_repeated_predict_creates_new_run_without_overwriting(client, tmp_path, output_root):
    source = prepare_source(tmp_path, name="src_repeat")
    upload_dir(client, "api_repeat", source, tmp_path)
    first = client.post("/predict", json={"uniqueId": "api_repeat",
                                         "isLastOperator": False}).json()
    second = client.post("/predict", json={"uniqueId": "api_repeat",
                                          "isLastOperator": False}).json()
    assert first["data"]["result"]["runId"] != second["data"]["result"]["runId"]
    operator_dir = output_root / "api_repeat" / inference.OPERATOR
    assert len([child for child in operator_dir.iterdir() if child.is_dir()]) == 2


def test_health_reports_identity(client):
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["operator"] == "input_admission"
    assert body["operatorNumber"] == 10
    assert body["interfaces"] == ["/uploadFile", "/predict", "/removeFile"]
    assert body["downloadFile"] is False
    assert body["model"] is None


def test_form2_detection_operator_id_conflict_rejected(client):
    """[D3] 形态二入口：detection 与 operatorId 同时出现且归一化后不一致 → 参数错误。"""
    response = client.post("/predict", params={"detection": "m10", "operatorId": "m11"},
                           json={"uniqueId": "x", "isLastOperator": False})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 1 and "必须一致" in body["msg"]
