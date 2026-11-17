"""真实 HTTP 冒烟：起 uvicorn，走 /uploadFile → /predict（中间/阶段末）→ /removeFile。

验证：真实网络栈下的响应信封、阶段中间/阶段末返回形态、两条 uniqueId 隔离、
非法 uniqueId、inputFile 目录穿越拒绝与 /downloadFile 不存在。

用法：python tests/smoke_api.py
默认使用本机 CLIP 权重配置（无权重时为显式 degraded），不伪造任何分数。
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import httpx  # noqa: E402

HOST = "127.0.0.1"
for port_candidate in (8112, 8122, 8132):
    with socket.socket() as probe:
        try:
            probe.bind((HOST, port_candidate))
            PORT = port_candidate
            break
        except OSError:
            continue
else:  # pragma: no cover
    raise SystemExit("没有可用端口")

BASE_URL = "http://%s:%d" % (HOST, PORT)
CHECKS = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))
    print("%s %s %s" % ("PASS" if condition else "FAIL", name, detail))
    if not condition:
        raise SystemExit("冒烟失败：%s %s" % (name, detail))


def build_package() -> bytes:
    """把 dataset/sample 打成可上传的素材包（含 manifest / 入口 / 媒体）。"""
    sample = BASE / "dataset" / "sample"
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name in ("manifest.json", "pairs.jsonl"):
            archive.write(str(sample / name), name)
        media_dir = sample / "media"
        for item in sorted(media_dir.glob("*")):
            if item.is_file():
                archive.write(str(item), "media/%s" % item.name)
    return payload.getvalue()


def wait_ready(process, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise SystemExit("uvicorn 提前退出，returncode=%s" % process.returncode)
        try:
            response = httpx.get(BASE_URL + "/health", timeout=2.0)
            if response.status_code == 200:
                return
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    raise SystemExit("uvicorn 启动超时")


def main() -> int:
    output_root = Path(tempfile.mkdtemp(prefix="op12_smoke_output_"))
    environment = dict(os.environ)
    environment["OUTPUT_DIR"] = str(output_root)
    environment["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", HOST,
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(BASE), env=environment)
    try:
        wait_ready(process)
        health = httpx.get(BASE_URL + "/health", timeout=5.0).json()
        check("健康检查返回算子身份", health["code"] == 0
              and health["data"]["operator"] == "image_text_semantic_scoring",
              "port=%d" % PORT)

        package = build_package()
        for uid in ("api_smoke_a", "api_smoke_b"):
            uploaded = httpx.post(BASE_URL + "/uploadFile", params={"uniqueId": uid},
                                  files={"file": ("pairs.zip", package)},
                                  timeout=60.0).json()
            check("上传 %s" % uid, uploaded["code"] == 0, uploaded["msg"])

        first = httpx.post(BASE_URL + "/predict",
                           json={"uniqueId": "api_smoke_a", "isLastOperator": False},
                           timeout=600.0).json()
        # [v4.2] 形态二统一响应：去 lineageFile，始终返回 operator/status/outputFile/result
        check("形态二统一响应（无 lineageFile）",
              first["code"] == 0 and set(first["data"]) == {"operator", "status",
                                                          "process_status", "outputFile",
                                                          "result"},
              "status=%s" % first["data"].get("process_status"))
        node = first["data"]["result"]
        temp_rel = node["files"]["outputFile"]
        check("业务摘要含任务相对产物路径",
              bool(temp_rel) and (output_root / "api_smoke_a" / temp_rel).is_file(),
              "temp=%s" % temp_rel)
        temp_file = temp_rel
        stats = node["statistics"]
        check("统计守恒", stats["conservation_ok"] is True,
              "input=%d output=%d isolated=%d pending=%d rejected=%d"
              % (stats["input"], stats["output"], stats["isolated"],
                 stats["pending_model"], stats["rejected"]))

        tail = httpx.post(BASE_URL + "/predict",
                          json={"uniqueId": "api_smoke_a", "isLastOperator": True,
                                "inputFile": temp_file}, timeout=600.0).json()
        check("形态二响应始终含 outputFile",
              tail["code"] == 0 and set(tail["data"]) == {"operator", "status",
                                                        "process_status", "outputFile",
                                                        "result"})
        files = tail["data"]["outputFile"]
        check("阶段末文件对象格式", files["format"] == "crossmodal-files-v1"
              and files["entrypoint"] == "12_image_text_scores.jsonl",
              "files=%d" % len(files["files"]))

        second = httpx.post(BASE_URL + "/predict",
                            json={"uniqueId": "api_smoke_b", "isLastOperator": False},
                            timeout=600.0).json()
        check("两条 uniqueId 产物隔离",
              second["code"] == 0
              and second["data"]["result"]["files"]["outputFile"] != temp_file,
              "b=%s" % second["data"]["result"]["files"]["outputFile"])

        bad_uid = httpx.post(BASE_URL + "/predict",
                             json={"uniqueId": "../escape", "isLastOperator": False},
                             timeout=30.0).json()
        check("非法 uniqueId 被拒绝", bad_uid["code"] == 1, bad_uid["msg"])

        traversal = httpx.post(BASE_URL + "/predict",
                               json={"uniqueId": "api_smoke_a", "isLastOperator": False,
                                     "inputFile": "../api_smoke_b/input/pairs.jsonl"},
                               timeout=30.0).json()
        check("跨任务 inputFile 被拒绝", traversal["code"] == 1, traversal["msg"])

        bad_flag = httpx.post(BASE_URL + "/predict",
                              json={"uniqueId": "api_smoke_a", "isLastOperator": "false"},
                              timeout=30.0).json()
        check("isLastOperator 非布尔被拒绝", bad_flag["code"] == 1, bad_flag["msg"])

        check("不存在 /downloadFile",
              httpx.get(BASE_URL + "/downloadFile",
                        params={"uniqueId": "api_smoke_a"}).status_code == 404)

        removed = httpx.post(BASE_URL + "/removeFile",
                             params={"uniqueId": "api_smoke_a"}, timeout=60.0).json()
        again = httpx.post(BASE_URL + "/removeFile",
                           params={"uniqueId": "api_smoke_a"}, timeout=60.0).json()
        check("removeFile 幂等且不误删其他任务",
              removed["code"] == 0 and removed["data"]["deleted"] is True
              and again["data"]["deleted"] is False
              and (output_root / "api_smoke_b").is_dir())

        print("\n冒烟全部通过：%d 项（output_root=%s）" % (len(CHECKS), output_root))
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


if __name__ == "__main__":
    sys.exit(main())
