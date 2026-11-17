"""算子 #09 三接口契约测试（V4.2）：信封、双协议、路径安全、隔离与幂等。

[v4.2] 响应不再含 lineageFile：形态二所有调用统一返回 outputFile + result；
形态一 multipart/form-data 返回首字段 int status。
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import app as app_module  # noqa: E402

SAMPLE_DIR = BASE / "dataset" / "sample"

# 接口测试全部写到独立临时根目录，避免污染仓库内空 output/
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="op09_api_"))
app_module.OUTPUT_ROOT = _TMP_ROOT
client = TestClient(app_module.app)


def teardown_module(module):
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


def _zip_payload() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in sorted(SAMPLE_DIR.iterdir()):
            if p.name == "make_sample.py" or p.is_dir():
                continue
            z.write(p, p.name)
        z.writestr("manifest.json", json.dumps(
            {"format": "crossmodal-manifest-v1", "entrypoint": "demo.jsonl",
             "artifacts": [{"path": "demo.jsonl", "role": "main"}]}, ensure_ascii=False))
    buf.seek(0)
    return buf.getvalue()


def _upload(uid: str):
    return client.post(f"/uploadFile?uniqueId={uid}",
                       files={"file": ("demo.zip", _zip_payload(), "application/zip")})


def test_three_interfaces_stage_forms():
    uid = "task_1000_file_01"
    assert _upload(uid).json()["code"] == 0

    # [v4.2] 阶段首与阶段内响应形态一致：outputFile + result（无 lineageFile）
    mid = client.post("/predict", json={"uniqueId": uid, "isLastOperator": False})
    assert mid.status_code == 200
    body = mid.json()
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert body["data"]["operator"] == "candidate_description"
    assert body["data"]["status"] == "success"
    obj = body["data"]["outputFile"]
    assert obj["format"] == "crossmodal-files-v1"
    assert obj["entrypoint"] == "09_descriptions.jsonl"
    paths = [f["path"] for f in obj["files"]]
    assert "manifest.json" in paths and "09_rewrite_tasks.jsonl" in paths
    stats = body["data"]["result"]["statistics"]
    assert stats["described"] == 8 and stats["pending_manual"] == 3
    relative = body["data"]["result"]["outputFile"]
    assert (_TMP_ROOT / uid / relative).is_file()

    # 阶段内：传上一响应的任务内相对 outputFile
    inner = client.post("/predict", json={"uniqueId": uid, "isLastOperator": False,
                                          "inputFile": relative})
    assert inner.json()["code"] == 0

    # 阶段末：isLastOperator 仍严格校验，但响应形态不再改变
    last = client.post("/predict", json={"uniqueId": uid, "isLastOperator": True,
                                         "inputFile": relative})
    last_body = last.json()
    assert last_body["code"] == 0
    assert set(last_body["data"]) == {"operator", "status", "outputFile", "result"}
    obj = last_body["data"]["outputFile"]
    assert obj["format"] == "crossmodal-files-v1"

    # 文件对象恢复到全新目录后可逐个打开引用媒体
    from utils.transport import restore_file_object
    restored = _TMP_ROOT / "_restore"
    shutil.rmtree(restored, ignore_errors=True)
    entry = restore_file_object(obj, restored)
    assert entry.is_file()
    recs = [json.loads(line) for line in entry.read_text(encoding="utf-8").splitlines()]
    for rec in recs:
        media = rec.get("image")
        if isinstance(media, str):
            assert (restored / media).is_file()
    shutil.rmtree(restored, ignore_errors=True)

    assert client.post("/removeFile", params={"uniqueId": uid}).json()["code"] == 0
    assert not (_TMP_ROOT / uid).exists()


@pytest.mark.parametrize("payload,expected", [
    ({"sample_id": "s1", "caption": "一只金毛犬在草地上接飞盘"},
     {"status": 1, "process_status": "success", "description_source": "caption"}),
    ({"sample_id": "s2", "image": "img_0002.png"},
     {"status": 1, "process_status": "waiting_description", "description": None}),
])
def test_predict_single_file_form(payload, expected):
    """[v4.2] 形态一：单条样本 JSON → int status + 描述业务字段。"""
    uid = "form1_%s" % payload["sample_id"]
    response = client.post(
        "/predict?detection=build_candidate_description&uniqueId=%s" % uid,
        files={"file": ("sample.json", json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        "application/json")})
    body = response.json()
    assert body["status"] == expected["status"], body
    assert body["process_status"] == expected["process_status"]
    for key, value in expected.items():
        assert body.get(key) == value
    assert (_TMP_ROOT / uid / "form1_audit.jsonl").is_file()
    client.post("/removeFile", params={"uniqueId": uid})


def test_predict_single_file_rejects_wrong_detection():
    response = client.post(
        "/predict?detection=m99",
        files={"file": ("sample.json", b"{}", "application/json")})
    assert response.json()["status"] == 0
    assert "detection" in response.json()["msg"]


def test_predict_single_file_requires_file():
    # 用 multipart 但只带无关字段，验证「缺少 file」分支
    response = client.post("/predict?detection=build_candidate_description",
                           data={"uniqueId": "form1_nofile"},
                           files={"other": ("note.txt", b"x", "text/plain")})
    assert response.json()["status"] == 0
    assert "file" in response.json()["msg"]


def test_no_download_interface():
    assert client.get("/downloadFile").status_code == 404


def test_strict_bool_and_unknown_fields():
    uid = "task_1001_file_01"
    assert client.post("/predict", json={"uniqueId": uid}).json()["code"] == 1
    assert client.post("/predict", json={"uniqueId": uid,
                                         "isLastOperator": "false"}).json()["code"] == 1
    assert client.post("/predict", json={"uniqueId": uid,
                                         "isLastOperator": 1}).json()["code"] == 1
    assert client.post("/predict", json={"uniqueId": uid, "isLastOperator": False,
                                         "extra": 1}).json()["code"] == 1
    assert client.post("/predict", json={"uniqueId": uid, "isLastOperator": False,
                                         "config": {"description": {"min_length": 4}}}).json()["code"] == 1


def test_config_accepts_v42_param_names():
    """[v4.2] §4.9 规范参数名（min_chars/max_chars/forbidden_sources）可用。"""
    cfg = app_module.inference.load_config(overrides={"description": {
        "min_chars": 8, "max_chars": 300, "forbidden_sources": ["filename"]}})
    assert cfg["description"]["min_length"] == 8
    assert cfg["description"]["max_length"] == 300
    assert cfg["description"]["forbid_filename"] is True
    assert cfg["description"]["forbid_tags"] is False


@pytest.mark.parametrize("bad", ["", "..", "a/b", "../escape", "C:tmp"])
def test_illegal_unique_id_rejected(bad):
    resp = client.post("/predict", json={"uniqueId": bad, "isLastOperator": False})
    assert resp.status_code in (400, 422)
    assert resp.json()["code"] == 1


def test_cross_task_path_rejected():
    a, b = "task_2000_file_01", "task_2001_file_01"
    _upload(a)
    mid = client.post("/predict", json={"uniqueId": a, "isLastOperator": False}).json()
    outside = str(_TMP_ROOT / a / mid["data"]["result"]["outputFile"])
    _upload(b)
    resp = client.post("/predict", json={"uniqueId": b, "isLastOperator": False,
                                         "inputFile": outside})
    assert resp.json()["code"] == 1
    client.post("/removeFile", params={"uniqueId": a})
    client.post("/removeFile", params={"uniqueId": b})


def test_bad_zip_member_rejected():
    uid = "task_3000_file_01"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../escape.jsonl", "{}")
    buf.seek(0)
    resp = client.post(f"/uploadFile?uniqueId={uid}",
                       files={"file": ("evil.zip", buf.getvalue(), "application/zip")})
    assert resp.json()["code"] == 1


def test_remove_file_idempotent_and_isolated():
    a, b = "task_4000_file_01", "task_4001_file_01"
    _upload(a)
    _upload(b)
    first = client.post("/removeFile", params={"uniqueId": a}).json()
    assert first["code"] == 0 and first["data"]["deleted"] is True
    assert not (_TMP_ROOT / a).exists()
    assert (_TMP_ROOT / b).exists()
    # 幂等：重复删除仍成功，且不会重建或影响其他任务
    second = client.post("/removeFile", params={"uniqueId": a}).json()
    assert second["code"] == 0 and second["data"]["deleted"] is False
    assert not (_TMP_ROOT / a).exists()
    assert (_TMP_ROOT / b).exists()
    client.post("/removeFile", params={"uniqueId": b})


def test_health_reports_identity():
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["operator"] == "candidate_description"
    assert body["data"]["stageId"] == "S3"
