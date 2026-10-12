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
