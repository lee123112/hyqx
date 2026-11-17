"""算子 #2 数据初筛测试：处理逻辑、异常场景与三接口契约。"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import inference
from app import app

SAMPLE = BASE / "dataset" / "sample" / "samples.jsonl"
client = TestClient(app)


@pytest.fixture(scope="session", autouse=True)
def ensure_sample():
    if not SAMPLE.exists():
        subprocess.run([sys.executable, str(BASE / "dataset" / "sample" / "make_sample.py")],
                       check=True)


def test_text_keywords():
    kws = inference.text_keywords("一只金毛犬在草地上接飞盘")
    assert "金毛犬" in kws and "草地" in kws
    assert "一" not in kws


def test_image_keywords():
    assert inference.image_keywords("golden_retriever_grass.jpg", {"image_tags": ["飞盘"]}) == set()
    assert "金毛犬" in inference.image_keywords("arbitrary.jpg", {"image_caption":"金毛犬在草地接飞盘"})


def test_run_on_sample():
    r=inference.run(str(SAMPLE), "pytest_sample")
    assert r["status"]=="success"
    assert r["statistics"]["kept"]==6
    assert r["statistics"]["rejected"]==2
    kept=[json.loads(l)["sample_id"] for l in Path(r["outputFile"]).read_text('utf-8').splitlines()]
    assert kept==["sample-0001","sample-0002","sample-0003","sample-0004","sample-0005","sample-0007"]
    # 0003缺图注；0005探测不可用时证据不足，不能用文件名或比例直接排除。
    reasons={json.loads(l).get("sample_id"):json.loads(l)["reason"] for l in Path(r["rejectsFile"]).read_text('utf-8').splitlines()}
    assert reasons["sample-0006"].startswith("file_not_found")
    assert reasons[None].startswith("missing_field")


def test_empty_and_corrupt_input(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    r = inference.run(str(empty), "pytest_empty")
    assert r["status"] == "success" and r["statistics"]["total"] == 0

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"sample_id": "x", "text": "仅文本"}\n{not json}\n',
                   encoding="utf-8")
    r = inference.run(str(bad), "pytest_bad")
    assert r["statistics"]["bad_lines"] == 1 and r["statistics"]["kept"] == 1


def test_input_not_found():
    r = inference.run(str(BASE / "no_such.jsonl"), "pytest_missing")
    assert r["status"] == "failed"
    # [v4.2] 血缘输出已停用：run() 返回不再携带 lineage 键
    assert "lineage" not in r
    assert r["reason"] == "input_not_found"


def test_three_interfaces():
    uid = "pytest_api"
    shutil.rmtree(BASE / "output" / uid, ignore_errors=True)

    # 真实场景：工程侧上传 zip（JSONL + 媒体文件），算子解压后处理
    import io as _io
    import zipfile
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in SAMPLE.parent.iterdir():
            if p.name != "make_sample.py":
                z.write(p, p.name)
    buf.seek(0)
    resp = client.post(f"/uploadFile?uniqueId={uid}",
                       files={"file": ("samples.zip", buf)})
    assert resp.status_code == 200

    # [v4.2] 形态二响应统一：不再区分阶段末/非阶段末，始终返回 outputFile + result，无 lineageFile
    resp = client.post("/predict", json={"uniqueId": uid, "isLastOperator": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert set(body["data"]) == {"operator", "status", "outputFile", "result"}
    assert body["data"]["operator"] == "filter_irrelevant_data"
    assert body["data"]["status"] == "success"
    assert body["data"]["result"]["statistics"]["kept"] == 6
    assert body["data"]["outputFile"]["format"] == "crossmodal-files-v1"
    assert body["data"]["outputFile"]["entrypoint"] == "02_screened.jsonl"

    last = client.post("/predict", json={"uniqueId": uid, "isLastOperator": True})
    assert last.status_code == 200
    last_body = last.json()
    assert last_body["code"] == 0
    assert set(last_body["data"]) == {"operator", "status", "outputFile", "result"}
    assert last_body["data"]["outputFile"]["entrypoint"] == "02_screened.jsonl"
    assert any(item["path"] == "manifest.json" for item in last_body["data"]["outputFile"]["files"])

    resp = client.get("/downloadFile")
    assert resp.status_code == 404
    assert resp.json()["code"] == 1

    resp = client.post("/removeFile", params={"uniqueId": uid})
    assert resp.status_code == 200
    assert not (BASE / "output" / uid).exists()


def test_isolation_between_tasks():
    inference.run(str(SAMPLE), "pytest_iso_a")
    inference.run(str(SAMPLE), "pytest_iso_b")
    a = BASE / "output" / "pytest_iso_a" / "02_screened.jsonl"
    b = BASE / "output" / "pytest_iso_b" / "02_screened.jsonl"
    assert a.exists() and b.exists()


# --------------------------------------------------------------------------- #
# [v4.2] 形态一（单文件流）：multipart/form-data + detection 别名
# --------------------------------------------------------------------------- #

def test_single_file_flow_multipart():
    """形态一：上传单条样本 JSONL，判定型 status（1=保留 0=过滤）。"""
    uid = "pytest_sf_keep"
    shutil.rmtree(BASE / "output" / uid, ignore_errors=True)
    record = {"sample_id": "sf-1", "text": "一只金毛犬在草地上接飞盘",
              "image": {"path": "x.jpg", "caption": "金毛犬草地飞盘"}}
    (BASE / "output" / uid).mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    resp = client.post("/predict?detection=M03&uniqueId=%s" % uid,
                       files={"file": ("one.jsonl", payload, "application/json")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 0  # 引用的媒体文件不存在 → file_not_found 过滤
    assert body["filter_status"] == "quarantine"
    assert body["reason_code"].startswith("file_not_found")

    record2 = {"sample_id": "sf-2", "text": "只有文本没有媒体"}
    payload2 = (json.dumps(record2, ensure_ascii=False) + "\n").encode("utf-8")
    resp2 = client.post("/predict?operatorId=filter_irrelevant_data&uniqueId=%s" % uid,
                        files={"file": ("one.jsonl", payload2, "application/json")})
    body2 = resp2.json()
    assert body2["status"] == 1
    assert body2["filter_status"] == "pass"
    assert body2["reason_code"] == "keyword_or_duration_check_passed"

    # detection 与算子别名不一致 → 参数错误
    bad = client.post("/predict?detection=M99&uniqueId=%s" % uid,
                      files={"file": ("one.jsonl", payload2, "application/json")})
    assert bad.json()["status"] == 0

    # detection 与 operatorId 同时出现但取值不一致 → 参数错误
    conflict = client.post("/predict?detection=M03&operatorId=M04&uniqueId=%s" % uid,
                           files={"file": ("one.jsonl", payload2, "application/json")})
    assert conflict.json()["status"] == 0


def test_health_reports_identity():
    """[D1] /health 存活探针：返回算子标识/状态。"""
    body = client.get("/health").json()
    assert body["code"] == 0
    assert body["data"]["operator"] == "data_prescreen"
    assert body["data"]["operatorVersion"] == inference.VERSION
