import wave
import pytest
import inference as op
from utils.schemas import OperatorConfig, BusinessOverrides
from utils import audio_processing as ap

def config(**kw):
    return OperatorConfig(operator={'name':'audio_standardization','version':'v1.1.0'},stage_id='S2',**kw)

def test_default_disabled():
    cfg=op.load_config()
    assert cfg.enabled is False

@pytest.mark.parametrize('kw',[{'sample_rate':8000},{'channels':2},{'clip_start':0,'clip_end':1}])
def test_forbidden_parameters(kw):
    with pytest.raises(ValueError):config(**kw)

def test_silent_audio_rejected(tmp_path):
    src=tmp_path/'silent.wav'
    with wave.open(str(src),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\x00\x00'*16000)
    with pytest.raises(ap.AudioProcessingError) as e:
        ap.normalize_audio(src,tmp_path/'out.wav',config())
    assert e.value.reason=='silent_audio'

@pytest.mark.parametrize('kw',[{'ffmpeg_path':'other'}, {'max_duration_seconds':1}, {'process_timeout_seconds':1}])
def test_request_cannot_override_deployment_parameters(kw):
    with pytest.raises(ValueError): BusinessOverrides.model_validate(kw)

def test_ffmpeg_reports_source_parameters(tmp_path):
    src=tmp_path/'source.wav'
    with wave.open(str(src),'wb') as w:
        w.setnchannels(2);w.setsampwidth(2);w.setframerate(22050)
        w.writeframes(b'\x20\x01\x20\x01'*22050)
    result=ap.normalize_audio(src,tmp_path/'out.wav',config())
    assert result['source_sample_rate']==22050
    assert result['source_channels']==2
