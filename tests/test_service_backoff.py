import asyncio

import httpx
import pytest

from autoclip.config import Settings
from autoclip.llm import LLM


@pytest.mark.parametrize('status,header,delay',[(429,None,15),(503,None,5),(429,'22',22),(503,'999',60)])
def test_transient_model_error_waits_and_reuses_success_without_changing_provider(tmp_path,monkeypatch,status,header,delay):
    monkeypatch.setattr(Settings,'api_key',lambda _:'test')
    waits=[];monkeypatch.setattr('autoclip.llm.time.sleep',waits.append)
    llm=LLM(Settings(max_api_calls=2),tmp_path);llm.client.close()
    requests=[]
    def respond(request):
        requests.append(request.url)
        if len(requests)==1:return httpx.Response(status,headers={'Retry-After':header} if header else {})
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]})
    llm.client=httpx.Client(transport=httpx.MockTransport(respond))
    try:
        assert llm.json('system','prompt')=={'ok':True}
        assert llm.json('system','prompt')=={'ok':True}
        assert waits==[delay]
        assert len(requests)==llm.calls==2 and requests[0]==requests[1]
        assert len(list((tmp_path/'llm').glob('*.json')))==1
    finally:llm.client.close()


def test_edge_rate_limit_waits_then_recovers_same_voice_and_endpoint(tmp_path,monkeypatch):
    import edge_tts
    import edge_tts.communicate as module
    from autoclip.speech import edge_synthesize
    calls=[];waits=[];previous=module.WSS_URL
    class Limited(Exception):status=429
    class Fake:
        def __init__(self,text,voice,rate):calls.append((module.WSS_URL,text,voice,rate))
        async def save(self,audio,boundaries):
            if len(calls)==1:raise Limited()
            from pathlib import Path
            Path(audio).write_bytes(b'complete-audio');Path(boundaries).write_text('')
    async def wait(delay):waits.append(delay)
    monkeypatch.setattr(edge_tts,'Communicate',Fake);monkeypatch.setattr(asyncio,'sleep',wait)
    target=tmp_path/'audio.mp3';edge_synthesize('现有稿件',target,tmp_path/'boundaries.jsonl',Settings())
    assert waits==[15] and len(calls)==2 and calls[0]==calls[1]
    assert target.read_bytes()==b'complete-audio' and module.WSS_URL==previous
