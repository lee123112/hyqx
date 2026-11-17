"""HTTP 接口 smoke：启动真实 uvicorn 服务并走完三接口全流程。

验证点（对应任务要求）：
1. 两个 uniqueId 的产物物理隔离；
2. 非法 inputFile（目录穿越 / 跨任务路径）被拒绝；
3. v4.2 形态二统一返回 ``outputFile`` + ``result``（不再返回血缘）；
4. ``/downloadFile`` 未注册（404）；
5. ``/removeFile`` 幂等。

运行：``python tests/smoke_api.py``（退出码 0 = 全部通过）
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

BASE = Path(__file__).resolve().parents[1]
SAMPLE = BASE / "dataset" / "sample" / "demo.jsonl"
OUTPUT_ROOT = BASE / "output_smoke_api"
PORT = int(os.environ.get("SMOKE_PORT", "8122"))
HOST = "127.0.0.1"
BASE_URL = "http://%s:%d" % (HOST, PORT)

CHECKS: List[Tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(passed), detail))
    print("[%s] %s%s" % ("PASS" if passed else "FAIL", name,
                         ("" if passed else " -> %s" % detail)))


def build_bundle() -> bytes:
    buffer = io.BytesIO()
    content = SAMPLE.read_text(encoding="utf-8")
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(SAMPLE.name, content)
        bundle.writestr("media/demo-0001.png", b"\x89PNG\r\n\x1a\nfake-binary")
        bundle.writestr("manifest.json", json.dumps({
            "format": "crossmodal-manifest-v1", "entrypoint": SAMPLE.name,
            "artifacts": [{"path": SAMPLE.name, "role": "records"},
                          {"path": "media/demo-0001.png", "role": "media"}]},
            ensure_ascii=False))
    return buffer.getvalue()


def wait_ready(client: httpx.Client, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get("%s/health" % BASE_URL, timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            time.sleep(0.4)
    return False


def main() -> int:
    if not SAMPLE.is_file():
        print("样例不存在，请先运行 python dataset/sample/make_sample.py")
        return 2
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT, ignore_errors=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["OUTPUT_DIR"] = str(OUTPUT_ROOT)
    env["OPERATOR_CONFIG"] = str(BASE / "configs" / "config.yaml")
    env["PYTHONPATH"] = str(BASE)
    server_log = open(OUTPUT_ROOT / "uvicorn.log", "w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", HOST,
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(BASE), env=env, stdout=server_log, stderr=subprocess.STDOUT,
        text=True)

    exit_code = 0
    try:
        with httpx.Client(timeout=120.0) as client:
            if not wait_ready(client):
                print("服务未能在超时时间内就绪")
                return 3
            bundle = build_bundle()
            files = {"file": ("bundle.zip", bundle, "application/zip")}

            up_a = client.post("%s/uploadFile" % BASE_URL,
                               params={"uniqueId": "smoke_a"}, files=files)
            up_b = client.post("%s/uploadFile" % BASE_URL,
                               params={"uniqueId": "smoke_b"}, files=files)
            check("uploadFile 任务 A 返回 code=0",
                  up_a.status_code == 200 and up_a.json()["code"] == 0, up_a.text[:200])
            check("uploadFile 任务 B 返回 code=0",
                  up_b.status_code == 200 and up_b.json()["code"] == 0, up_b.text[:200])
            entry = up_a.json()["data"]["entrypoint"]
            check("uploadFile 声明入口为 demo.jsonl", entry == SAMPLE.name, entry)

            bad = client.post("%s/uploadFile" % BASE_URL,
                              params={"uniqueId": "../escape"}, files=files)
            check("非法 uniqueId 被拒（422）", bad.status_code == 422, bad.text[:200])

            form1 = client.post(
                "%s/predict" % BASE_URL,
                params={"detection": "generate_crossmodal_tokens",
                        "uniqueId": "smoke_form1_file01"},
                files={"file": (SAMPLE.name, SAMPLE.read_bytes(), "application/jsonl")})
            form1_body = form1.json()
            check("形态一单调用返回 status=1",
                  form1.status_code == 200 and form1_body.get("status") == 1,
                  form1.text[:200])
            check("形态一返回 uniqueId/runDir/outputFile",
                  form1_body.get("uniqueId") == "smoke_form1_file01"
                  and bool(form1_body.get("runDir"))
                  and bool(form1_body.get("outputFile")), str(form1_body)[:300])
            check("形态一样本数/隔离数正确",
                  form1_body.get("sample_count") == 3
                  and form1_body.get("isolated_skipped_count") == 1, str(form1_body)[:300])

            mid = client.post("%s/predict" % BASE_URL,
                              json={"uniqueId": "smoke_a", "isLastOperator": False})
            mid_body = mid.json()
            mid_data = mid_body.get("data", {})
            mid_result = mid_data.get("result", {})
            check("v4.2 中间调用返回 outputFile + result",
                  mid.status_code == 200 and sorted(mid_data) == ["operator", "outputFile", "result", "status"],
                  str(sorted(mid_data)))
            stats = mid_result.get("statistics", {})
            check("中间结果含 runDir/outputFile",
                  bool(mid_result.get("runDir")) and bool(mid_result.get("outputFile")))
            check("统计守恒 conservation_ok=true",
                  stats.get("conservation_ok") is True, str(stats.get("conservation_failures")))
            check("生成 3 条 Token（图文）", stats.get("generated_records") == 3,
                  str(stats.get("generated_records")))
            check("隔离样本未进入样本文件", stats.get("isolated_skipped_records") == 1,
                  str(stats.get("isolated_skipped_records")))
            check("不可训练样本单独列出", stats.get("untrainable_records") == 1,
                  str(stats.get("untrainable_records")))
            check("flagged 旁路样本单列", stats.get("flagged_records") == 1,
                  str(stats.get("flagged_records")))

            mid_b = client.post("%s/predict" % BASE_URL,
                                json={"uniqueId": "smoke_b", "isLastOperator": False})
            node_a = mid_result.get("runDir")
            node_b = mid_b.json()["data"]["result"].get("runDir")
            check("两个 uniqueId 产物物理隔离", bool(node_a) and bool(node_b)
                  and node_a != node_b, "%s / %s" % (node_a, node_b))
            check("任务 A 目录归属正确", "smoke_a" in str(node_a), str(node_a))

            cross = client.post("%s/predict" % BASE_URL,
                                json={"uniqueId": "smoke_b", "isLastOperator": False,
                                      "inputFile": mid_result["outputFile"].split("output_smoke_api", 1)[-1].lstrip("\\/")})
            check("跨任务 inputFile 被拒（400）", cross.status_code == 400,
                  cross.text[:200])

            traversal = client.post("%s/predict" % BASE_URL,
                                    json={"uniqueId": "smoke_a", "isLastOperator": False,
                                          "inputFile": "../../../etc/passwd"})
            check("目录穿越 inputFile 被拒（400）", traversal.status_code == 400,
                  traversal.text[:200])

            strict = client.post("%s/predict" % BASE_URL,
                                 json={"uniqueId": "smoke_a", "isLastOperator": "false"})
            check("isLastOperator 非布尔被拒（422）", strict.status_code == 422,
                  strict.text[:200])

            last = client.post("%s/predict" % BASE_URL,
                               json={"uniqueId": "smoke_a", "isLastOperator": True})
            last_body = last.json()
            last_data = last_body.get("data", {})
            last_result = last_data.get("result", {})
            check("v4.2 阶段末返回 outputFile + result",
                  last.status_code == 200
                  and sorted(last_data) == ["operator", "outputFile", "result", "status"],
                  str(sorted(last_data)))
            file_object = last_data.get("outputFile", {})
            paths = [item["path"] for item in file_object.get("files", [])]
            check("阶段末文件对象含 samples.jsonl", "samples.jsonl" in paths, str(paths))
            check("阶段末文件对象含 22_tokens.jsonl",
                  "22_tokens.jsonl" in paths, str(paths))
            check("阶段末文件对象含隔离清单",
                  "22_isolated_skipped.jsonl" in paths, str(paths))
            check("阶段末文件对象为 crossmodal-files-v1",
                  file_object.get("format") == "crossmodal-files-v1")
            check("阶段末 result 统计为真实数量",
                  last_result.get("statistics", {}).get("generated_records") == 3)

            sample_text = next(item["content"] for item in file_object["files"]
                               if item["path"] == "samples.jsonl")
            first_line = json.loads(sample_text.splitlines()[0])
            token_text = first_line["processing"]["22"]["token_text"]
            check("默认图文 Token 以 <image> 开头",
                  token_text.startswith("<image>"), token_text[:40])

            download = client.get("%s/downloadFile" % BASE_URL)
            check("/downloadFile 未注册（404）", download.status_code == 404,
                  str(download.status_code))

            rm_a = client.post("%s/removeFile" % BASE_URL, params={"uniqueId": "smoke_a"})
            rm_a2 = client.post("%s/removeFile" % BASE_URL, params={"uniqueId": "smoke_a"})
            check("removeFile 首次 removed=true",
                  rm_a.json()["data"]["removed"] is True, rm_a.text[:200])
            check("removeFile 幂等 removed=false",
                  rm_a2.json()["data"]["removed"] is False, rm_a2.text[:200])
            check("任务 A 目录已删除", not (OUTPUT_ROOT / "smoke_a").exists())
            check("任务 B 目录未被影响", (OUTPUT_ROOT / "smoke_b").is_dir())
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:                        # pragma: no cover
            server.kill()
        server_log.close()
    failed = [item for item in CHECKS if not item[1]]
    summary: Dict[str, Any] = {"total": len(CHECKS), "passed": len(CHECKS) - len(failed),
                               "failed": [item[0] for item in failed],
                               "details": [{"name": name, "ok": ok, "detail": detail}
                                           for name, ok, detail in failed]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failed:
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
