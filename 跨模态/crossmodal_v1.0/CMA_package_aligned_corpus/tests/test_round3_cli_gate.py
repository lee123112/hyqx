import inference


def test_pending_release_cli_returns_nonzero(monkeypatch,tmp_path):
    monkeypatch.setattr(inference,'run',lambda **kwargs:{'runDir':str(tmp_path),'outputFile':'pending.zip',
        'stageLineage':[{'status':'partial','statistics':{'deliverable':False}}],
        'status':'partial','message':'版本保持待发布','statistics':{'deliverable':False}})
    assert inference.main(['--input',str(tmp_path/'samples.jsonl'),'--unique-id','gate-test']) != 0
