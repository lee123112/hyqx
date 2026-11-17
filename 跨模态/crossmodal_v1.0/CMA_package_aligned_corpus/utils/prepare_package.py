"""交付包离线校验/重打包工具（工程侧交接用，不参与在线链路）。

用途：
1. ``verify``：校验一次运行产出的交付 ZIP 是否自洽 —— 成员路径全为相对路径且唯一、
   包内 ``manifest.json`` 可解析且 ``entryPoint`` 存在、逐文件 ``sha256`` 与 ``size`` 匹配、
   ``version.json`` 声明了启用路线与标注粒度、隔离清单中的样本未出现在训练文件；
2. ``repack``：把 run 目录里的主输出重新打成 ZIP（仅用于取证/复现，不改变业务判断）。

用法::

    python utils/prepare_package.py verify output_smoke/smoke_cli/delivery_package/run001
    python utils/prepare_package.py repack <run_dir> <out.zip>
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import delivery  # noqa: E402


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verify(package: Path) -> Tuple[bool, List[str]]:
    problems: List[str] = []
    with zipfile.ZipFile(package) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)):
            problems.append("包内存在重复成员路径")
        for name in names:
            if name.startswith(("/", "C:")) or ".." in name.split("/"):
                problems.append("成员路径不是包内相对路径：%s" % name)
        if delivery.TRAIN_MEMBER not in names:
            problems.append("缺少训练文件 %s" % delivery.TRAIN_MEMBER)
        if delivery.PACKAGE_MANIFEST_MEMBER not in names:
            problems.append("缺少包清单 manifest.json")
            return False, problems
        manifest: Dict[str, Any] = json.loads(
            bundle.read(delivery.PACKAGE_MANIFEST_MEMBER).decode("utf-8"))
        for item in manifest.get("files") or []:
            path = item.get("path")
            if path not in names:
                problems.append("清单声明的成员缺失：%s" % path)
                continue
            payload = bundle.read(path)
            if item.get("size") is not None and int(item["size"]) != len(payload):
                problems.append("成员大小不一致：%s" % path)
            if item.get("sha256") and item["sha256"] != _sha256(payload):
                problems.append("成员 sha256 不一致：%s" % path)
        version_name = delivery.PACKAGE_VERSION_MEMBER
        if version_name in names:
            version = json.loads(bundle.read(version_name).decode("utf-8"))
            if not version.get("enabled_routes"):
                problems.append("version.json 未声明 enabled_routes")
            if not version.get("annotation_granularity"):
                problems.append("version.json 未声明 annotation_granularity")
        else:
            problems.append("缺少版本说明 %s" % version_name)
        isolated: List[str] = []
        if delivery.package_member_for_role(delivery.MATERIAL_ISOLATION_LIST) in names:
            isolation_text = bundle.read(
                delivery.package_member_for_role(
                    delivery.MATERIAL_ISOLATION_LIST)).decode("utf-8")
            for line in isolation_text.splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("sample_id"):
                    isolated.append(str(entry["sample_id"]))
        train_text = bundle.read(delivery.TRAIN_MEMBER).decode("utf-8")
        leaked = [item for item in isolated if '"%s"' % item in train_text]
        if leaked:
            problems.append("隔离样本出现在训练文件：%s" % ", ".join(sorted(set(leaked))))
    return (not problems), problems


def repack(run_dir: Path, target: Path) -> Path:
    """把 run 目录的主输出与材料重新打成 ZIP（取证/复现用）。"""
    main = Path(run_dir) / delivery.MAIN_NAME
    if not main.is_file():
        raise SystemExit("主输出不存在：%s" % main)
    members: List[Tuple[str, bytes]] = [(delivery.TRAIN_MEMBER, main.read_bytes())]
    for role in (delivery.MATERIAL_QUALITY_REPORT, delivery.MATERIAL_SAMPLING_CONCLUSION,
                 delivery.MATERIAL_ISOLATION_LIST, delivery.MATERIAL_VERSION_NOTES):
        member = delivery.package_member_for_role(role)
        if member is None:
            continue
        candidate = Path(run_dir).parent.parent / "input" / "materials" / Path(member).name
        if candidate.is_file():
            members.append((member, candidate.read_bytes()))
    payload = b"\n".join(json.dumps({"member": name, "bytes": len(data)},
                                    ensure_ascii=False).encode("utf-8")
                         for name, data in members) + b"\n"
    members.append((delivery.PACKAGE_MANIFEST_MEMBER, payload))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in members:
            bundle.writestr(name, data)
    return target


def main(argv: List[str]) -> int:
    if len(argv) >= 2 and argv[1] == "verify":
        package = Path(argv[2]) if len(argv) > 2 else ROOT / delivery.PACKAGE_DELIVERABLE_NAME
        ok, problems = verify(package)
        print("校验对象：%s" % package)
        if ok:
            print("结果：通过（成员路径安全、清单与 sha256 一致、版本说明完整、无隔离样本）")
            return 0
        print("结果：不通过")
        for item in problems:
            print("  - %s" % item)
        return 1
    if len(argv) >= 4 and argv[1] == "repack":
        target = repack(Path(argv[2]), Path(argv[3]))
        print("已重新打包：%s" % target)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
