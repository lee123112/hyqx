"""Check delivered file sizes and SHA-256 hashes using only Python's standard library."""
import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = json.loads((root / '交付文件清单.json').read_text(encoding='utf-8-sig'))
    failures = []
    checked = total = 0
    seen = set()
    for item in manifest['files']:
        relative = Path(item['path'])
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root) or item['path'] in seen:
            failures.append({'path': item['path'], 'reason': 'invalid_or_duplicate_path'})
            continue
        seen.add(item['path'])
        if not path.is_file() or path.is_symlink():
            failures.append({'path': item['path'], 'reason': 'missing_or_symlink'})
            continue
        size = path.stat().st_size
        if size != item['bytes'] or digest(path) != item['sha256']:
            failures.append({'path': item['path'], 'reason': 'size_or_sha256_mismatch'})
        checked += 1
        total += size
    print(json.dumps({'status': 'failed' if failures else 'passed', 'files': checked,
                      'bytes': total, 'failures': failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
