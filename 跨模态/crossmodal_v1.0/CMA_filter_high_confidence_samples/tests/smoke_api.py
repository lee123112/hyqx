"""真实服务 smoke：用 uvicorn 起进程 + httpx 走 HTTP，验证三接口与阶段末文件对象。

与 ``tests/test_api.py``（进程内 TestClient）互补：本脚本验证**真实监听端口**下的行为。

运行::

    python tests/smoke_api.py            # 默认端口 8129
    python tests/smoke_api.py 8130       # 指定端口

退出码：0 全部通过；1 有断言失败。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

BASE = Path(__file__).resolve().parents[1]
SAMPLE_DIR = BASE / "dataset" / "sample"

CHECKS: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         (" — " + detail) if detail else ""))


def free_port(preferred: int) -> int:
    for candidate in (preferred, preferred + 1, preferred + 2):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def build_bundle() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(SAMPLE_DIR / "demo.jsonl", "demo.jsonl")
        bundle.writestr("manifest.json", json.dumps(
            {"format": "crossmodal-manifest-v1", "entrypoint": "demo.jsonl",
             "artifacts": [{"path": "demo.jsonl", "role": "main"}]},
            ensure_ascii=False))
    return buffer.getvalue()


def wait_ready(client: httpx.Client, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get("/health").status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    return False


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    preferred = int(argv[0]) if argv else 8129
    port = free_port(preferred)
    workdir = Path(tempfile.mkdtemp(prefix="smoke_api_19_"))
    output_dir = workdir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"OUTPUT_DIR": str(output_dir),
                "OPERATOR_CONFIG": str(BASE / "configs" / "config.yaml"),
                "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(BASE)})
    log = open(workdir / "uvicorn.log", "w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BASE), env=env, stdout=log, stderr=subprocess.STDOUT)
    base_url = "http://127.0.0.1:%d" % port
    try:
        with httpx.Client(base_url=base_url, timeout=60.0) as client:
            if not wait_ready(client):
                check("服务启动", False, "30s 内 /health 不可用")
                return _finish(process, log, workdir)

            health = client.get("/health").json()
            check("GET /health 返回通知信封", health["code"] == 0
                  and health["data"]["formal_operator_number"] == 19,
                  json.dumps(health["data"], ensure_ascii=False)[:160])
            check("/downloadFile 不存在",
                  client.get("/downloadFile").status_code == 404
                  and client.post("/downloadFile").status_code == 404)

            bundle = build_bundle()
            upload = client.post("/uploadFile", params={"uniqueId": "smoke_a"},
                                 files={"file": ("bundle.zip", bundle,
                                                 "application/zip")})
            body = upload.json()
            check("POST /uploadFile 成功", upload.status_code == 200
                  and body["code"] == 0 and body["data"]["entrypoint"] == "demo.jsonl",
                  "files=%d" % len(body["data"].get("files", [])))

            bad = client.post("/uploadFile", params={"uniqueId": "../escape"},
                              files={"file": ("b.zip", bundle, "application/zip")})
            check("非法 uniqueId 被拒绝", bad.status_code == 422
                  and bad.json()["code"] == 1)

            first = client.post("/predict", json={"uniqueId": "smoke_a",
                                                  "isLastOperator": False})
            first_body = first.json()
            data = first_body["data"]
            check("中间响应 data 为 operator/status/outputFile/result",
                  set(data) == {"operator", "status", "outputFile", "result"}
                  and first_body["code"] == 0)
            stats = data["result"]["statistics"]
            check("中间响应含真实统计",
                  bool(data["result"].get("outputFile"))
                  and stats["input_records"] == 21
                  and stats["admitted_records"] == 6
                  and stats["sample_isolated"] == 6
                  and stats["pending_records"] == 4
                  and stats["origin_quarantined"] == 3,
                  "admit=%s iso=%s pend=%s origin=%s cons=%s"
                  % (stats["admitted_records"], stats["sample_isolated"],
                     stats["pending_records"], stats["origin_quarantined"],
                     stats["conservation_ok"]))

            last = client.post("/predict", json={"uniqueId": "smoke_a",
                                                 "isLastOperator": True})
            last_body = last.json()
            check("阶段末返回 outputFile + result",
                  set(last_body["data"]) == {"operator", "status", "outputFile", "result"})
            file_object = last_body["data"]["outputFile"]
            paths = [item["path"] for item in file_object["files"]]
            check("文件对象为 crossmodal-files-v1 且含四类台账",
                  file_object["format"] == "crossmodal-files-v1"
                  and "19_candidate_pool.jsonl" in paths
                  and "19_sample_quarantine.jsonl" in paths
                  and "19_pending.jsonl" in paths
                  and "19_rejects.jsonl" in paths
                  and "manifest.json" in paths,
                  "entrypoint=%s" % file_object["entrypoint"])

            # 恢复文件对象 → 重新上传 → 作为下一阶段输入
            restored_dir = workdir / "restored"
            restored_dir.mkdir(parents=True, exist_ok=True)
            pool_entry = None
            for item in file_object["files"]:
                target = restored_dir / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if item["encoding"] == "base64":
                    import base64
                    target.write_bytes(base64.b64decode(item["content"]))
                else:
                    target.write_text(item["content"], encoding="utf-8")
                if item["path"] == file_object["entrypoint"]:
                    pool_entry = target
            check("阶段末文件对象可恢复到本地",
                  pool_entry is not None and pool_entry.is_file()
                  and len([line for line in pool_entry.read_text(
                      encoding="utf-8").splitlines() if line.strip()]) == 6)

            second_bundle = io.BytesIO()
            with zipfile.ZipFile(second_bundle, "w", zipfile.ZIP_DEFLATED) as bundle2:
                bundle2.writestr("19_candidate_pool.jsonl",
                                 pool_entry.read_text(encoding="utf-8"))
                bundle2.writestr("manifest.json", json.dumps(
                    {"format": "crossmodal-manifest-v1",
                     "entrypoint": "19_candidate_pool.jsonl",
                     "artifacts": [{"path": "19_candidate_pool.jsonl",
                                    "role": "main"}]}, ensure_ascii=False))
            client.post("/uploadFile", params={"uniqueId": "smoke_next"},
                        files={"file": ("b2.zip", second_bundle.getvalue(),
                                        "application/zip")})
            again = client.post("/predict", json={"uniqueId": "smoke_next",
                                                  "isLastOperator": False}).json()
            check("入池结果可作为下一阶段输入再跑",
                  again["code"] == 0
                  and again["data"]["result"]["statistics"]["input_records"] == 6,
                  "input_records=%s"
                  % again["data"]["result"]["statistics"]["input_records"])

            # 任务隔离 + 非法参数
            client.post("/uploadFile", params={"uniqueId": "smoke_b"},
                        files={"file": ("b.zip", bundle, "application/zip")})
            other = client.post("/predict", json={"uniqueId": "smoke_b",
                                                  "isLastOperator": False}).json()
            check("两个任务互不污染",
                  other["data"]["result"]["uniqueId"] == "smoke_b"
                  and data["result"]["uniqueId"] == "smoke_a")

            cross = output_dir / "smoke_a" / data["result"]["outputFile"]
            relative = cross.relative_to(output_dir).as_posix()
            cross_resp = client.post("/predict", json={
                "uniqueId": "smoke_b", "isLastOperator": False,
                "inputFile": relative})
            check("跨任务 inputFile 被拒绝", cross_resp.status_code == 400
                  and cross_resp.json()["code"] == 1,
                  "status=%s" % cross_resp.status_code)

            traversal = client.post("/predict", json={
                "uniqueId": "smoke_b", "isLastOperator": False,
                "inputFile": "../../etc/passwd"})
            check("穿越式 inputFile 被拒绝", traversal.status_code == 400)

            loose = client.post("/predict", json={"uniqueId": "smoke_b",
                                                  "isLastOperator": "false"})
            check("isLastOperator 非布尔被拒绝", loose.status_code == 422
                  and loose.json()["code"] == 1)

            removed = client.post("/removeFile", params={"uniqueId": "smoke_b"}).json()
            again_removed = client.post("/removeFile",
                                        params={"uniqueId": "smoke_b"}).json()
            check("removeFile 幂等且限定任务",
                  removed["data"]["removed"] is True
                  and again_removed["data"]["removed"] is False
                  and not (output_dir / "smoke_b").exists()
                  and (output_dir / "smoke_a").exists())
    finally:
        return _finish(process, log, workdir)


def _finish(process: subprocess.Popen, log, workdir: Path) -> int:
    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:                              # noqa: BLE001
        process.kill()
    log.close()
    failed = [name for name, ok, _ in CHECKS if not ok]
    print("\n合计 %d 项，通过 %d 项，失败 %d 项"
          % (len(CHECKS), len(CHECKS) - len(failed), len(failed)))
    if failed:
        print("失败项：" + "，".join(failed))
        print("服务日志：%s" % (workdir / "uvicorn.log"))
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
