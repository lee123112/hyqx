"""三接口端到端 smoke（不启动真实端口，用 TestClient 覆盖同一套路由）。

运行：
    python tests/smoke_api.py

覆盖：
1. ``/health`` 真实后端状态；
2. 两个 uniqueId 上传 + 阶段中间响应（只回 lineageFile，含临时 outputFile）；
3. 阶段末响应（文件内容对象 + 完整阶段血缘）并真实还原核对；
4. 非法 uniqueId / 跨任务 inputFile / ``/downloadFile`` / ``removeFile`` 幂等。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

WORKSPACE = tempfile.mkdtemp(prefix="operator17_smoke_")
os.environ["OUTPUT_DIR"] = str(Path(WORKSPACE) / "output")

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from tests import fixtures  # noqa: E402
from utils import transport  # noqa: E402

PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        raise AssertionError("FAILED: %s %s" % (label, detail))
    PASSED += 1
    print("  ok  %s" % label)


def main() -> int:
    client = TestClient(app_module.app)
    media = {"media/demo.png": fixtures.png_bytes(12, 12)}

    print("[1] /health")
    health = client.get("/health").json()
    check("code=0", health["code"] == 0)
    check("operator=local_to_whole_annotation",
          health["data"]["operator"] == "local_to_whole_annotation")
    check("正式编号 17", health["data"]["formal_operator_number"] == 17)
    check("默认关闭", health["data"]["enabled_by_default"] is False)
    detector = health["data"]["detector"]
    check("模型名 YOLOv8n", detector["detector_model"] == "YOLOv8n")
    check("仅通用类别", detector["general_classes_only"] is True)
    check("不联网", detector["network_access"] is False)
    if not detector["available"]:
        check("不可用有真实原因", bool(detector["reason"]), detector["reason"])
    check("/downloadFile 未注册", health["data"]["downloadFile"] == "not_registered")

    print("[2] 两个 uniqueId 上传并跑阶段中间响应")
    for unique_id, records in (
        ("smoke_a", [fixtures.make_record("a-1", image="media/demo.png"),
                     fixtures.make_record("a-2", modality=["text"], description="文本")]),
        ("smoke_b", [fixtures.make_record("b-1", image="media/demo.png",
                                          human=fixtures.human_evidence(
                                              boxes=[fixtures.human_box()]))]),
    ):
        uploaded = client.post(
            "/uploadFile", params={"uniqueId": unique_id},
            files={"file": ("bundle.zip", fixtures.bundle_zip(records=records, media=media),
                            "application/zip")}).json()
        check("upload %s code=0" % unique_id, uploaded["code"] == 0)
        check("upload %s 有入口" % unique_id, uploaded["data"]["entrypoint"] == "demo.jsonl")
        check("upload %s 有媒体" % unique_id, "media/demo.png" in uploaded["data"]["mediaFiles"])

    runs = {}
    for unique_id in ("smoke_a", "smoke_b"):
        body = client.post("/predict", json={"uniqueId": unique_id,
                                            "isLastOperator": False,
                                            "config": {"enabled": True}}).json()
        check("%s 响应 code=0" % unique_id, body["code"] == 0)
        # [v4.2] 统一响应形态：去 lineageFile，始终返回 outputFile + result
        check("%s 无 lineageFile" % unique_id, "lineageFile" not in body["data"])
        check("%s 始终返回 outputFile" % unique_id,
              body["data"]["outputFile"]["format"] == "crossmodal-files-v1")
        statistics = body["data"]["result"]["statistics"]
        check("%s 统计守恒" % unique_id,
              statistics["conservation_ok"] is True,
              json.dumps(statistics.get("conservation"), ensure_ascii=False))
        check("%s 降级不造框" % unique_id,
              statistics["boxes_prefilled"] == 0
              or statistics["detector_available"] is True)
        runs[unique_id] = body["data"]
    check("两个任务输出对象独立",
          runs["smoke_a"]["outputFile"] != runs["smoke_b"]["outputFile"])

    print("[3] 阶段末响应 + 真实还原")
    last = client.post("/predict", json={"uniqueId": "smoke_b", "isLastOperator": True,
                                         "config": {"enabled": True}}).json()
    check("阶段末 code=0", last["code"] == 0)
    check("阶段末字段齐全",
          set(last["data"]) == {"operator", "status", "outputFile", "result"})
    file_object = last["data"]["outputFile"]
    check("文件对象格式", file_object["format"] == "crossmodal-files-v1")
    paths = {item["path"] for item in file_object["files"]}
    check("含入口", file_object["entrypoint"] in paths)
    check("含 manifest", "manifest.json" in paths)
    check("含原始隔离清单", "17_rejects.jsonl" in paths)
    restored_dir = Path(WORKSPACE) / "restored"
    entry = transport.restore_file_object(file_object, restored_dir)
    rows = fixtures.read_jsonl(entry)
    check("还原后可解析", len(rows) >= 1)
    check("还原时真实写出媒体",
          (restored_dir / "media" / "demo.png").is_file())

    print("[3b] 阶段控制文件 + #18 下游字段")
    run_dir = Path(os.environ["OUTPUT_DIR"]) / "smoke_b" / "17_local_to_whole_annotation"
    run_dir = sorted(run_dir.glob("run*"))[-1]
    # [v4.2] 血缘文件已停用：不再产出 lineage.json / stage_context.json
    check("lineage.json 已停用", not (run_dir / "lineage.json").exists())
    check("stage_context.json 已停用", not (run_dir / "stage_context.json").exists())
    log_text = (run_dir / "run.log").read_text(encoding="utf-8")
    check("run.log 落盘", "run_start" in log_text and "run_end" in log_text)
    check("控制文件不进文件对象",
          not ({"lineage.json", "stage_context.json", "run.log"} & paths))
    delivered = rows
    block = delivered[0]["processing"]["17"]
    check("#18 regions/candidate_boxes 就绪",
          isinstance(block["regions"], list) and isinstance(block["candidate_boxes"], list))
    check("#18 paths.image 就绪", block["paths"]["image"] == "media/demo.png")
    check("#18 预填绝不标人工确认",
          all(item["human_verified"] is False for item in block["candidate_boxes"]))
    check("人工确认框与预填分支单列",
          block["boxes_source"] == "human_confirmed"
          and block["regions"] == block["confirmed_boxes"])

    print("[4] 安全边界")
    for bad in ("../escape", "a/b", "CON", ""):
        response = client.post("/uploadFile", params={"uniqueId": bad},
                               files={"file": ("b.zip", fixtures.bundle_zip(media=media),
                                               "application/zip")})
        check("拒绝非法 uniqueId %r" % bad, response.status_code == 422)
    for value in ("false", 0, None):
        response = client.post("/predict", json={"uniqueId": "smoke_a",
                                                 "isLastOperator": value})
        check("isLastOperator 必须严格布尔 %r" % (value,), response.status_code == 422)
    response = client.post("/predict", json={"uniqueId": "smoke_a",
                                             "isLastOperator": False,
                                             "inputFile": "../smoke_b/input/demo.jsonl"})
    check("拒绝跨任务 inputFile", response.status_code == 400)
    response = client.get("/downloadFile", params={"uniqueId": "smoke_a"})
    check("/downloadFile 返回 404", response.status_code == 404)
    check("/downloadFile 也是 code=1 信封", response.json()["code"] == 1)
    response = client.post("/predict", json={"uniqueId": "smoke_a", "isLastOperator": False,
                                             "config": {"weights": "hack.pt"}})
    check("拒绝改写模型/路径", response.status_code == 400)
    check("失败信封 data 为空对象", response.json()["data"] == {})

    from utils import io as io_utils

    with io_utils.TaskLock(Path(os.environ["OUTPUT_DIR"]), "smoke_b"):
        busy = client.post("/predict", json={"uniqueId": "smoke_b",
                                             "isLastOperator": False})
    check("同 uniqueId 并发写返回 task_busy",
          busy.status_code == 409 and "task_busy" in busy.json()["msg"])

    print("[5] removeFile 幂等与隔离")
    first = client.post("/removeFile", params={"uniqueId": "smoke_a"}).json()
    check("首次删除 removed=True", first["data"]["removed"] is True)
    second = client.post("/removeFile", params={"uniqueId": "smoke_a"}).json()
    check("重复删除 removed=False", second["data"]["removed"] is False)
    check("另一任务未被删除",
          (Path(os.environ["OUTPUT_DIR"]) / "smoke_b").is_dir())

    print("\nSMOKE PASSED: %d checks" % PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
