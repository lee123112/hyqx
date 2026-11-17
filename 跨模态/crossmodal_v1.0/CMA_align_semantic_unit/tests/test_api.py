"""三接口契约测试：/uploadFile、/predict、/removeFile（且不存在 /downloadFile）。"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import app as app_module  # noqa: E402
from tests import fixtures  # noqa: E402
from utils import transport  # noqa: E402
from utils.coordination import media_references  # noqa: E402

UNIQUE_ID = "cm07_api_0001"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    with TestClient(app_module.app) as instance:
        yield instance


def make_package(tmp_path: Path, unique_id: str = UNIQUE_ID,
                 with_context: bool = True) -> Path:
    """把生成的 fixture 打成素材包（含上游 stage_context 以验证阶段血缘）。"""
    sample = fixtures.build_fixture(tmp_path / "sample")
    if with_context:
        fixtures.write_stage_context(sample, unique_id)
    archive = tmp_path / "package.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(sample.rglob("*")):
            if path.is_file():
                handle.write(path, path.relative_to(sample).as_posix())
    return archive


def upload(client: TestClient, archive: Path, unique_id: str = UNIQUE_ID):
    with open(archive, "rb") as stream:
        return client.post("/uploadFile", params={"uniqueId": unique_id},
                           files={"file": (archive.name, stream, "application/zip")})


def predict(client: TestClient, payload: dict):
    return client.post("/predict", json=payload)


# ---------------------------------------------------------------- 正常流程

def test_upload_then_predict_middle_and_final_unified_response(client, tmp_path):
    archive = make_package(tmp_path)
    response = upload(client, archive)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0 and body["msg"] == "success"
    assert body["data"]["entrypoint"] == "demo.jsonl"

    # [v4.2] 中间与阶段末统一响应：outputFile（文件内容对象）+ result，无 lineageFile
    middle = predict(client, {"uniqueId": UNIQUE_ID, "isLastOperator": False})
    assert middle.status_code == 200
    middle_body = middle.json()
    assert middle_body["code"] == 0
    middle_data = middle_body["data"]
    assert set(middle_data) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in middle_data
    assert middle_data["operator"] == "align_semantic_unit"
    assert middle_data["status"] == "success"
    assert middle_data["outputFile"]["entrypoint"] == "07_candidates.jsonl"
    assert middle_data["result"]["statistics"]["output"] == 5

    # 阶段末：同样的统一响应形态
    final = predict(client, {"uniqueId": UNIQUE_ID, "isLastOperator": True})
    final_body = final.json()
    assert final_body["code"] == 0
    final_data = final_body["data"]
    assert set(final_data) == {"operator", "status", "outputFile", "result"}
    assert "lineageFile" not in final_data
    file_object = final_data["outputFile"]
    assert file_object["format"] == transport.FILE_OBJECT_FORMAT
    assert file_object["entrypoint"] == "07_candidates.jsonl"
    assert final_data["result"]["statistics"]["rejected"] == 0

    # 全新目录恢复后：入口与全部引用媒体可读（无上游目录也能进入 08）
    restore_dir = tmp_path / "restore"
    entry = transport.restore_file_object(file_object, restore_dir)
    records = [json.loads(line) for line in
               entry.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(records) == 5
    assert (restore_dir / "manifest.json").is_file()
    for record in records:
        for relative in media_references(record):
            assert (restore_dir / relative).is_file(), relative


def test_upload_single_jsonl_then_predict(client, tmp_path, monkeypatch):
    """单个 JSONL（不引用媒体）也能上传并处理。"""
    record = {"sample_id": "s1", "text": {"content": "hi", "lang": "en"},
              "image": None, "video": None, "audio": None}
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    response = client.post(
        "/uploadFile", params={"uniqueId": UNIQUE_ID},
        files={"file": ("demo.jsonl", payload, "application/json")})
    assert response.json()["code"] == 0
    result = predict(client, {"uniqueId": UNIQUE_ID, "isLastOperator": True}).json()
    assert result["code"] == 0
    assert result["data"]["result"]["statistics"]["not_applicable"] == 1


def test_remove_file_is_idempotent(client, tmp_path):
    upload(client, make_package(tmp_path))
    first = client.post("/removeFile", params={"uniqueId": UNIQUE_ID}).json()
    second = client.post("/removeFile", params={"uniqueId": UNIQUE_ID}).json()
    assert first == {"code": 0, "msg": "success", "data": {}}
    assert second == first
    # 清理后任务目录不存在，再次 predict 是失败信封而不是崩溃
    failed = predict(client, {"uniqueId": UNIQUE_ID, "isLastOperator": True}).json()
    assert failed["code"] == 1 and failed["data"] == {}


def test_download_file_is_not_implemented(client):
    """按契约不实现 /downloadFile。"""
    assert client.get("/downloadFile", params={"uniqueId": UNIQUE_ID}).status_code == 404
    assert client.post("/downloadFile", params={"uniqueId": UNIQUE_ID}).status_code == 404


# ---------------------------------------------------------------- 失败信封

@pytest.mark.parametrize("payload,status", [
    ({"uniqueId": UNIQUE_ID}, 422),                                     # 缺 isLastOperator
    ({"uniqueId": UNIQUE_ID, "isLastOperator": "false"}, 422),          # 字符串布尔
    ({"uniqueId": UNIQUE_ID, "isLastOperator": 1}, 422),                # 非布尔
    ({"uniqueId": UNIQUE_ID, "isLastOperator": False, "extra": 1}, 422),  # 未知字段
])
def test_predict_request_validation(client, payload, status):
    response = predict(client, payload)
    assert response.status_code == status
    body = response.json()
    assert body["code"] == 1 and body["data"] == {}
    assert body["msg"]


@pytest.mark.parametrize("config", [
    {"unknown_key": 1},
    {"av_mismatch_action": "skip"},
    {"max_av_duration_diff_ratio": "0.5"},
    {"operator": {"name": "other", "version": "v9"}},
    {"stage_id": "S1"},
    {"require_existing_media": False},
])
def test_predict_invalid_config_returns_failure_envelope(client, tmp_path, config):
    upload(client, make_package(tmp_path))
    response = predict(client, {"uniqueId": UNIQUE_ID, "isLastOperator": True,
                                "config": config})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 1 and body["data"] == {}
    assert "config" in body["msg"] or "非法" in body["msg"]


def test_predict_valid_override_is_applied(client, tmp_path):
    upload(client, make_package(tmp_path))
    response = predict(client, {"uniqueId": UNIQUE_ID, "isLastOperator": True,
                                "config": {"transcription_enabled": True}})
    body = response.json()
    assert body["code"] == 0
    # [v4.2] 无血缘 config 快照；以统计结果核对覆盖生效。
    # [v1.0.1] 配对参数已 deprecated，请求唯一可切换的是「是否做语音转写」；
    # 总开关仍关闭，所以实际仍不加载模型（effective=False）。
    stats = body["data"]["result"]["statistics"]
    assert stats["transcription"]["transcription_switch"] is True
    assert stats["transcription"]["master_switch"] is False
    assert stats["transcription"]["effective"] is False
    assert stats["transcription_model_loaded"] is False


def test_predict_bad_unique_id_returns_failure_envelope(client):
    response = predict(client, {"uniqueId": "a/b", "isLastOperator": True})
    body = response.json()
    assert body["code"] == 1 and body["data"] == {}


def test_predict_without_upload_returns_failure_envelope(client, tmp_path):
    body = predict(client, {"uniqueId": "cm07_absent", "isLastOperator": True}).json()
    assert body["code"] == 1 and body["data"] == {}


def test_predict_input_file_outside_task_rejected(client, tmp_path):
    upload(client, make_package(tmp_path))
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    body = predict(client, {"uniqueId": UNIQUE_ID, "isLastOperator": True,
                            "inputFile": str(outside)}).json()
    assert body["code"] == 1 and body["data"] == {}


@pytest.mark.parametrize("bad_id", ["bad/id", "..", "trailing."])
def test_upload_rejects_invalid_unique_id(client, tmp_path, bad_id):
    archive = make_package(tmp_path)
    body = upload(client, archive, unique_id=bad_id).json()
    assert body["code"] == 1 and body["data"] == {}


def test_upload_rejects_package_without_entrypoint(client, tmp_path):
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("notes.txt", "no jsonl here")
    body = upload(client, archive).json()
    assert body["code"] == 1 and body["data"] == {}
    assert "素材包" in body["msg"]


def test_upload_rejects_zip_slip(client, tmp_path):
    archive = tmp_path / "slip.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escape.jsonl", "{}\n")
    body = upload(client, archive).json()
    assert body["code"] == 1 and body["data"] == {}


def test_health_reports_identity(client):
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert body["data"]["operator"] == "align_semantic_unit"


def test_detection_alias_registry_operator_id(client):
    """[D2] map.json 登记表英文 operatorId 别名可寻址（走到缺文件分支即证明别名放行）。"""
    response = client.post("/predict", params={"detection": "multimodal_preprocessing"},
                           files={"other": ("note.txt", b"hi", "text/plain")})
    assert response.json()["msg"] == "缺少上传文件 file"
    assert "multimodal_preprocessing" in app_module.DETECTION_ALIASES
