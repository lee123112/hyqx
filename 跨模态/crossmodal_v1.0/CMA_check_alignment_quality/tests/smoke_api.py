"""真实 uvicorn HTTP 冒烟（**不是 TestClient**）。

启动真实服务进程 → httpx 走网络请求 → 覆盖：
上传/预测/删除三接口、中间与阶段末两种响应形态、两个 uniqueId 隔离、
非法 uniqueId、跨任务 inputFile、非布尔 isLastOperator、`/downloadFile` 404、
zip 目录穿越、删除幂等。

用法::

    python tests/smoke_api.py
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

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tests.conftest import png_bytes, write_mp4, write_wav  # noqa: E402

DEFAULT_PORT = int(os.environ.get("SMOKE_PORT", "8120"))

CHECKS: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    line = "[%s] %s" % (mark, name)
    if detail and not condition:
        line += " —— %s" % detail
    print(line)


def free_port(preferred: int) -> int:
    for candidate in (preferred, 0):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return probe.getsockname()[1]
    raise RuntimeError("无法分配端口")


def build_bundle(media_root: Path) -> bytes:
    media_root.mkdir(parents=True, exist_ok=True)
    write_wav(media_root / "tone.wav")
    write_mp4(media_root / "clip_a.mp4")
    rows = [
        {"sample_id": "smoke-0001", "text": {"content": "金毛犬在草地上接飞盘"},
         "image": {"path": "media/ok_a.png", "brisque": 30.0},
         "qc": {"reverse_verify": {"passed": True, "score": 0.6,
                                   "check_status": "checked"}}},
        {"sample_id": "smoke-0002", "text": {"content": "单模态样本"}},
        {"sample_id": "smoke-0003", "video": "media/clip_a.mp4",
         "audio": {"path": "media/tone.wav", "snr_db": 19.0}},
        "broken-json-line",
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("20_candidate_pool.jsonl",
                         "\n".join(item if isinstance(item, str)
                                   else json.dumps(item, ensure_ascii=False)
                                   for item in rows) + "\n")
        archive.writestr("media/ok_a.png", png_bytes())
        archive.writestr("media/tone.wav", (media_root / "tone.wav").read_bytes())
        archive.writestr("media/clip_a.mp4", (media_root / "clip_a.mp4").read_bytes())
        archive.writestr("manifest.json", json.dumps({
            "format": "crossmodal-manifest-v1",
            "entrypoint": "20_candidate_pool.jsonl",
            "artifacts": [{"path": "20_candidate_pool.jsonl", "role": "main"}],
        }))
    return buffer.getvalue()


def main() -> int:
    port = free_port(DEFAULT_PORT)
    output_dir = Path(tempfile.mkdtemp(prefix="op20_smoke_out_"))
    media_root = Path(tempfile.mkdtemp(prefix="op20_smoke_media_"))
    env = dict(os.environ)
    env["OUTPUT_DIR"] = str(output_dir)
    env["PYTHONPATH"] = str(BASE_DIR)
    env["PYTHONIOENCODING"] = "utf-8"

    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BASE_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    try:
        ready = False
        for _ in range(60):
            if process.poll() is not None:
                break
            try:
                if httpx.get(base + "/health", timeout=1.0).status_code == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001 - 启动期连接失败是正常的
                time.sleep(0.5)
        check("服务启动并 /health 就绪", ready)
        if not ready:
            output = process.stdout.read().decode("utf-8", "replace") if process.stdout else ""
            print(output[-2000:])
            return 1

        health = httpx.get(base + "/health", timeout=5.0).json()
        check("health 身份正确",
              health.get("operator") == "alignment_qc" and health.get("operatorNumber") == 20)

        bundle = build_bundle(media_root)

        for unique_id in ("smoke_task_a", "smoke_task_b"):
            response = httpx.post("%s/uploadFile?uniqueId=%s" % (base, unique_id),
                                  files={"file": ("bundle.zip", bundle, "application/zip")},
                                  timeout=30.0).json()
            check("上传 %s" % unique_id, response.get("code") == 0, str(response))

        middle = httpx.post(base + "/predict", json={
            "uniqueId": "smoke_task_a", "isLastOperator": False}, timeout=60.0).json()
        check("中间形态 code=0", middle.get("code") == 0, str(middle)[:400])
        check("中间 data 仅含 lineageFile", set(middle.get("data", {})) == {"lineageFile"})
        node = middle.get("data", {}).get("lineageFile", {})
        check("中间血缘含临时 outputFile", bool(node.get("outputFile")))
        check("中间血缘 operator/stage 正确",
              node.get("operator") == "alignment_qc" and node.get("stageId") == "S4")

        last = httpx.post(base + "/predict", json={
            "uniqueId": "smoke_task_a", "isLastOperator": True}, timeout=60.0).json()
        check("阶段末 code=0", last.get("code") == 0, str(last)[:400])
        check("阶段末 data 含 outputFile+lineageFile",
              set(last.get("data", {})) == {"outputFile", "lineageFile"})
        check("阶段末 outputFile 是文件内容对象",
              last.get("data", {}).get("outputFile", {}).get("format")
              == "crossmodal-files-v1")
        lineage = last.get("data", {}).get("lineageFile")
        check("阶段末 lineageFile 是数组", isinstance(lineage, list))
        check("阶段末血缘已剔除临时 outputFile",
              all("outputFile" not in item for item in (lineage or [])))
        names = [item["path"] for item in
                 last.get("data", {}).get("outputFile", {}).get("files", [])]
        check("阶段末文件对象含质量报告",
              "20_quality_report.json" in names, str(names))
        check("阶段末文件对象含入口主清单",
              "20_alignment_qc.jsonl" in names, str(names))

        chained = httpx.post(base + "/predict", json={
            "uniqueId": "smoke_task_a", "isLastOperator": False,
            "inputFile": node.get("outputFile")}, timeout=60.0).json()
        check("阶段内串联（传入上一临时 outputFile）", chained.get("code") == 0,
              str(chained)[:300])

        other = httpx.post(base + "/predict", json={
            "uniqueId": "smoke_task_b", "isLastOperator": True}, timeout=60.0).json()
        ids = {item["uniqueId"] for item in other.get("data", {}).get("lineageFile", [])}
        check("两个 uniqueId 互不污染", ids == {"smoke_task_b"}, str(ids))

        bad_uid = httpx.post("%s/uploadFile?uniqueId=%s" % (base, "..%2Fescape"),
                             files={"file": ("a.jsonl", b"{}\n", "application/json")},
                             timeout=10.0).json()
        check("非法 uniqueId 被拒", bad_uid.get("code") == 1, str(bad_uid))

        not_found = httpx.get(base + "/downloadFile?uniqueId=x", timeout=10.0)
        check("/downloadFile 未实现（404）", not_found.status_code == 404)
        check("/downloadFile 仍是统一信封", not_found.json().get("code") == 1)

        bad_bool = httpx.post(base + "/predict", json={
            "uniqueId": "smoke_task_b", "isLastOperator": "false"}, timeout=10.0)
        check("非布尔 isLastOperator 被拒", bad_bool.status_code == 422)

        foreign = output_dir / "smoke_task_b" / "input" / "20_candidate_pool.jsonl"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("{}\n", encoding="utf-8")
        cross = httpx.post(base + "/predict", json={
            "uniqueId": "smoke_task_a", "isLastOperator": False,
            "inputFile": str(foreign)}, timeout=10.0).json()
        check("跨任务 inputFile 被拒", cross.get("code") == 1, str(cross))

        slip_buffer = io.BytesIO()
        with zipfile.ZipFile(slip_buffer, "w") as archive:
            archive.writestr("../escape.jsonl", "{}\n")
        slip = httpx.post("%s/uploadFile?uniqueId=smoke_slip" % base,
                          files={"file": ("b.zip", slip_buffer.getvalue(), "application/zip")},
                          timeout=10.0).json()
        check("zip 目录穿越被拒", slip.get("code") == 1, str(slip))

        first_remove = httpx.post(base + "/removeFile?uniqueId=smoke_task_a",
                                  timeout=10.0).json()
        second_remove = httpx.post(base + "/removeFile?uniqueId=smoke_task_a",
                                   timeout=10.0).json()
        check("removeFile 幂等", first_remove.get("code") == 0
              and second_remove.get("code") == 0, str(second_remove))
        check("removeFile 未误删其他任务", (output_dir / "smoke_task_b").is_dir())

        routes = set(httpx.get(base + "/openapi.json", timeout=10.0).json()["paths"])
        check("仅暴露三接口 + /health",
              routes == {"/uploadFile", "/predict", "/removeFile", "/health"}, str(routes))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        shutil.rmtree(output_dir, ignore_errors=True)
        shutil.rmtree(media_root, ignore_errors=True)

    passed = sum(1 for _name, ok, _detail in CHECKS if ok)
    failed = len(CHECKS) - passed
    print("\n共 %d 项检查，通过 %d 项，失败 %d 项" % (len(CHECKS), passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
