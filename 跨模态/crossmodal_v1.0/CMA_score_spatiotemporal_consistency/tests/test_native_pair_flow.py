"""Native M11 pair identities and timelines through the official multipart route."""
import copy
import json
import zipfile

import pytest

import app as api
from tests import fixtures
from utils import temporal


def pair_with(record):
    return {'pair_id': 'pair-native', 'modality_pair': ['text', 'video', 'audio'],
            'left': {'sample_id': 'caption', 'record': {'sample_id': 'caption',
                     'text': {'content': 'A dog runs'}}},
            'right': {'sample_id': record.get('sample_id'), 'record': record}}


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('sidecar', [False, True])
def test_pair_id_native_input_survives_official_zip(client, monkeypatch, enabled, sidecar):
    original = pair_with(fixtures.make_record('movie'))
    media = {}
    if sidecar:
        nested = original['right']['record']
        payload = dict(nested.pop('timeline'), temporal_bindings=nested.pop('temporal_bindings'),
                       temporal_anchors=nested.pop('temporal_anchors'))
        nested['timeline_file'] = 'timeline.json'
        media['timeline.json'] = json.dumps(payload).encode()
    config = api.inference.load_config(overrides={'enabled': enabled})
    monkeypatch.setattr(api.inference, 'load_config', lambda *args, **kwargs: config)
    response = client.post('/predict?detection=M15&uniqueId=native_pair',
        files={'file': ('input.zip', fixtures.bundle_zip([original], media=media), 'application/zip')})
    body = response.json()
    assert body['status'] == 1, body
    with zipfile.ZipFile(body['outputFile']['zip']) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        rows = [json.loads(line) for line in archive.read(manifest['entrypoint']).splitlines()]
        if sidecar:
            assert json.loads(archive.read('timeline.json')) == payload
    assert len(rows) == 1
    assert rows[0]['sample_id'] == original['pair_id']
    assert rows[0]['left'] == original['left'] and rows[0]['right'] == original['right']
    if enabled:
        assert rows[0]['temporal_alignment']['passed'] is True
    else:
        assert rows[0]['processing']['15']['process_status'] == 'skipped'


def test_nested_sidecar_timelines_resolve_and_keep_real_failure(tmp_path):
    payload = fixtures.make_record('movie', video=(0,), audio=(2,), text=(0,),
                                   anchor_offset=None)
    sidecar = dict(payload['timeline'], temporal_bindings=payload['temporal_bindings'])
    (tmp_path / 'timeline.json').write_text(json.dumps(sidecar), encoding='utf-8')
    source = pair_with({'sample_id': 'movie', 'timeline_file': 'timeline.json'})
    spec = temporal.resolve_spec(source, [tmp_path])
    assert spec['media_refs'] == ['timeline.json']
    outcome = temporal.evaluate_record(spec, line=1,
        config=api.inference.load_config(overrides={'enabled': True}), event_time='2026-11-11')
    assert outcome['action'] == 'isolate'
    assert source['right']['record']['timeline_file'] == 'timeline.json'


def test_conflicting_nested_timeline_definitions_reject_instead_of_arbitrary_choice(tmp_path):
    pair = pair_with(fixtures.make_record('movie'))
    pair['left']['record']['timeline'] = {'video': [{'id': 'different', 'timestamp_sec': 8}]}
    with pytest.raises(temporal.TimelineError, match='conflict|冲突'):
        temporal.resolve_spec(pair, [tmp_path])


def test_identical_derived_view_timelines_do_not_conflict(tmp_path):
    source = fixtures.make_record('original')
    pair = pair_with(source)
    pair['left']['record'].update(copy.deepcopy(source))
    spec = temporal.resolve_spec(pair, [tmp_path])
    assert all(spec['timelines'].values())
    assert len(spec['bindings']) == len(source['temporal_bindings'])


def test_split_native_pair_timelines_preserve_explicit_bindings(tmp_path):
    source = fixtures.make_record('original')
    pair = pair_with({'sample_id': 'movie', 'timeline': {
        'video': source['timeline']['video'], 'audio': source['timeline']['audio']}})
    pair['left']['record'].update({'timeline': {'text': source['timeline']['text']},
                                 'temporal_bindings': source['temporal_bindings']})
    spec = temporal.resolve_spec(pair, [tmp_path])
    assert all(spec['timelines'].values())
    assert spec['bindings'] == source['temporal_bindings']
