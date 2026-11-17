"""Check dependency versions and native imports before running model examples."""
import importlib
from importlib import metadata
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():
    from packaging.requirements import Requirement
    failures, versions = [], {}
    if sys.version_info[:2] != (3, 11):
        failures.append({'python': sys.version, 'reason': 'delivery_requires_python_3_11'})
    for line in (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        requirement = Requirement(line)
        try:
            version = metadata.version(requirement.name)
            versions[requirement.name] = version
            if not requirement.specifier.contains(version, prereleases=True):
                failures.append({'package': requirement.name, 'version': version, 'required': str(requirement.specifier)})
        except metadata.PackageNotFoundError:
            failures.append({'package': requirement.name, 'reason': 'not_installed'})
    imports = {}
    for name in ('numpy', 'scipy', 'sklearn', 'cv2', 'torch', 'torchvision', 'fastapi', 'transformers'):
        try:
            module = importlib.import_module(name)
            imports[name] = getattr(module, '__version__', 'imported')
        except Exception as exc:
            failures.append({'module': name, 'reason': str(exc)})
    try:
        from transformers import AutoModelForMaskedLM, WhisperForConditionalGeneration, WhisperProcessor
        imports['transformersModelClasses'] = 'imported'
    except Exception as exc:
        failures.append({'module': 'transformers model classes', 'reason': str(exc)})
    report = {'status': 'failed' if failures else 'passed', 'python': sys.version,
              'platform': sys.platform, 'versions': versions, 'imports': imports, 'failures': failures}
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
