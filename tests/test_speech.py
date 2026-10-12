import xml.etree.ElementTree as ET

import httpx
import pytest

from autoclip.config import Settings
from autoclip.speech import azure_synthesize


def test_azure_never_reuses_model_credentials(tmp_path,monkeypatch):
    monkeypatch.delenv('AZURE_SPEECH_KEY',raising=False)
    monkeypatch.setenv('OPENCODE_API_KEY','go-only')
    monkeypatch.setenv('DEEPSEEK_API_KEY','deepseek-only')
    monkeypatch.setattr('autoclip.speech.httpx.post',lambda *a,**k:pytest.fail('不得发送模型密钥'))
    with pytest.raises(ValueError,match='不能使用模型密钥'):
        azure_synthesize('文本',tmp_path/'audio.mp3',Settings(tts='azure',azure_speech_region='eastasia'))


def test_azure_region_cannot_redirect_credentials_even_after_copy(tmp_path,monkeypatch):
    monkeypatch.setenv('AZURE_SPEECH_KEY','speech-only')
    cfg=Settings(tts='azure',azure_speech_region='eastasia').model_copy(update={'azure_speech_region':'evil.test/x'})
    monkeypatch.setattr('autoclip.speech.httpx.post',lambda *a,**k:pytest.fail('不能访问任意凭证主机'))
    with pytest.raises(ValueError,match='区域格式'):
        azure_synthesize('文本',tmp_path/'audio.mp3',cfg)


def test_azure_voice_and_text_are_encoded_as_ssml(tmp_path,monkeypatch):
    monkeypatch.setenv('AZURE_SPEECH_KEY','speech-only')
    sent=[]
    def request(url,**kwargs):
        sent.append((url,kwargs))
        assert url=='https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1'
        assert kwargs['headers']['Ocp-Apim-Subscription-Key']=='speech-only'
        assert kwargs['follow_redirects'] is False
        ssml=ET.fromstring(kwargs['content'])
        voice=next(e for e in ssml.iter() if e.tag.endswith('voice'))
        assert voice.attrib['name']=='zh-CN-YunyangNeural'
        assert ''.join(ssml.itertext())=='银子 & <名单>'
        return httpx.Response(200,content=b'audio')
    monkeypatch.setattr('autoclip.speech.httpx.post',request)
    path=tmp_path/'audio.mp3'
    azure_synthesize('银子 & <名单>',path,Settings(tts='azure',azure_speech_region='eastasia'))
    assert path.read_bytes()==b'audio' and len(sent)==1


def test_azure_auth_error_is_sanitized_and_never_falls_back(tmp_path,monkeypatch):
    monkeypatch.setenv('AZURE_SPEECH_KEY','speech-only')
    calls=[]
    def request(url,**kwargs):
        calls.append(url)
        return httpx.Response(403,json={'message':'do-not-log-server-body'})
    monkeypatch.setattr('autoclip.speech.httpx.post',request)
    with pytest.raises(ValueError) as error:
        azure_synthesize('文本',tmp_path/'audio.mp3',Settings(tts='azure',azure_speech_region='eastasia'))
    assert 'do-not-log' not in str(error.value) and len(calls)==1
    assert not (tmp_path/'audio.mp3').exists()


def test_macos_voice_is_used_and_changes_audio_cache(tmp_path,monkeypatch):
    import autoclip.render as render
    commands=[]
    def run(args):
        commands.append(args)
        if args[0]=='say':open(args[-1],'wb').write(b'aiff')
        else:open(args[-1],'wb').write(b'audio'*40)
    monkeypatch.setattr(render,'run',run)
    monkeypatch.setattr(render,'duration',lambda *a:10)
    a=render.synthesize('口播',tmp_path,Settings(tts='macos',macos_voice='Eddy',macos_rate=300))
    b=render.synthesize('口播',tmp_path,Settings(tts='macos',macos_voice='Tingting',macos_rate=300))
    assert a!=b
    assert commands[0][:5]==['say','-v','Eddy','-r',300]


@pytest.mark.parametrize('endpoint,fragment',[('msedge','api.msedgeservices.com/tts/cognitiveservices/websocket/v1?Ocp-Apim-Subscription-Key='),('bing','speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1?TrustedClientToken=')])
def test_edge_endpoint_is_scoped_and_never_uses_user_keys(tmp_path,monkeypatch,endpoint,fragment):
    import edge_tts
    import edge_tts.communicate as module
    from autoclip.speech import edge_synthesize
    monkeypatch.setenv('AZURE_SPEECH_KEY','private-speech-key')
    monkeypatch.setenv('OPENCODE_API_KEY','private-go-key')
    previous=module.WSS_URL
    seen=[]
    class Fake:
        def __init__(self,*args,**kwargs):
            seen.append(module.WSS_URL)
        async def save(self,audio,boundaries):
            open(audio,'wb').write(b'audio')
    monkeypatch.setattr(edge_tts,'Communicate',Fake)
    edge_synthesize('口播',tmp_path/'audio.mp3',tmp_path/'boundaries.jsonl',Settings(edge_endpoint=endpoint))
    assert len(seen)==1 and fragment in seen[0]
    assert 'private-' not in seen[0] and module.WSS_URL==previous


def test_edge_failed_requests_restore_endpoint_and_clear_partial_files(tmp_path,monkeypatch):
    import edge_tts
    import edge_tts.communicate as module
    import asyncio
    from autoclip.speech import edge_synthesize
    previous=module.WSS_URL
    audio=tmp_path/'audio.mp3'; boundaries=tmp_path/'boundaries.jsonl'
    calls=[]
    class Fake:
        def __init__(self,*a,**k):calls.append(module.WSS_URL)
        async def save(self,*args):
            audio.write_bytes(b'partial');boundaries.write_text('partial')
            raise ValueError('private-server-response')
    async def no_sleep(*args):pass
    monkeypatch.setattr(edge_tts,'Communicate',Fake)
    monkeypatch.setattr(asyncio,'sleep',no_sleep)
    with pytest.raises(RuntimeError) as error:
        edge_synthesize('口播',audio,boundaries,Settings())
    assert len(calls)==3 and module.WSS_URL==previous
    assert 'private-server-response' not in str(error.value)
    assert not audio.exists() and not boundaries.exists()
