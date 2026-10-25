"""Whole-script protocol recovery must retain actual editorial findings."""
from copy import deepcopy
import json

import pytest

from autoclip.config import Settings
from autoclip.models import Beat, Clip, Line
from autoclip.narrative import (
    NARRATIVE_CRITERIA,
    require_commentary_review,
    review_commentary,
)
from autoclip.util import read_json


def fixture(repair_attempts=2):
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,
                 max_seconds=1320,repair_attempts=repair_attempts)
    clip=Clip(title='先把条件说清',angle='交代当前交锋',hook='对方如何回应',beats=[
        Beat(mode='original',start=0,end=20,text='',evidence_ids=[0],reason='完整开场'),
        Beat(mode='narration',start=20,end=40,text='他把条件说清，要求对方先给一个交代。',evidence_ids=[1],reason='讲清条件'),
        Beat(mode='original',start=40,end=60,text='',evidence_ids=[2],reason='保留回应'),
        Beat(mode='narration',start=60,end=80,text='对方没有答应，转而追问这件事由谁负责。',evidence_ids=[3],reason='说明追问')])
    lines=[Line(id=i,start=i*20+1,end=i*20+10,text=text) for i,text in enumerate([
        '请按规矩办事', '先给一个交代', '别碰这位客人', '这件事由谁负责'])]
    return clip,lines,cfg


def finding(beat,quote,action='rewrite'):
    return {'beat':beat,'quote':quote,'problem':'当前表述需要衔接或精简',
            'direction':'依据当前实际音轨修订，保留真实问题','action':action}


def rejected(issues):
    return {'checks':[{'criterion':c,'passed':False if c in {'continuity','selection'} else True,
                       'reason':'仍有具体问题' if c in {'continuity','selection'} else '通过'}
                      for c in sorted(NARRATIVE_CRITERIA)],
            'issues':issues}


def feedback_problems(prompt,round_number):
    feedback=prompt.split(f'\n第{round_number}轮格式或引用无效：',1)[1]
    data=feedback.split('请同时修正所有整稿引用和操作问题：',1)[1]
    return json.JSONDecoder().raw_decode(data)[0]


def test_one_retry_reports_all_bad_quotes_indices_and_actions_together(tmp_path):
    clip,lines,cfg=fixture()
    invalid=rejected([
        finding(1,'这句不在当前旁白中'),
        finding(99,'段号不存在'),
        finding(2,lines[2].text,'rewrite'),
        finding(0,lines[0].text,'drop')])
    original=deepcopy(invalid)
    corrected=rejected([
        finding(1,clip.beats[1].text),
        finding(3,clip.beats[3].text),
        finding(2,lines[2].text,'trim'),
        finding(0,lines[0].text,'trim')])
    prompts=[]
    class Model:
        def json(self,system,prompt):
            prompts.append(prompt)
            if len(prompts)==1:return invalid
            problems=feedback_problems(prompt,1)
            assert {p['issue'] for p in problems}=={0,1,2,3}
            by_issue={p['issue']:p for p in problems}
            assert by_issue[0]['actual_text']==clip.beats[1].text
            assert by_issue[0]['submitted_quote']=='这句不在当前旁白中'
            assert by_issue[1]['beat']==99 and by_issue[1]['allowed_beats']==[0,1,2,3]
            assert by_issue[2]['allowed_actions']==['drop','trim','narrate']
            assert by_issue[3]['allowed_actions']==['trim']
            assert '上轮完整响应：' in prompt and '不得为了修格式把问题清空' in prompt
            return corrected
    report=review_commentary(clip,lines,cfg,Model(),tmp_path)
    assert len(prompts)==2 and invalid==original
    assert report['passed'] is False and report['issues']==corrected['issues']
    protocol=read_json(next(tmp_path.glob('narrative-review/*/protocol-0.json')))
    assert protocol['raw']==original and '段号不存在' in protocol['error']
    assert not (tmp_path/'plan-approved.json').exists()


def test_repeated_bad_quote_uses_distinct_retry_prompts_instead_of_failed_cache(tmp_path):
    clip,lines,cfg=fixture(repair_attempts=2)
    invalid=rejected([finding(1,'完全不属于当前音轨的句子')])
    corrected=rejected([finding(1,clip.beats[1].text)])
    class CachedModel:
        def __init__(self):self.cache={};self.requests=[]
        def json(self,system,prompt):
            if prompt in self.cache:return self.cache[prompt]
            self.requests.append(prompt)
            self.cache[prompt]=deepcopy(invalid if len(self.requests)<3 else corrected)
            return self.cache[prompt]
    model=CachedModel()
    report=review_commentary(clip,lines,cfg,model,tmp_path)
    assert len(model.requests)==3
    assert model.requests[1]!=model.requests[2]
    assert '\n第1轮格式或引用无效：' in model.requests[1]
    assert '\n第2轮格式或引用无效：' in model.requests[2]
    for prompt in model.requests[1:]:
        assert '完全不属于当前音轨的句子' in prompt
        assert clip.beats[1].text in prompt
    assert report['passed'] is False and report['issues']==corrected['issues']
    assert len(list(tmp_path.glob('narrative-review/*/protocol-*.json')))==2
    with pytest.raises(ValueError,match='不能交付'):
        require_commentary_review(report,clip)


def test_valid_editorial_rejection_is_returned_without_retrying_until_it_disappears(tmp_path):
    clip,lines,cfg=fixture()
    raw=rejected([finding(2,lines[2].text,'trim')])
    calls=[]
    class Model:
        def json(self,*args):calls.append(1);return deepcopy(raw)
    report=review_commentary(clip,lines,cfg,Model(),tmp_path)
    assert calls==[1] and report['issues']==raw['issues'] and report['passed'] is False
    assert not list(tmp_path.glob('narrative-review/*/protocol-*.json'))
    with pytest.raises(ValueError,match='不能交付'):
        require_commentary_review(report,clip)


@pytest.mark.parametrize('action',['drop','narrate','rewrite'])
def test_original_opening_only_accepts_trim_after_explicit_feedback(tmp_path,action):
    clip,lines,cfg=fixture(repair_attempts=1)
    prompts=[]
    class Model:
        def json(self,system,prompt):
            prompts.append(prompt)
            if len(prompts)==1:return rejected([finding(0,lines[0].text,action)])
            problems=feedback_problems(prompt,1)
            assert len(problems)==1
            assert problems[0]['beat']==0 and problems[0]['actual_text']==lines[0].text
            assert problems[0]['submitted_action']==action
            assert problems[0]['allowed_actions']==['trim']
            return rejected([finding(0,lines[0].text,'trim')])
    report=review_commentary(clip,lines,cfg,Model(),tmp_path)
    assert len(prompts)==2 and report['issues'][0]['action']=='trim'
    assert report['passed'] is False and not (tmp_path/'plan-approved.json').exists()
