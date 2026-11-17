"""真实服务冒烟：启动 uvicorn 子进程 + httpx 走三接口全链路（非 TestClient）。

用法::

    python tests/smoke_api.py [port]

退出码 0 表示全部通过。不联网、不加载模型。
"""
from __future__ import annotations

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
from typing import Any, Dict, List, Tuple

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from conftest import (DEFAULT_RECORDS, build_input_zip, build_scenario,  # noqa: E402
                      token_record)

from utils import delivery, transport  # noqa: E402

CHECKS: List[Tuple[str, bool, str]] = []


def check(name: str, condition: Any, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


def free_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])


def wait_ready(base_url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = httpx.get(base_url + "/health", timeout=2.0)
            if response.status_code == 200:
                return True
        except Exception:                                  # noqa: BLE001
            time.sleep(0.4)
    return False


def main(argv: List[str]) -> int:
    port = free_port(int(argv[1]) if len(argv) > 1 else 8123)
    base_url = "http://127.0.0.1:%d" % port
    workdir = Path(tempfile.mkdtemp(prefix="op23_smoke_"))
    output_root = workdir / "output"
    output_root.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update({"OUTPUT_DIR": str(output_root), "PYTHONIOENCODING": "utf-8",
                "OPERATOR_LOG_LEVEL": "WARNING"})
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BASE), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8")

    try:
        if not wait_ready(base_url):
            print("服务未就绪，输出如下：")
            print(process.stdout.read() if process.stdout else "")
            return 1

        client = httpx.Client(base_url=base_url, timeout=60.0)

        health = client.get("/health").json()
        check("health 信封与编号", health["code"] == 0
              and health["data"]["formal_operator_number"] == 23, str(health))
        check("health 声明三接口且无 /downloadFile",
              health["data"]["endpoints"] == ["/uploadFile", "/predict", "/removeFile"]
              and health["data"]["downloadFile"] == "not_registered")

        ready_zip = build_input_zip(workdir, "ready.zip", written_back=True)
        pending_zip = build_input_zip(workdir, "pending.zip", written_back=False)

        with open(ready_zip, "rb") as handle:
            upload_one = client.post("/uploadFile", params={"uniqueId": "smoke_one"},
                                     files={"file": ("ready.zip", handle, "application/zip")})
        check("上传 ready 包成功", upload_one.status_code == 200
              and upload_one.json()["code"] == 0, upload_one.text[:200])
        with open(pending_zip, "rb") as handle:
            upload_two = client.post("/uploadFile", params={"uniqueId": "smoke_two"},
                                     files={"file": ("pending.zip", handle, "application/zip")})
        check("上传 pending 包成功", upload_two.status_code == 200
              and upload_two.json()["code"] == 0)

        mid = client.post("/predict", json={"uniqueId": "smoke_one",
                                            "isLastOperator": False}).json()
        check("中间态 data 仅含 lineageFile",
              mid["code"] == 0 and set(mid["data"]) == {"lineageFile"}, str(mid["data"].keys()))
        node = mid["data"]["lineageFile"]
        check("中间态血缘含临时 outputFile",
              bool(node.get("outputFile")) and Path(node["outputFile"]).is_file())
        check("中间态统计真实",
              node["statistics"]["packaged_records"] == 10
              and node["statistics"]["excluded_isolated"] == 3
              and node["statistics"]["conservation_ok"] is True, str(node["statistics"]))

        pending_mid = client.post("/predict", json={"uniqueId": "smoke_two",
                                                    "isLastOperator": False}).json()
        check("两条任务互相隔离",
              pending_mid["data"]["lineageFile"]["uniqueId"] == "smoke_two"
              and pending_mid["data"]["lineageFile"]["statistics"]["version_state"]
              == delivery.VERSION_STATE_PENDING)
        check("待发布任务为 degraded 且可交付为 success",
              mid["msg"] and pending_mid["code"] == 0)

        relative = Path(node["outputFile"]).relative_to(
            output_root / "smoke_one").as_posix()
        chained = client.post("/predict", json={"uniqueId": "smoke_one",
                                                "isLastOperator": True,
                                                "inputFile": relative}).json()
        check("阶段内传入上一节点 outputFile 可复用", chained["code"] == 0,
              chained.get("msg", ""))
        check("阶段末 data 恰为 outputFile + lineageFile",
              set(chained["data"]) == {"outputFile", "lineageFile"})
        file_object = chained["data"]["outputFile"]
        check("阶段末文件对象为 crossmodal-files-v1",
              file_object["format"] == transport.FILE_OBJECT_FORMAT
              and file_object["entrypoint"] == delivery.MAIN_NAME)
        paths = [item["path"] for item in file_object["files"]]
        check("阶段末包含交付 ZIP 与清单",
              delivery.PACKAGE_DELIVERABLE_NAME in paths and "manifest.json" in paths,
              str(paths))
        stage_lineage = chained["data"]["lineageFile"]
        check("阶段末血缘为数组且剔除临时 outputFile",
              isinstance(stage_lineage, list)
              and all("outputFile" not in item for item in stage_lineage))

        restored = workdir / "restored"
        entry_path = transport.restore_file_object(file_object, restored)
        rows = [json.loads(line) for line in
                entry_path.read_text(encoding="utf-8").strip().splitlines()]
        check("文件对象可逐字节恢复且入口行数一致", len(rows) == 10, str(len(rows)))
        package = restored / delivery.PACKAGE_DELIVERABLE_NAME
        with zipfile.ZipFile(package) as bundle:
            manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
            train = bundle.read("train/samples.jsonl").decode("utf-8").strip().splitlines()
        check("交付包 manifest 为可交付且训练文件无隔离样本",
              manifest["version_state"] == delivery.VERSION_STATE_DELIVERABLE
              and len(train) == 10
              and all("cm-0011" not in line and "cm-0013" not in line for line in train))

        bad_id = client.post("/predict", json={"uniqueId": "../escape",
                                               "isLastOperator": False})
        check("非法 uniqueId 被拒绝", bad_id.status_code == 422
              and bad_id.json()["code"] == 1)
        escape = client.post("/predict", json={
            "uniqueId": "smoke_one", "isLastOperator": False,
            "inputFile": "../smoke_two/input/samples.jsonl"})
        check("跨任务 inputFile 被拒绝", escape.status_code == 400
              and escape.json()["code"] == 1)
        strict_bool = client.post("/predict", json={"uniqueId": "smoke_one",
                                                    "isLastOperator": "false"})
        check("isLastOperator 非严格布尔被拒绝", strict_bool.status_code == 422)
        download = client.get("/downloadFile", params={"uniqueId": "smoke_one"})
        check("/downloadFile 未注册", download.status_code == 404)

        slip_zip = workdir / "slip.zip"
        with zipfile.ZipFile(slip_zip, "w") as bundle:
            bundle.writestr("../escape.jsonl", "{}")
            bundle.writestr("samples.jsonl", "{}\n")
        with open(slip_zip, "rb") as handle:
            slip = client.post("/uploadFile", params={"uniqueId": "smoke_slip"},
                               files={"file": ("slip.zip", handle, "application/zip")})
        check("zip 目录穿越被拒绝",
              slip.status_code == 422 and slip.json()["code"] == 1)

        removed = client.post("/removeFile", params={"uniqueId": "smoke_two"}).json()
        again = client.post("/removeFile", params={"uniqueId": "smoke_two"}).json()
        check("removeFile 幂等且只清本任务",
              removed["data"]["removed"] is True and again["data"]["removed"] is False
              and (output_root / "smoke_one").is_dir())

        client.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:                  # pragma: no cover
            process.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    for name, ok, detail in CHECKS:
        print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                           (" | " + detail) if (detail and not ok) else ""))
    print("共 %d 项检查，通过 %d 项，失败 %d 项" % (len(CHECKS), passed, len(CHECKS) - passed))
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
