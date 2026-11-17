"""算子 26「生成交付样例」CLI（批量/离线调用，等价于 ``POST /predict``）。

示例::

    python processor.py --input dataset/sample/demo.jsonl \\
        --unique-id smoke_cli --output-root output_smoke
    python processor.py --input E:\\pkg\\23_deliverable_package.zip \\
        --unique-id region_a --output-root D:\\out --is-last-operator

退出码：``0`` 成功 / ``degraded``，``1`` 失败或参数错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import inference
from utils import io as io_utils
from utils import sampling, schemas


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="算子 26：生成交付样例（只从 #23 可交付语料包抽样导出 Excel 样例清单）")
    parser.add_argument("--input", required=True,
                        help="交付包入口（.jsonl / .zip，或包含交付包的目录）")
    parser.add_argument("--unique-id", required=True, help="任务唯一标识")
    parser.add_argument("--output-root", default=None,
                        help="输出根目录（默认 OUTPUT_DIR 或 ./output）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 configs/config.yaml）")
    parser.add_argument("--max-samples-per-route", type=int, default=None,
                        help="每条已开启路线抽样上限（1..10，表1 只读上限）")
    parser.add_argument("--sampling-seed", type=int, default=None, help="抽样种子")
    parser.add_argument("--is-last-operator", action="store_true",
                        help="[v4.2] 阶段末调用标记；响应形态与阶段内一致（outputFile + result）")
    parser.add_argument("--allow-external-input", action="store_true", default=True,
                        help="允许输入位于任务目录之外（CLI 默认允许）")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    overrides = {}
    if args.max_samples_per_route is not None:
        overrides["max_samples_per_route"] = args.max_samples_per_route
    if args.sampling_seed is not None:
        overrides["sampling_seed"] = args.sampling_seed
    try:
        result = inference.run(input_file=args.input, unique_id=args.unique_id,
                               config_path=args.config,
                               output_root=args.output_root,
                               overrides=overrides or None,
                               allow_external_input=True)
    except (ValueError, schemas.ConfigError, inference.InputError) as exc:
        print(json.dumps({"code": 1, "msg": str(exc), "data": {}},
                         ensure_ascii=False))
        return 1

    # [v4.2] 响应去 lineageFile：阶段内/阶段末统一 outputFile（crossmodal-files-v1）+ result
    statistics = dict(result["statistics"])
    statistics["input_file"] = statistics.get("entrypoint")
    data = {"outputFile": inference.build_file_object(result),
            "result": {"uniqueId": result["uniqueId"], "runId": result["runId"],
                       "status": result["status"], "message": result["message"],
                       "statistics": statistics,
                       "delivery_sample": sampling.EXCEL_NAME}}
    print(json.dumps({"code": 0, "msg": result["message"], "data": data},
                     ensure_ascii=False, indent=2))

    if result["status"] == "failed":
        return 1
    summary = result["statistics"]
    print("样例条数=%d | Excel=%s | 拒绝=%d | 排除=%d | 状态=%s"
          % (summary["sampled_records"], Path(result["excelFile"]).name,
             summary["rejects_entries"], summary["excluded_records"],
             result["status"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
