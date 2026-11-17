"""Upload packaged examples, execute HTTP calls, and optionally save/clean output ZIPs."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import time
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def request(base_url, route, *, key, timeout, payload=None, upload=None, binary=False):
    headers = {'X-API-Key': key} if key else {}
    content = None
    if payload is not None:
        content = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if upload is not None:
        boundary = 'SME-' + uuid.uuid4().hex
        content = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                   f'filename="input.zip"\r\nContent-Type: application/zip\r\n\r\n').encode()
        content += upload + f'\r\n--{boundary}--\r\n'.encode()
        headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
    try:
        with urlopen(Request(base_url + route, data=content, headers=headers), timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code}: {exc.read(4096).decode("utf-8", errors="replace")}') from exc
    if binary:
        return body
    envelope = json.loads(body)
    if envelope.get('code') != 0:
        raise RuntimeError(json.dumps(envelope, ensure_ascii=False))
    return envelope['data']


def verify_output(bundle, execution):
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        broken = archive.testzip()
        if broken is not None:
            raise RuntimeError('Output ZIP CRC mismatch: ' + broken)
        manifest = json.loads(archive.read('manifest.json'))
        result = json.loads(archive.read('result.json'))
        for item in manifest['files']:
            if hashlib.sha256(archive.read(item['name'])).hexdigest() != item['sha256']:
                raise RuntimeError('Output member SHA-256 mismatch: ' + item['name'])
        for key in ('rowsIn', 'rowsOut'):
            if execution[key] != manifest[key] or execution[key] != result[key]:
                raise RuntimeError('Inconsistent output counter: ' + key)
        return {'crc': 'passed', 'memberHashes': 'passed', 'files': len(archive.namelist())}


def run_case(case, args, output_dir):
    started = time.monotonic()
    uid = 'HANDOFF-' + case['id'] + '-' + uuid.uuid4().hex[:8]
    folder = output_dir / uid
    folder.mkdir(parents=True)
    report = {'caseId': case['id'], 'uniqueId': uid, 'method': case['method'],
              'type': case['type'], 'status': 'failed', 'outputDirectory': str(folder)}
    print('START ' + case['id'], flush=True)
    try:
        path = ROOT / 'examples/input_zips' / case['file']
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != case['sha256']:
            raise RuntimeError('Input example SHA-256 mismatch: ' + case['file'])
        def call(route, **kwargs):
            return request(args.url, route, key=args.api_key, timeout=args.timeout, **kwargs)
        upload = call('/uploadFile?' + urlencode({'uniqueId': uid}), upload=content)
        params = {'mode': 'measured', 'registerTask': True}
        if case['protocol']:
            params['protocol'] = True
        else:
            params['measurementSource'] = 'engine' if case['method'] == '引擎测量' else 'import'
        execution = call('/predict', payload={'uniqueId': uid, 'operatorId': case['operatorId'],
                         'inputFile': upload['inputFile'], 'params': params, 'isLastOperator': False})
        write_json(folder / 'response.json', execution)
        if execution.get('status') != 'succeeded':
            raise RuntimeError('Execution status is not succeeded')
        if case['protocol']:
            expected = {'text': 4, 'image': 20, 'audio': 2}[case['type']]
            if execution['rowsIn'] != expected or execution['rowsOut'] != expected:
                raise RuntimeError('Training counters must equal fixed E: ' + str(expected))
        if args.download or args.cleanup:
            bundle = call('/downloadFile?' + urlencode({'uniqueId': uid, 'path': execution['outputPath']}), binary=True)
            report['outputVerification'] = verify_output(bundle, execution)
            (folder / 'output.zip').write_bytes(bundle)
        if args.cleanup:
            call('/removeFile', payload={'uniqueId': uid})
            report['workspaceRemoved'] = True
            verify_output((folder / 'output.zip').read_bytes(), execution)
        report.update(status='passed', taskId=execution['taskId'], rowsIn=execution['rowsIn'], rowsOut=execution['rowsOut'])
        print('PASS ' + case['id'] + f" rows={execution['rowsIn']}/{execution['rowsOut']}", flush=True)
    except Exception as exc:
        report['error'] = str(exc)
        print('FAIL ' + case['id'] + ': ' + str(exc), flush=True)
    report['durationSeconds'] = round(time.monotonic() - started, 3)
    write_json(folder / 'case-summary.json', report)
    return report


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    cases = json.loads((ROOT / 'examples/index.json').read_text(encoding='utf-8'))['cases']
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--case', choices=[case['id'] for case in cases])
    group.add_argument('--all', action='store_true', help='Execute all twelve examples, including tiny training runs')
    group.add_argument('--list', action='store_true')
    parser.add_argument('--url', default='http://127.0.0.1:17024')
    parser.add_argument('--timeout', type=float, default=1800)
    parser.add_argument('--api-key', default=os.environ.get('SME_API_KEY', ''))
    parser.add_argument('--download', action='store_true', help='Save output ZIP and verify CRC/member hashes')
    parser.add_argument('--cleanup', action='store_true', help='Remove server workspace after saving and verifying output ZIP')
    parser.add_argument('--output', type=Path,
                        default=Path(os.environ.get('SME_DATA_DIR', str(ROOT / 'runtime'))) / 'manual_tests')
    args = parser.parse_args()
    if args.list:
        print(json.dumps(cases, ensure_ascii=False, indent=2))
        return 0
    args.url = args.url.rstrip('/')
    if urlsplit(args.url).scheme not in ('http', 'https') or args.timeout <= 0:
        parser.error('Use an http(s) URL and positive timeout')
    run_dir = args.output.resolve() / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    run_dir.mkdir(parents=True)
    health = request(args.url, '/health', key=args.api_key, timeout=min(args.timeout, 30))
    if health.get('operatorCount') != 5:
        raise RuntimeError('Expected five registered operators')
    selected = cases if args.all else [case for case in cases if case['id'] == args.case]
    results = []
    for case in selected:
        results.append(run_case(case, args, run_dir))
        summary = {'serviceUrl': args.url, 'total': len(results),
                   'passed': sum(row['status'] == 'passed' for row in results),
                   'failed': sum(row['status'] != 'passed' for row in results), 'results': results}
        write_json(run_dir / 'summary.json', summary)
    print(json.dumps({'status': 'failed' if summary['failed'] else 'passed', 'total': summary['total'],
                      'passed': summary['passed'], 'failed': summary['failed'],
                      'summaryFile': str(run_dir / 'summary.json')}, ensure_ascii=False), flush=True)
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
