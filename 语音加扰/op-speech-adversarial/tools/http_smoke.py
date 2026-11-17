#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证六接口：上传、01→02 按返回路径串联、下载产物/音频及清理。"""

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


def request_json(base, path, timeout, *, body=None, content_type=None):
    headers = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(base + path, data=body, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError("HTTP %s %s: %s" % (exc.code, path, detail)) from exc
    except URLError as exc:
        raise RuntimeError("无法访问 %s: %s" % (base, exc.reason)) from exc


def check_success(payload, label):
    if payload.get("code") != 0:
        raise RuntimeError("%s 业务失败: %s" % (label, payload.get("msg", payload)))
    return payload["data"]


def download_file(base, path, timeout):
    # Match the engineering backend exactly: GET with a full path, no task/operator query.
    query = urlencode({"path": path})
    request = Request(base + "/downloadFile?" + query)
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:17023")
    parser.add_argument("--input", required=True, type=Path, help="小型 ZIP，含音频及同名 UTF-8 .txt")
    parser.add_argument("--timeout", default=1800, type=float, help="预测请求超时，单位秒")
    parser.add_argument("--keep", action="store_true", help="保留本次独立任务的输入输出")
    args = parser.parse_args()
    if not args.input.is_file() or args.input.suffix.lower() != ".zip":
        parser.error("--input 必须是已存在的 ZIP 文件")
    base = args.base_url.rstrip("/")
    url = urlsplit(base)
    if url.scheme not in ("http", "https") or not url.netloc:
        parser.error("--base-url 必须是 http(s) 服务地址")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")

    unique_id = "http_smoke_" + uuid.uuid4().hex
    print("baseUrl=%s\nuniqueId=%s" % (base, unique_id), flush=True)
    uploaded = False
    succeeded = False
    cleanup_failed = False
    try:
        health = request_json(base, "/health", 10)
        if health.get("status") != 1 or health.get("operators") != 11:
            raise RuntimeError("health 异常: %s" % health)
        if health.get("capabilities", {}).get("downloadFile") is not True:
            raise RuntimeError("服务器尚未启用 downloadFile，请覆盖新版 inference.py 并重建镜像/容器")
        print("PASS GET /health (operators=11)", flush=True)

        registry = check_success(request_json(base, "/operators", 10), "operators")
        if registry.get("count") != 11 or len(registry.get("operators", [])) != 11:
            raise RuntimeError("注册算子数量不是 11")
        print("PASS GET /operators (count=11)", flush=True)

        boundary = "avp-smoke-" + uuid.uuid4().hex
        body = (
            ('--%s\r\nContent-Disposition: form-data; name="file"; filename="corpus.zip"\r\n'
             'Content-Type: application/zip\r\n\r\n' % boundary).encode("ascii")
            + args.input.read_bytes()
            + ("\r\n--%s--\r\n" % boundary).encode("ascii")
        )
        # 请求可能在落盘后才断连；从发起上传起就清理自己的唯一任务。
        uploaded = True
        upload = check_success(request_json(
            base, "/uploadFile?" + urlencode({"uniqueId": unique_id}), args.timeout,
            body=body, content_type="multipart/form-data; boundary=" + boundary,
        ), "uploadFile")
        input_file = upload["inputFile"]
        print("PASS POST /uploadFile inputFile=%s" % input_file, flush=True)
        uploaded_bytes = download_file(base, upload["archive"], args.timeout)
        if uploaded_bytes != args.input.read_bytes():
            raise RuntimeError("上传 ZIP 下载后字节不一致")
        print("PASS GET /downloadFile uploaded ZIP (exact bytes)", flush=True)

        lineage = []
        steps = [
            ("avp_ingest_corpus", {
                "dataset_name": "服务器HTTP冒烟", "source": "自主采集", "manifest_mode": "none",
            }),
            ("avp_normalize_audio", {
                "sample_rate": 16000, "channels": 1, "format": "wav_pcm16",
                "min_duration": 0.01, "max_duration": 60.0,
            }),
        ]
        for operator, params in steps:
            prediction = check_success(request_json(
                base, "/predict", args.timeout,
                body=json.dumps({
                    "operator": operator, "uniqueId": unique_id, "inputFile": input_file,
                    "isLastOperator": False, "lineageFile": lineage, "params": params,
                }, ensure_ascii=False).encode("utf-8"),
                content_type="application/json",
            ), operator)
            if prediction.get("status") not in ("success", "partial"):
                raise RuntimeError("预测状态异常: %s" % prediction.get("status"))
            record = prediction["lineageFile"]
            if not isinstance(record, dict) or record.get("inputFile") != input_file:
                raise RuntimeError("中间血缘或 inputFile 接续异常: %s" % record)
            input_file = record["outputFile"]
            if not input_file.startswith("/data/" + unique_id + "/"):
                raise RuntimeError("产物不在当前任务目录: %s" % input_file)
            lineage.append(record)
            content = download_file(base, input_file, args.timeout)
            rows = [json.loads(line) for line in content.decode("utf-8-sig").splitlines() if line.strip()]
            if not rows:
                raise RuntimeError("下载的语料表为空，无法继续验证链路")
            audio = download_file(base, rows[0]["audio_path"], args.timeout)
            if not audio or (operator == "avp_normalize_audio" and not audio.startswith(b"RIFF")):
                raise RuntimeError("下载的音频为空或标准化 WAV 文件头异常")
            print("PASS POST /predict %s status=%s outputFile=%s" % (
                operator, prediction["status"], input_file,
            ), flush=True)
            print("PASS GET /downloadFile corpus=%d rows audio=%d bytes sha256=%s" % (
                len(rows), len(audio), hashlib.sha256(content).hexdigest(),
            ), flush=True)
        succeeded = True
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        print("FAIL %s" % exc, file=sys.stderr, flush=True)
    finally:
        if uploaded and not args.keep:
            try:
                removed = check_success(request_json(
                    base, "/removeFile?" + urlencode({"uniqueId": unique_id}), 30, body=b"",
                ), "removeFile")
                if removed.get("removed") != "/data/" + unique_id:
                    raise RuntimeError("删除响应路径与本次任务不一致")
                if succeeded:
                    try:
                        download_file(base, input_file, 10)
                    except HTTPError as exc:
                        missing = json.load(exc)
                        if exc.code != 404 or missing.get("code") != 1 or not missing.get("msg", "").startswith("missing_file:"):
                            raise RuntimeError("清理后的下载未返回预期 missing_file/404") from exc
                    else:
                        raise RuntimeError("任务清理后仍能下载原产物")
                print("PASS POST /removeFile (本次冒烟任务已清理)", flush=True)
            except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
                cleanup_failed = True
                print("FAIL 清理任务 %s: %s" % (unique_id, exc), file=sys.stderr, flush=True)
        elif uploaded:
            print("KEEP 任务 %s 的文件已保留" % unique_id, flush=True)
    return 0 if succeeded and not cleanup_failed else 1


if __name__ == "__main__":
    sys.exit(main())
