"""算子 #12 三接口契约测试：上传/预测/删除、阶段中间与阶段末响应、路径与 ZIP 安全。"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import app as app_module  # noqa: E402
from app import OUTPUT_ROOT, app  # noqa: E402
from conftest import StubEncoder, png_bytes, unit, vector_with_cosine  # noqa: E402
from utils.transport import restore_file_object  # noqa: E402

client = TestClient(app)

TEXT_A = "一只金毛犬在草地上接飞盘"
TEXT_B = "红色方块放在桌面上"


def _pair(pair_id: str, image: str, text: str = TEXT_A) -> dict:
    return {
        "pair_id": pair_id,
        "group_id": "grp-src-A",
        "modality_pair": ["text", "image"],
        "evidence": {"type": "A", "criterion": "same_source_id", "evidence_source": "rule",
                     "value": {"source_id": "src-A"}},
        "evidence_priority": 1,
        "left": {"sample_id": "s1", "modalities": ["text"],
                 "record": {"sample_id": "s1", "batch_id": "batch-01",
                            "text": {"content": text, "description_source": "caption"},
                            "image": None, "video": None, "audio": None, "metadata": {}}},
        "right": {"sample_id": "s2", "modalities": ["image"],
                  "record": {"sample_id": "s2", "batch_id": "batch-01",
                             "text": None, "image": {"path": image}, "video": None,
                             "audio": None, "metadata": {}}},
        "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0"},
    }


def _zip(rows=None, entry: str = "pairs.jsonl", media=("media/a.png", "media/b.png",
                                                       "media/c.png")) -> bytes:
    rows = rows if rows is not None else [_pair("p1", "media/a.png"),
                                         _pair("p2", "media/b.png", TEXT_B),
                                         _pair("p3", "media/c.png")]
    manifest = {"format": "crossmodal-manifest-v1", "entrypoint": entry,
                "artifacts": [{"path": entry, "role": "main"}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr(entry, "".join(json.dumps(row, ensure_ascii=False) + "\n"
                                        for row in rows))
        for name in media:
            archive.writestr(name, png_bytes())
    return buf.getvalue()


def _stub() -> StubEncoder:
    return StubEncoder(
        text_vectors={TEXT_A: [1.0, 0.0], TEXT_B: [1.0, 0.0]},
        image_vectors={"a.png": [1.0, 0.0], "b.png": vector_with_cosine(0.25),
                       "c.png": unit([-1.0, 0.0])},
    )


def upload(uid: str, data: bytes, name: str = "pairs.zip"):
    return client.post("/uploadFile", params={"uniqueId": uid},
                       files={"file": (name, data)})


@pytest.fixture
def stub_backend(monkeypatch):
    """注入测试替身后端：run() 期望 (encoder, degrade_reason, model_identity)。"""
    created = []

    def build(cfg):
        stub = _stub()
        created.append(stub)
        return stub, None, stub.model_identity()

    monkeypatch.setattr(app_module.inference, "build_encoder", build)
    monkeypatch.setattr(app_module.inference, "build_detector", lambda cfg: (None, None))
    return created


def test_upload_and_intermediate_response_contract(stub_backend):
    uid = "api_intermediate"
    assert upload(uid, _zip()).json()["code"] == 0
    body = client.post("/predict", json={"uniqueId": uid,
                                         "isLastOperator": False}).json()
    assert body["code"] == 0
    # [v4.2] 形态二统一响应：去 lineageFile，始终返回 operator/status/outputFile/result
    assert set(body["data"]) == {"operator", "status", "process_status",
                                 "outputFile", "result"}
    assert body["data"]["operator"] == "image_text_semantic_scoring"
    assert body["data"]["status"] == "success"
    assert body["data"]["outputFile"]["format"] == "crossmodal-files-v1"
    assert body["data"]["result"]["statistics"]["output"] == 2
    assert body["data"]["result"]["statistics"]["isolated"] == 1


def test_stage_first_request_needs_no_input_file_and_tail_object(stub_backend):
    uid = "api_stage_tail"
    assert upload(uid, _zip()).json()["code"] == 0
    intermediate = client.post("/predict", json={"uniqueId": uid,
                                                 "isLastOperator": False}).json()
    # [v4.2] 业务摘要内的产物路径为任务相对路径，可直接回传 inputFile
    temp_file = intermediate["data"]["result"]["files"]["outputFile"]

    body = client.post("/predict", json={"uniqueId": uid, "isLastOperator": True,
                                         "inputFile": temp_file}).json()
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "process_status",
                                 "outputFile", "result"}

    files = body["data"]["outputFile"]
    assert files["format"] == "crossmodal-files-v1"
    assert files["entrypoint"] == "12_image_text_scores.jsonl"
    # [v4.2] 回传自身产物触发复用（不重复评分）
    assert body["data"]["result"]["reused"] is True

    restore_dir = OUTPUT_ROOT / uid / "restored"
    entry = restore_file_object(files, restore_dir)
    assert entry.is_file()
    restored = [json.loads(line) for line in entry.read_text("utf-8").splitlines()]
    assert len(restored) == 2
    assert (restore_dir / "12_isolation.jsonl").is_file()


def test_two_unique_ids_are_isolated(stub_backend):
    assert upload("api_iso_a", _zip()).json()["code"] == 0
    assert upload("api_iso_b", _zip()).json()["code"] == 0
    first = client.post("/predict", json={"uniqueId": "api_iso_a",
                                          "isLastOperator": False}).json()
    second = client.post("/predict", json={"uniqueId": "api_iso_b",
                                           "isLastOperator": False}).json()
    assert first["code"] == 0 and second["code"] == 0
    # [v4.2] 产物路径在 result.files 中（任务相对路径）
    path_a = OUTPUT_ROOT / "api_iso_a" / first["data"]["result"]["files"]["outputFile"]
    path_b = OUTPUT_ROOT / "api_iso_b" / second["data"]["result"]["files"]["outputFile"]
    assert path_a != path_b
    assert path_a.is_file() and path_b.is_file()
    assert "api_iso_a" in str(path_a) and "api_iso_b" in str(path_b)


@pytest.mark.parametrize("payload", [
    {"isLastOperator": False},
    {"uniqueId": "api_bad", "isLastOperator": "false"},
    {"uniqueId": "api_bad", "isLastOperator": 0},
    {"uniqueId": "api_bad", "isLastOperator": False, "inputFile": 5},
    {"uniqueId": "api_bad", "isLastOperator": False, "config": "x"},
    {"uniqueId": "api_bad", "isLastOperator": False, "operator": "hacked"},
])
def test_predict_rejects_invalid_payload(payload, stub_backend):
    body = client.post("/predict", json=payload).json()
    assert body["code"] == 1


@pytest.mark.parametrize("uid", ["", "..", "../escape", "a/b", "a\\b", "C:", "CON"])
def test_upload_and_predict_reject_bad_unique_id(uid, stub_backend):
    assert upload(uid, _zip()).json()["code"] == 1
    assert client.post("/predict", json={"uniqueId": uid,
                                         "isLastOperator": False}).json()["code"] == 1


def test_predict_rejects_out_of_task_input_file(stub_backend):
    uid = "api_path_guard"
    assert upload(uid, _zip()).json()["code"] == 0
    outside = OUTPUT_ROOT.parent / "not_in_task.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    body = client.post("/predict", json={"uniqueId": uid, "isLastOperator": False,
                                         "inputFile": str(outside)}).json()
    assert body["code"] == 1
    traversal = client.post("/predict", json={
        "uniqueId": uid, "isLastOperator": False,
        "inputFile": "../api_iso_a/input/pairs.jsonl"}).json()
    assert traversal["code"] == 1


def test_predict_rejects_unknown_config_key(stub_backend):
    uid = "api_cfg_guard"
    assert upload(uid, _zip()).json()["code"] == 0
    body = client.post("/predict", json={"uniqueId": uid, "isLastOperator": False,
                                         "config": {"output_root": "/"}}).json()
    assert body["code"] == 1


def test_download_file_is_not_registered():
    assert client.get("/downloadFile", params={"uniqueId": "x_f1"}).status_code == 404
    assert client.post("/downloadFile").status_code == 404


def test_zip_slip_and_duplicate_members_rejected():
    slip = io.BytesIO()
    with zipfile.ZipFile(slip, "w") as archive:
        archive.writestr("../evil.jsonl", "{}")
    assert upload("api_slip", slip.getvalue()).json()["code"] == 1
    assert not (OUTPUT_ROOT / "api_slip" / "input").exists()

    dup = io.BytesIO()
    with zipfile.ZipFile(dup, "w") as archive:
        archive.writestr("pairs.jsonl", "{}")
        archive.writestr("PAIRS.JSONL", "{}")
    assert upload("api_dup", dup.getvalue()).json()["code"] == 1


def test_upload_requires_file_and_unambiguous_entry():
    assert client.post("/uploadFile", params={"uniqueId": "api_no_file"}).json()["code"] == 1

    ambiguous = io.BytesIO()
    with zipfile.ZipFile(ambiguous, "w") as archive:
        archive.writestr("a.jsonl", "{}")
        archive.writestr("b.jsonl", "{}")
    assert upload("api_ambiguous", ambiguous.getvalue()).json()["code"] == 1


def test_remove_file_is_idempotent_and_isolated(stub_backend):
    assert upload("api_remove_a", _zip()).json()["code"] == 0
    assert upload("api_remove_b", _zip()).json()["code"] == 0
    first = client.post("/removeFile", params={"uniqueId": "api_remove_a"}).json()
    assert first["code"] == 0 and first["data"]["deleted"] is True
    assert not (OUTPUT_ROOT / "api_remove_a").exists()
    assert (OUTPUT_ROOT / "api_remove_b").is_dir()
    second = client.post("/removeFile", params={"uniqueId": "api_remove_a"}).json()
    assert second["code"] == 0 and second["data"]["deleted"] is False


def test_predict_failed_envelope_when_model_required(stub_backend, monkeypatch, tmp_path):
    uid = "api_degraded"
    monkeypatch.setattr(app_module.inference, "build_encoder",
                        lambda cfg: (None, "no_weights", None))
    assert upload(uid, _zip()).json()["code"] == 0
    body = client.post("/predict", json={"uniqueId": uid, "isLastOperator": False}).json()
    assert body["code"] == 0
    # [v4.2] 模型未启用是正常流程态：code=0 + process_status=degraded，不伪装成接口失败
    assert body["data"]["status"] == "success"
    assert body["data"]["process_status"] == "degraded"
    assert body["data"]["result"]["statistics"]["pending_model"] == 3


# ---------------------------------------------------------------- [v4.2] 形态一（单文件流）


def _pairs_jsonl(rows=None) -> bytes:
    rows = rows if rows is not None else [_pair("p1", "media/a.png")]
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")


def test_single_file_protocol_zip_success(stub_backend):
    # 高分（cos=1.0）→ auto_align，判定型 status=1
    response = client.post("/predict", params={"detection": "M12"},
                           files={"file": ("pairs.zip", _zip(rows=[_pair("p1", "media/a.png")],
                                                             media=("media/a.png",)))})
    body = response.json()
    assert body["status"] == 1
    # [v4.3] 压缩包模式：首字段 status=1 表示执行成功，判定在 band；outputFile 为路径引用
    assert body["outputFile"]["format"] == "crossmodal-files-v1"
    assert body["outputFile"]["mode"] == "path-reference"
    assert Path(body["outputFile"]["zip"]).is_file()
    assert body["score"] == pytest.approx(1.0)
    assert body["band"] == "auto_align"
    assert body["model_version"]
    assert body["thresholds"]["isolation"] == pytest.approx(0.20)  # [2026-11-05 全量标定]
    assert body["thresholds"]["auto_align"] == pytest.approx(0.32)
    assert body["process_status"] == "success"


def test_single_file_protocol_quarantine(stub_backend):
    # 低分（cos=-1.0 < 0.25）→ quarantine；[v4.3] 压缩包首字段 status 为执行态（1=成功），
    # 业务判定从 band/score 读取，不落后形态一旧判定语义。
    payload = _zip(rows=[_pair("p3", "media/c.png")], media=("media/c.png",))
    response = client.post("/predict", params={"detection": "score_image_text_consistency"},
                           files={"file": ("pairs.zip", payload)})
    body = response.json()
    assert body["status"] == 1
    assert body["outputFile"]["mode"] == "path-reference"
    assert body["process_status"] == "success"
    assert body["band"] == "quarantine"
    assert body["score"] == pytest.approx(-1.0)


def test_single_file_protocol_detection_mismatch_rejected(stub_backend):
    response = client.post("/predict", params={"detection": "M13"},
                           files={"file": ("pairs.jsonl", _pairs_jsonl())})
    body = response.json()
    assert body["status"] == 0
    assert "不匹配" in body["msg"]


def test_single_file_protocol_media_rejected(stub_backend):
    response = client.post("/predict", params={"detection": "12"},
                           files={"file": ("cat.png", png_bytes(), "image/png")})
    body = response.json()
    assert body["status"] == 0
    assert "JSON" in body["msg"]


def test_single_file_protocol_registry_operator_id_alias(stub_backend):
    """[D2] map.json 登记表英文 operatorId 别名可寻址。"""
    response = client.post("/predict", params={"detection": "image_text_semantic_scoring"},
                           files={"file": ("pairs.zip", _zip(rows=[_pair("p1", "media/a.png")],
                                                             media=("media/a.png",)))})
    body = response.json()
    assert body["status"] == 1
    assert body["band"] == "auto_align"
