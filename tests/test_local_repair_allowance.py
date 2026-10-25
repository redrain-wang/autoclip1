"""One CLI run can fix newly found shots without resetting repeated failures."""
import pytest

import autoclip.series as series
from autoclip.util import read_json
from test_editorial_preflight import ready_fixture, measured_draft, whole_report


def setup_runtime(monkeypatch, events):
    monkeypatch.setattr(series,'measure_sample_plan',measured_draft(events))
    monkeypatch.setattr(series,'duration',lambda _:1480)


@pytest.mark.parametrize('category',['selection','editorial'])
def test_different_targets_finish_in_one_run_with_final_source_approval(tmp_path,monkeypatch,category):
    plan,lines,cfg=ready_fixture();cfg.repair_attempts=1
    for beat in plan.clips[0].beats:
        if beat.mode=='narration':beat.text+='据'*25
    events=[];reviews=[];audits=[]
    setup_runtime(monkeypatch,events)
    def review(clip,*args,**kwargs):
        reviews.append(clip.model_dump())
        if len(reviews)>4:return whole_report(clip)
        index=len(clip.beats)-1 if category=='selection' else len(reviews)
        return whole_report(clip,[{'beat':index,'quote':'重复','problem':'该段仍有重复',
            'direction':'删除重复','action':'drop' if category=='selection' else 'rewrite'}])
    def edit(current,issues,*args,**kwargs):
        revised=current.model_copy(deep=True)
        revised.clips[0].beats[int(issues[0]['id'].split('-')[1])].text+='补清'
        return revised
    def audit(video,current,*args):
        assert read_json(tmp_path/'editorial-preflight.json')['status']=='passed'
        audits.append(current.model_dump())
        return {'passed':True,'issues':[]}
    monkeypatch.setattr(series,'review_commentary',review)
    monkeypatch.setattr(series,'repair_plan',edit)
    monkeypatch.setattr(series,'audit_plan',audit)
    final,report=series.complete_long_plan(tmp_path/'video',plan,lines,{},tmp_path,cfg,None)
    budget=read_json(tmp_path/'repair-budget.json')
    assert report['passed'] and len(reviews)==5 and audits==[final.model_dump()]
    assert budget['used'][category]==4 and set(budget['used_per_target'].values())=={1}
    assert read_json(tmp_path/'plan-approved.json')==final.model_dump()


@pytest.mark.parametrize('split',[False,True])
def test_shortening_or_splitting_source_does_not_reset_its_allowance(tmp_path,monkeypatch,split):
    plan,lines,cfg=ready_fixture();cfg.repair_attempts=1
    events=[];reviews=[];writes=[]
    setup_runtime(monkeypatch,events)
    def review(clip,*args,**kwargs):
        reviews.append(clip.model_dump())
        index=19 if split and len(reviews)>1 else 18
        return whole_report(clip,[{'beat':index,'quote':'重复','problem':'原声重复',
            'direction':'缩短本段','action':'trim'}])
    class Model:
        def json(self,*args):
            writes.append(1)
            cuts=[(720,725),(732,737)] if split else [(720,750)]
            return {'changes':[{'beat':18,'action':'trim','start':a,'end':b,'evidence_ids':[]}
                               for a,b in cuts]}
    monkeypatch.setattr(series,'review_commentary',review)
    monkeypatch.setattr(series,'audit_plan',lambda *a:pytest.fail('未解决的原声不能进入批准'))
    with pytest.raises(ValueError,match='自动修复达到上限'):
        series.complete_long_plan(tmp_path/'video',plan,lines,{},tmp_path,cfg,Model())
    assert len(reviews)==2 and writes==[1]
    assert read_json(tmp_path/'repair-budget.json')['used_per_target']=={'selection:source-18':1}
    assert not (tmp_path/'plan-approved.json').exists()


def test_rewording_same_paragraph_does_not_reset_editorial_allowance(tmp_path,monkeypatch):
    plan,lines,cfg=ready_fixture();cfg.repair_attempts=1;events=[];edits=[]
    setup_runtime(monkeypatch,events)
    def review(clip,*args,**kwargs):
        return whole_report(clip,[{'beat':1,'quote':clip.beats[1].text,'problem':'解释仍重复',
                                  'direction':'删去复述','action':'rewrite'}])
    def edit(current,*args,**kwargs):
        edits.append(1);revised=current.model_copy(deep=True);revised.clips[0].beats[1].text+='换词'
        return revised
    monkeypatch.setattr(series,'review_commentary',review)
    monkeypatch.setattr(series,'repair_plan',edit)
    monkeypatch.setattr(series,'audit_plan',lambda *a:pytest.fail('换词不能冒充整稿通过'))
    with pytest.raises(ValueError,match='自动修复达到上限'):
        series.complete_long_plan(tmp_path/'video',plan,lines,{},tmp_path,cfg,None)
    assert edits==[1]
    assert read_json(tmp_path/'repair-budget.json')['used_per_target']=={'editorial:source-1':1}
    assert not (tmp_path/'plan-approved.json').exists()
