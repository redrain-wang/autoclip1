from pathlib import Path

import httpx
import pytest

from autoclip.config import Settings
from autoclip.llm import LLM
from autoclip.util import digest


@pytest.mark.parametrize('mode,expected',[('fixed',100),('steps',105)])
def test_more_than_one_hundred_requests_with_bounded_steps_and_explicit_fixed_budget(tmp_path,monkeypatch,mode,expected):
    monkeypatch.setattr(Settings,'api_key',lambda _:'test')
    model=LLM(Settings(api_budget_mode=mode,max_api_calls=100),tmp_path)
    model.client.close()
    requests=[]
    def respond(request):
        requests.append(1)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]})
    model.client=httpx.Client(transport=httpx.MockTransport(respond))
    try:
        for i in range(expected):assert model.json('writer',f'unit {i}')=={'ok':True}
        assert model.json('writer','unit 0')=={'ok':True}
        assert len(requests)==model.calls==expected
        if mode=='fixed':
            with pytest.raises(RuntimeError,match='次数上限'):model.json('writer','one more')
            assert len(requests)==100
        else:
            assert model.usage()['request_policy']['global_request_limit'] is None
            assert model.calls==105
    finally:model.client.close()


@pytest.mark.parametrize('failure',['http','json','transport'])
def test_steps_policy_still_stops_a_failing_request_after_three_attempts(tmp_path,monkeypatch,failure):
    monkeypatch.setattr(Settings,'api_key',lambda _:'test')
    waits=[];monkeypatch.setattr('autoclip.llm.time.sleep',waits.append)
    model=LLM(Settings(api_budget_mode='steps'),tmp_path)
    model.client.close()
    requests=[]
    def respond(request):
        requests.append(1)
        if failure=='transport':raise httpx.ReadTimeout('timeout',request=request)
        if failure=='http':return httpx.Response(503)
        return httpx.Response(200,json={'choices':[{'message':{'content':'incomplete JSON'},'finish_reason':'stop'}]})
    model.client=httpx.Client(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(RuntimeError,match='模型调用失败'):model.json('writer','broken request')
        assert len(requests)==model.calls==3 and waits==[5,10]
        assert not list((tmp_path/'llm').glob('*.json'))
    finally:model.client.close()


def test_stock_production_configs_use_step_bounds_without_invalidating_existing_fingerprints():
    paths=sorted(Path(__file__).resolve().parents[1].glob('configs*.toml'))
    assert len(paths)>=7
    for path in paths:
        cfg=Settings.load(path)
        assert cfg.api_budget_mode=='steps'
        fixed=cfg.model_copy(update={'api_budget_mode':'fixed'})
        assert cfg.model_dump(mode='json')==fixed.model_dump(mode='json')
        assert digest(cfg.model_dump(mode='json'))==digest(fixed.model_dump(mode='json'))
        assert 'api_budget_mode' not in cfg.model_dump(mode='json')
        assert cfg.api_call_policy()['global_request_limit'] is None
        assert fixed.api_call_policy()['global_request_limit']==100


def test_legacy_explicit_limit_remains_available():
    cfg=Settings(max_api_calls=2)
    assert cfg.api_call_policy()['mode']=='fixed'
    assert cfg.api_call_policy()['global_request_limit']==2
    with pytest.raises(ValueError):Settings(max_api_calls=0)
