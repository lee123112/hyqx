"""真实服务 API smoke：启动 uvicorn 子进程并用 httpx 走真实 HTTP。

用法：``python tests/smoke_api.py``
退出码 0 表示全部通过；非 0 表示存在失败项（逐项打印）。

覆盖：三接口信封、阶段首/中间/阶段末形态、``crossmodal-files-v1`` 恢复、
两个 uniqueId 隔离、非法 uniqueId、非严格布尔 ``isLastOperator``、
任务外 ``inputFile``、zip 逃逸、``/downloadFile`` 404、``removeFile`` 幂等、
开启路线的显式失败与显式降级（零伪造分数）。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for path in (str(BASE), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import httpx  # noqa: E402

from fixtures import (default_frames, default_media, make_flat_record,  # noqa: E402
                      make_pair, zip_bytes)

HOST = "127.0.0.1"
PORT = int(os.environ.get("SMOKE_PORT", "8114"))
BASE_URL = "http://%s:%d" % (HOST, PORT)

PASSED: list = []
FAILED: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print("  PASS  %s" % name)
    else:
        FAILED.append("%s %s" % (name, detail))
        print("  FAIL  %s %s" % (name, detail))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def payload_bytes(records) -> bytes:
    return "".join(json.dumps(item, ensure_ascii=False) + "\n"
                   for item in records).encode("utf-8")


def wait_ready(client: httpx.Client, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get("/openapi.json", timeout=2.0).status_code == 200:
                return True
        except Exception:  # noqa: BLE001 - 服务尚未就绪
            time.sleep(0.4)
    return False


def upload(client, unique_id, records, media=None, filename="bundle.zip"):
    members = {"demo.jsonl": payload_bytes(records)}
    members.update(media or default_media())
    return client.post("/uploadFile", params={"uniqueId": unique_id},
                       files={"file": (filename, zip_bytes(members), "application/zip")})


def main() -> int:
    global PORT, BASE_URL
    PORT = free_port()
    BASE_URL = "http://%s:%d" % (HOST, PORT)

    workspace = Path(tempfile.mkdtemp(prefix="smoke14_"))
    output_dir = workspace / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = workspace / "config.yaml"
    config_path.write_text((BASE / "configs" / "config.yaml").read_text(encoding="utf-8"),
                           encoding="utf-8")

    env = dict(os.environ)
    env["OUTPUT_DIR"] = str(output_dir)
    env["OPERATOR_CONFIG"] = str(config_path)
    env["PYTHONPATH"] = str(BASE)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    log_path = workspace / "uvicorn.log"
    with open(log_path, "wb") as log_stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", HOST,
             "--port", str(PORT), "--log-level", "warning"],
            cwd=str(BASE), env=env, stdout=log_stream, stderr=subprocess.STDOUT)

    try:
        with httpx.Client(base_url=BASE_URL) as client:
            if not wait_ready(client):
                print("服务未在超时时间内就绪，日志：%s" % log_path)
                print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
                return 2

            print("[1] 上传与信封")
            records_a = [make_flat_record("s-a", "描述A", default_frames(2))]
            records_b = [make_flat_record("s-b1", "描述B1", default_frames(2)),
                         make_flat_record("s-b2", "描述B2", default_frames(2))]
            response = upload(client, "task_a", records_a)
            body = response.json()
            check("upload 返回 code=0", body.get("code") == 0, str(body)[:200])
            check("upload 返回入口", body["data"]["entrypoint"] == "demo.jsonl")
            upload(client, "task_b", records_b)

            response = client.post("/uploadFile", params={"uniqueId": "task_empty"},
                                   files={"file": ("empty.jsonl", b"", "application/jsonl")})
            check("空上传被拒绝(422)", response.status_code == 422,
                  "status=%s" % response.status_code)

            response = client.post(
                "/uploadFile", params={"uniqueId": "task_evil"},
                files={"file": ("evil.zip", zip_bytes({"../escape.jsonl": b"{}"}),
                                "application/zip")})
            check("zip 逃逸被拒绝", response.json().get("code") == 1)
            check("zip 逃逸未落盘", not (workspace / "escape.jsonl").exists())

            print("[2] /predict 阶段首与中间形态")
            response = client.post("/predict", json={"uniqueId": "task_a",
                                                     "isLastOperator": False})
            body = response.json()
            check("predict 返回 code=0", body.get("code") == 0, str(body)[:200])
            check("中间态 data 仅含 lineageFile", set(body["data"]) == {"lineageFile"},
                  str(set(body["data"])))
            lineage = body["data"]["lineageFile"]
            check("中间态含临时 outputFile",
                  Path(lineage["outputFile"]).name == "14_passthrough.jsonl")
            check("中间态统计真实", lineage["statistics"]["input_records"] == 1)
            check("中间态 stageId=S3", lineage["stageId"] == "S3")
            check("中间态 operator 正确",
                  lineage["operator"] == "video_text_scoring")
            check("中间态未产出模型身份", lineage["model"] is None)

            print("[3] 两个 uniqueId 隔离")
            response_b = client.post("/predict", json={"uniqueId": "task_b",
                                                       "isLastOperator": False})
            stats_b = response_b.json()["data"]["lineageFile"]["statistics"]
            check("task_b 统计独立", stats_b["input_records"] == 2,
                  str(stats_b["input_records"]))
            check("task_b 血缘 uniqueId 正确",
                  response_b.json()["data"]["lineageFile"]["uniqueId"] == "task_b")

            print("[4] /predict 阶段末形态")
            response = client.post("/predict", json={"uniqueId": "task_a",
                                                     "isLastOperator": True})
            body = response.json()
            check("阶段末 data 含 outputFile+lineageFile",
                  set(body["data"]) == {"outputFile", "lineageFile"},
                  str(set(body["data"])))
            output = body["data"]["outputFile"]
            check("阶段末为 crossmodal-files-v1",
                  output.get("format") == "crossmodal-files-v1")
            check("阶段末入口正确", output["entrypoint"] == "14_passthrough.jsonl")
            paths = {item["path"] for item in output["files"]}
            check("阶段末含 manifest.json", "manifest.json" in paths)
            check("关闭路线不搬运抽帧（表1：不生成视文产物）",
                  not any(path.startswith("frames/") for path in paths))
            stage = body["data"]["lineageFile"]
            check("阶段血缘为数组", isinstance(stage, list) and len(stage) >= 1)
            check("阶段血缘已剔除临时 outputFile", "outputFile" not in stage[0])

            print("[5] 开启路线：显式失败与显式降级（零伪造分数）")
            upload(client, "task_enabled",
                   [make_flat_record("s-e", "描述", default_frames(2))])
            response = client.post("/predict", json={
                "uniqueId": "task_enabled", "isLastOperator": False,
                "config": {"enabled": True}})
            check("严格模式无权重显式失败", response.status_code == 400
                  and "ModelUnavailable" in response.json().get("msg", ""),
                  str(response.json())[:200])

            response = client.post("/predict", json={
                "uniqueId": "task_enabled", "isLastOperator": False,
                "config": {"enabled": True, "allow_degraded": True}})
            body = response.json()
            check("降级模式 code=0", body.get("code") == 0, str(body)[:200])
            stats = body["data"]["lineageFile"]["statistics"]
            check("降级模式无通过样本", stats["passed_records"] == 0)
            check("降级模式无隔离样本", stats["sample_isolated"] == 0)
            check("降级模式统计守恒", stats["conservation_ok"] is True)

            response = client.post("/predict", json={
                "uniqueId": "task_enabled", "isLastOperator": True,
                "config": {"enabled": True, "allow_degraded": True}})
            output = response.json()["data"]["outputFile"]
            paths = {item["path"] for item in output["files"]}
            check("启用路线阶段末入口为视文评分文件",
                  output["entrypoint"] == "14_video_text_scores.jsonl")
            check("启用路线阶段末含被引用抽帧",
                  any(path.startswith("frames/") for path in paths))
            check("启用路线阶段末含待处理台账", "14_pending.jsonl" in paths)

            print("[6] 校验与边界")
            for value in ["..", "a/b", "con"]:
                response = client.post("/predict", json={"uniqueId": value,
                                                          "isLastOperator": False})
                check("非法 uniqueId 被拒绝(%s)" % value, response.status_code == 422,
                      "status=%s" % response.status_code)
            for value in ["false", 1, None]:
                response = client.post("/predict", json={"uniqueId": "task_a",
                                                          "isLastOperator": value})
                check("非严格布尔 isLastOperator 被拒绝(%r)" % value,
                      response.status_code == 422, "status=%s" % response.status_code)

            outside = workspace / "outside.jsonl"
            outside.write_bytes(payload_bytes(records_a))
            response = client.post("/predict", json={"uniqueId": "task_a",
                                                      "isLastOperator": False,
                                                      "inputFile": str(outside)})
            check("任务外 inputFile 被拒绝(400)", response.status_code == 400,
                  "status=%s" % response.status_code)

            check("/downloadFile 未注册(404)",
                  client.get("/downloadFile", params={"uniqueId": "task_a"}).status_code == 404)

            print("[7] /removeFile")
            first = client.post("/removeFile", params={"uniqueId": "task_a"})
            second = client.post("/removeFile", params={"uniqueId": "task_a"})
            check("removeFile 首次删除成功", first.json()["data"]["removed"] is True)
            check("removeFile 幂等", second.json()["data"]["removed"] is False)
            check("removeFile 不影响其他任务", (output_dir / "task_b").exists())
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()

    print("\n结果：%d 通过 / %d 失败" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("失败项：")
        for item in FAILED:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
