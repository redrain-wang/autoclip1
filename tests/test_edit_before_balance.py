"""Do not spend the film's growth allowance on unfinished editorial drafts."""
import pytest

from autoclip.models import Plan
from autoclip.narrative import require_commentary, require_commentary_review
from autoclip.util import read_json, write_json
from test_duration_balance import short_fixture, measured
from test_editorial_preflight import whole_report


def setup_pipeline(monkeypatch, tmp_path):
    import autoclip.series as series
    plan, lines, cfg = short_fixture()
    cfg.repair_attempts = 3
    events = []
    def measure(video,path,transcript,cfg,root):
        current=Plan.model_validate(read_json(path))
        write_json(root/'plan-timed.json',current.model_dump())
        events.append('measure')
        return measured(current,cfg)
    def audit(video,current,*args):
        events.append('audit')
        assert measured(current,cfg)['issues']==[]
        require_commentary(current.clips[0],cfg,measured(current,cfg))
        require_commentary_review(read_json(tmp_path/'editorial-preflight.json')['narrative_review'],current.clips[0])
        assert not (tmp_path/'plan-approved.json').exists()
        return {'passed':True,'issues':[]}
    monkeypatch.setattr(series,'measure_sample_plan',measure)
    monkeypatch.setattr(series,'audit_plan',audit)
    monkeypatch.setattr(series,'duration',lambda _:1360)
    return series,plan,lines,cfg,events


def picker(events, indices):
    class Model:
        def json(self,system,prompt):
            assert '只选择有真实讲述价值' in system
            events.append('choose')
            return {'choices':[{'beat':i,'reason':'当前主要交锋的必要解释','evidence_ids':[i*2,i*2+1]} for i in indices]}
    return Model()


def grow(current,issues):
    revised=current.model_copy(deep=True)
    for issue in issues:
        assert issue['stage']=='commentary_balance'
        revised.clips[0].beats[int(issue['id'].split('-')[1])].text='据'*issue['minimum_chars']
    return revised


def test_finish_editorial_before_balancing_a_short_and_underwritten_intermediate(tmp_path,monkeypatch):
    series,plan,lines,cfg,events=setup_pipeline(monkeypatch,tmp_path)
    heard=[]
    def preflight(clip,*args,**kwargs):
        events.append('whole');heard.append(clip.model_dump())
        if len(heard)==1:
            return whole_report(clip,[{'beat':2,'quote':clip.beats[2].text,'problem':'段尾重复',
                                      'direction':'删除同义重复','action':'rewrite'}])
        return whole_report(clip)
    def repair(current,issues,*args,**kwargs):
        if issues[0].get('stage')=='narrative_review':
            events.append('edit')
            revised=current.model_copy(deep=True)
            revised.clips[0].beats[2].text='据'*81
            revised.clips[0].beats[2].end=revised.clips[0].beats[2].start+24
            assert measured(revised,cfg)['measured_total_seconds']<1080
            with pytest.raises(ValueError,match='碎片过场'):
                require_commentary(revised.clips[0],cfg,measured(revised,cfg))
            return revised
        events.append('balance')
        assert len(heard)==2  # Both the short draft and its cleanup were reviewed first.
        return grow(current,issues)
    monkeypatch.setattr(series,'review_commentary',preflight)
    monkeypatch.setattr(series,'repair_plan',repair)
    final,report=series.complete_long_plan(tmp_path/'source.mp4',plan,lines,{},tmp_path,cfg,picker(events,(0,1,3)))
    assert events==['measure','whole','edit','measure','whole','choose','balance','measure','whole','audit']
    assert measured(final,cfg)['issues']==[] and report['passed']
    used=read_json(tmp_path/'repair-budget.json')['used']
    assert used['editorial']==used['commentary']==1 and used['timing']==0
    assert read_json(tmp_path/'plan-approved.json')==final.model_dump()


@pytest.mark.parametrize('automatic_alignment',[False,True])
def test_short_intermediate_aligns_only_the_broken_shot_and_does_not_rewrite_every_narration(tmp_path,monkeypatch,automatic_alignment):
    series,plan,lines,cfg,events=setup_pipeline(monkeypatch,tmp_path)
    plan.clips[0].beats[0].end=10
    first=measured(plan,cfg)
    assert {i['code'] for i in first['issues']}=={'voice_alignment','duration_short'}
    targets=[]
    def preflight(clip,*args,**kwargs):events.append('whole');return whole_report(clip)
    def repair(current,issues,*args,**kwargs):
        if issues[0].get('stage')=='commentary_balance':
            events.append('balance');return grow(current,issues)
        events.append('align')
        targets.extend(i['id'] for i in issues)
        assert targets==['beat-0']
        assert len(issues)==1 and '口播' in issues[0]['message']
        revised=current.model_copy(deep=True);revised.clips[0].beats[0].end=30
        return revised
    def candidates(clip,timing,lines,cfg,**kwargs):
        events.append('candidates')
        assert kwargs['allow_original'] is False
        if not automatic_alignment:return []
        aligned=plan.model_copy(deep=True);aligned.clips[0].beats[0].end=30
        report=measured(aligned,cfg)
        assert [i['code'] for i in report['issues']]==['duration_short']
        return [{'clip':aligned.clips[0].model_dump(),'timing':report}]
    monkeypatch.setattr(series,'review_commentary',preflight)
    monkeypatch.setattr(series,'repair_plan',repair)
    monkeypatch.setattr('autoclip.recut.cut_candidates',candidates)
    final,report=series.complete_long_plan(tmp_path/'source.mp4',plan,lines,{},tmp_path,cfg,picker(events,(0,1)))
    assert report['passed'] and measured(final,cfg)['issues']==[]
    assert events.count('audit')==1 and events.count('balance')==1 and events.count('candidates')==1
    assert targets==([] if automatic_alignment else ['beat-0'])
    used=read_json(tmp_path/'repair-budget.json')['used']
    assert used['timing']==int(not automatic_alignment) and used['commentary']==1
    assert final.clips[0].beats[2:]==plan.clips[0].beats[2:]


def test_passing_editorial_and_commentary_cannot_approve_an_unfilled_duration_gap(tmp_path,monkeypatch):
    series,plan,lines,cfg,events=setup_pipeline(monkeypatch,tmp_path)
    assert require_commentary(plan.clips[0],cfg,measured(plan,cfg))['narration_chars']==1818
    monkeypatch.setattr(series,'review_commentary',lambda clip,*a,**k:whole_report(clip))
    monkeypatch.setattr(series,'commentary_balance_issues',lambda *a,**k:[])
    with pytest.raises(ValueError,match='实测时长仍不足'):
        series.complete_long_plan(tmp_path/'source.mp4',plan,lines,{},tmp_path,cfg,None)
    assert events==['measure']
    assert not (tmp_path/'plan-approved.json').exists()
    assert read_json(tmp_path/'editorial-preflight.json')['status']=='passed'
