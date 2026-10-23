import json

import httpx
import pytest

from autoclip.config import Settings
from autoclip.llm import LLM
from autoclip.util import read_json


def go():
    return Settings(llm_base_url='https://opencode.ai/zen/go/v1', llm_model='deepseek-v4.1-flash')


def test_go_uses_its_own_key_and_a_stable_honest_client_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCODE_API_KEY', 'go-test-key')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'official-test-key')
    a = LLM(go(), tmp_path / 'episode-a')
    b = LLM(go(), tmp_path / 'episode-a')
    c = LLM(go(), tmp_path / 'episode-b')
    try:
        assert a.client.headers['authorization'] == 'Bearer go-test-key'
        assert a.client.headers['user-agent'] == 'autoclip-director/1.1'
        assert a.client.headers['x-opencode-session'] == b.client.headers['x-opencode-session']
        assert a.client.headers['x-opencode-session'] != c.client.headers['x-opencode-session']
        assert str(tmp_path) not in a.client.headers['x-opencode-session']
    finally:
        for m in (a, b, c): m.client.close()


def test_go_limit_error_never_sends_request_to_paid_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCODE_API_KEY', 'go-test-key')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'official-test-key')
    monkeypatch.setattr('autoclip.llm.time.sleep', lambda *a: None)
    requests = []
    client = httpx.Client
    def respond(request):
        requests.append(request)
        return httpx.Response(429, json={'error': 'remote-message-that-must-not-be-logged'})
    monkeypatch.setattr('autoclip.llm.httpx.Client', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    m = LLM(go(), tmp_path)
    try:
        with pytest.raises(RuntimeError) as error:
            m.json('system', 'prompt')
        assert 'remote-message-that-must-not-be-logged' not in str(error.value)
        assert len(requests) == 3
        assert all(str(r.url) == 'https://opencode.ai/zen/go/v1/chat/completions' for r in requests)
        assert all(r.headers['authorization'] == 'Bearer go-test-key' for r in requests)
        assert not list(tmp_path.rglob('*.json'))
    finally: m.client.close()


def test_go_cache_records_endpoint_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCODE_API_KEY', 'go-test-key')
    client = httpx.Client
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'model': 'deepseek-v4.1-flash', 'choices': [
            {'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]})
    monkeypatch.setattr('autoclip.llm.httpx.Client', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    m = LLM(go(), tmp_path)
    try:
        assert m.json('system', 'prompt') == {'ok': True}
        assert m.json('system', 'prompt') == {'ok': True}
        assert len(requests) == 1
        cache = next((tmp_path / 'llm').glob('*.json'))
        assert read_json(cache)['endpoint'] == go().llm_base_url
        assert read_json(cache)['requested_model'] == go().llm_model
        assert 'go-test-key' not in cache.read_text()
        assert m.usage()['endpoint'] == go().llm_base_url
    finally: m.client.close()


@pytest.mark.parametrize('endpoint,model,effort',[
    ('https://opencode.ai/zen/go/v1','deepseek-v4.1-flash','none'),
    ('https://opencode.ai/zen/go/v1','deepseek-flash','none'),
    ('https://api.deepseek.com','deepseek-flash',None),
    ('https://opencode.ai/zen/go/v1','glm-5.3',None),
])
def test_reasoning_switch_is_scoped_to_go_deepseek(tmp_path,monkeypatch,endpoint,model,effort):
    monkeypatch.setenv('OPENCODE_API_KEY','go-test-key')
    monkeypatch.setenv('DEEPSEEK_API_KEY','direct-test-key')
    client=httpx.Client
    def respond(request):
        body=json.loads(request.content)
        assert body.get('reasoning_effort')==effort
        assert body['thinking']=={'type':'disabled'}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]})
    monkeypatch.setattr('autoclip.llm.httpx.Client',lambda **kwargs:client(transport=httpx.MockTransport(respond),**kwargs))
    m=LLM(Settings(llm_base_url=endpoint,llm_model=model),tmp_path)
    try:assert m.json('system','prompt')=={'ok':True}
    finally:m.client.close()


def test_disputed_review_uses_same_model_and_budget_with_distinct_reasoning_cache(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENCODE_API_KEY','go-test-key')
    client=httpx.Client;requests=[]
    def respond(request):
        body=json.loads(request.content);requests.append((str(request.url),body))
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':
            {'content':'{"ok":true}','reasoning_content':'internal reasoning must not be stored'}}]})
    monkeypatch.setattr('autoclip.llm.httpx.Client',lambda **kwargs:client(transport=httpx.MockTransport(respond),**kwargs))
    m=LLM(go().model_copy(update={'max_api_calls':2}),tmp_path)
    try:
        m.json('reviewer','same source')
        m.review_json('reviewer','same source')
        m.review_json('reviewer','same source')
        assert len(requests)==m.calls==2
        assert requests[0][1]['reasoning_effort']=='none'
        assert requests[1][1]['reasoning_effort']=='low'
        assert requests[1][1]['thinking']=={'type':'enabled'} and 'temperature' not in requests[1][1]
        assert requests[0][0]==requests[1][0] and requests[0][1]['model']==requests[1][1]['model']
        with pytest.raises(RuntimeError,match='次数上限'):m.review_json('reviewer','new source')
        caches=list((tmp_path/'llm').glob('*.json'))
        assert len(caches)==2 and all('internal reasoning' not in p.read_text() for p in caches)
    finally:m.client.close()


@pytest.mark.parametrize('always_truncated', [False, True])
def test_truncated_response_uses_bounded_larger_budget_without_caching_partial_json(tmp_path, monkeypatch, always_truncated):
    monkeypatch.setenv('OPENCODE_API_KEY', 'go-test-key')
    monkeypatch.setattr('autoclip.llm.time.sleep', lambda *a: None)
    budgets = []
    client = httpx.Client
    def respond(request):
        budgets.append(json.loads(request.content)['max_tokens'])
        truncated = always_truncated or len(budgets) == 1
        return httpx.Response(200, json={'choices': [{'finish_reason': 'length' if truncated else 'stop',
            'message': {'content': '{"partial":true}' if truncated else '{"ok":true}'}}]})
    monkeypatch.setattr('autoclip.llm.httpx.Client', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    m = LLM(go(), tmp_path)
    try:
        if always_truncated:
            with pytest.raises(RuntimeError, match='截断'):
                m.json('system', 'prompt')
            assert budgets == [14000, 28000, 28000]
            assert not list(tmp_path.rglob('*.json'))
        else:
            assert m.json('system', 'prompt') == {'ok': True}
            assert m.json('system', 'prompt') == {'ok': True}
            assert budgets == [14000, 28000]
            assert len(list(tmp_path.rglob('*.json'))) == 1
        assert m.calls == len(budgets)
    finally: m.client.close()
