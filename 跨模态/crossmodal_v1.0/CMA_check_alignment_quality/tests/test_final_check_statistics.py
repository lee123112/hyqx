"""Published check totals must describe finalized results, including quarantines.

The deterministic encoder is a test-only dependency: these tests exercise
finalization and reporting, not model accuracy. Delivery replay uses real CLIP.
"""
from collections import Counter
import json
from pathlib import Path

import pytest

import inference
from tests.conftest import StubEncoder, write_jsonl, write_png
from utils import alignment_qc as qc
from utils import reverse_verify as rv


@pytest.mark.parametrize('case,bucket', [
    ('consistent', 'ok'),
    ('inconsistent', 'failed'),
    ('single_candidate', 'insufficient_evidence'),
    ('model_unavailable', 'insufficient_evidence'),
    ('upstream_reused', 'ok'),
    ('route_disabled', 'not_applicable'),
])
def test_published_statistics_match_final_checks_with_quarantines(
        tmp_path, monkeypatch, case, bucket):
    root = tmp_path / 'input'
    write_png(root / 'media/red.png', rgb=(220, 20, 20))
    write_png(root / 'media/green.png', rgb=(20, 220, 20))
    pairs = [
        {'sample_id': 'red', 'text': 'red shape', 'image': 'media/red.png'},
        {'sample_id': 'green', 'text': 'green shape', 'image': 'media/green.png'},
    ]
    if case == 'single_candidate':
        pairs = pairs[:1]
    if case == 'upstream_reused':
        for row in pairs:
            row['qc'] = {'reverse_verify': {'passed': True, 'score': .8,
                                            'check_status': 'checked'}}
    rows = pairs + [
        {'sample_id': 'missing', 'text': 'missing image', 'image': 'media/missing.png'},
        {'sample_id': 'text_only', 'text': 'missing second modality'},
    ]
    source = write_jsonl(root / 'samples.jsonl', rows)
    vectors = {'text:red shape': [1, 0], 'text:green shape': [0, 1],
               'image:red.png': [1, 0], 'image:green.png': [0, 1]}
    if case == 'inconsistent':
        vectors['image:red.png'], vectors['image:green.png'] = [0, 1], [1, 0]
    encoder = StubEncoder(dim=2, vectors=vectors)
    monkeypatch.setattr(rv, 'build_encoder', lambda config:
                        (None, 'test-only-unavailable') if case == 'model_unavailable'
                        else (encoder, None))
    result = inference.run(str(source), 'final_stats_' + case,
        output_root=str(tmp_path / 'out'), allow_external_input=True,
        overrides={'enabled': True, 'allow_degraded': True,
                   'quality_subset_fallback': 0.0,
                   'reverse_verify': {'enabled': case != 'route_disabled'}})
    run_dir = Path(result['runDir'])
    checks = [json.loads(line) for line in
              (run_dir / qc.CHECKS_NAME).read_text(encoding='utf-8').splitlines()]
    assert len(checks) == len(rows)
    assert result['statistics']['origin_quarantined'] == 1
    assert result['statistics']['sample_quarantined'] == 1
    pair_checks = [r['checks']['reverse_verify'] for r in checks
                   if r['sample_id'] in {p['sample_id'] for p in pairs}]
    assert Counter(r['status'] for r in pair_checks) == {bucket: len(pairs)}

    states = ('ok', 'failed', 'not_applicable', 'insufficient_evidence', 'pending_pool')
    expected = {name: {state: sum(r.get('checks', {}).get(name, {}).get('status') == state
                                  for r in checks) for state in states}
                for name in qc.CHECK_ORDER}
    assert expected['reverse_verify']['pending_pool'] == 0
    assert result['statistics']['checks'] == expected
    report = json.loads((run_dir / qc.REPORT_NAME).read_text(encoding='utf-8'))
    assert report['checks'] == expected
    assert report['statistics']['checks'] == expected
    # The rejected records must remain represented in non-reverse check totals.
    assert expected['file_openable']['failed'] == 1
    assert expected['min_modalities']['failed'] == 2
