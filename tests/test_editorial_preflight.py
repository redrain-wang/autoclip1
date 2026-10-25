"""Finish bounded editorial work before the final whole-film source audit."""
import pytest

from autoclip.models import Plan
from autoclip.narrative import NARRATIVE_CRITERIA, NARRATIVE_POLICY, NARRATIVE_REVIEW_POLICY, require_commentary_review
from autoclip.selection import repair_original_selection
from autoclip.util import digest, read_json, write_json
from test_commentary_balance import fixture


def ready_fixture():
    plan,lines,cfg,_=fixture()
    plan.clips[0].beats[0].text+='据'*30
    return plan,lines,cfg


def whole_report(clip,issues=()):
    return {'policy':NARRATIVE_POLICY,'review_policy':NARRATIVE_REVIEW_POLICY,
        'clip_digest':digest(clip.model_dump()),'passed':not issues,'issues':list(issues),
        'checks':[{'criterion':c,'passed':not issues,'reason':'检查当前实配稿'}
                  for c in sorted(NARRATIVE_CRITERIA)]}


def measured_draft(events):
    def measure(video,path,transcript,cfg,root):
        events.append('measure')
        current=Plan.model_validate(read_json(path))
        write_json(root/'plan-timed.json',current.model_dump())
        return {'issues':[],'beats':[{'output_seconds':len(b.text)/4.5 if b.mode=='narration' else b.end-b.start}
                                    for b in current.clips[0].beats]}
    return measure


def test_multiple_selection_and_editorial_rounds_source_audit_only_the_final_measured_draft(tmp_path,monkeypatch):
    import autoclip.series as series
    plan,lines,cfg=ready_fixture();cfg.repair_attempts=2
    events=[];heard=[];audited=[]
    def preflight(clip,*args,**kwargs):
        events.append('whole');heard.append(clip.model_dump())
        assert not (tmp_path/'plan-approved.json').exists()
        assert not (tmp_path/'automatic-review.json').exists()
        rewrite={'beat':1,'quote':clip.beats[1].text,'problem':'重复复述','direction':'精简重复并补清承接','action':'rewrite'}
        if len(heard)<3:
            index=len(clip.beats)-1
            # A structural edit must discard the accompanying old paragraph finding.
            return whole_report(clip,[rewrite,{'beat':index,'quote':'过场','problem':'不推进主冲突',
                'direction':'删除本段','action':'drop'}])
        return whole_report(clip,[rewrite] if len(heard)==3 else [])
    def edit(current,issues,*args,**kwargs):
        events.append('edit')
        assert len(issues)==1 and issues[0]['id']=='beat-1' and issues[0]['stage']=='narrative_review'
        revised=current.model_copy(deep=True);revised.clips[0].beats[1].text+='补清'
        return revised
    def audit(video,current,*args):
        events.append('audit');audited.append(current.model_dump())
        pre=read_json(tmp_path/'editorial-preflight.json')
        assert pre['status']=='passed' and pre['plan_digest']==digest(current.model_dump())
        require_commentary_review(pre['narrative_review'],current.clips[0])
        assert not (tmp_path/'plan-approved.json').exists()
        return {'passed':True,'issues':[],'plan_digest':digest(current.model_dump()),'source_proof':'fixed-test-proof'}
    monkeypatch.setattr(series,'measure_sample_plan',measured_draft(events))
    monkeypatch.setattr(series,'review_commentary',preflight)
    monkeypatch.setattr(series,'repair_plan',edit)
    monkeypatch.setattr(series,'audit_plan',audit)
    monkeypatch.setattr(series,'duration',lambda _:1480)
    final,report=series.complete_long_plan(tmp_path/'video.mp4',plan,lines,{},tmp_path,cfg,None)
    assert events==['measure','whole','measure','whole','measure','whole','edit','measure','whole','audit']
    assert audited==[final.model_dump()] and len(final.clips[0].beats)==35
    assert report['source_proof']=='fixed-test-proof'
    require_commentary_review(report['narrative_review'],final.clips[0])
    assert read_json(tmp_path/'plan-approved.json')==final.model_dump()
    assert read_json(tmp_path/'automatic-review.json')==report
    assert read_json(tmp_path/'repair-budget.json')['used']=={
        'timing':0,'content':0,'selection':2,'commentary':0,'editorial':1}


def test_factual_repair_invalidates_preflight_and_requires_new_measurement_whole_review_and_source(tmp_path,monkeypatch):
    import autoclip.series as series
    plan,lines,cfg=ready_fixture();events=[];heard=[];audited=[]
    def preflight(clip,*args,**kwargs):
        events.append('whole');heard.append(clip.model_dump())
        return whole_report(clip)
    def audit(video,current,*args):
        events.append('audit');audited.append(current.model_dump())
        assert not (tmp_path/'plan-approved.json').exists()
        return {'passed':len(audited)==2,'issues':[] if len(audited)==2 else
                [{'id':'beat-1','message':'原片只支持另一种动作归属'}]}
    def factual(current,issues,*args,**kwargs):
        events.append('fact')
        assert issues==[{'id':'beat-1','message':'原片只支持另一种动作归属'}]
        revised=current.model_copy(deep=True);revised.clips[0].beats[1].text+='更正'
        return revised
    monkeypatch.setattr(series,'measure_sample_plan',measured_draft(events))
    monkeypatch.setattr(series,'review_commentary',preflight)
    monkeypatch.setattr(series,'repair_plan',factual)
    monkeypatch.setattr(series,'audit_plan',audit)
    monkeypatch.setattr(series,'duration',lambda _:1480)
    final,report=series.complete_long_plan(tmp_path/'video.mp4',plan,lines,{},tmp_path,cfg,None)
    assert events==['measure','whole','audit','fact','measure','whole','audit']
    assert heard==[p['clips'][0] for p in audited] and heard[0]!=heard[1]
    assert read_json(tmp_path/'editorial-preflight.json')['plan_digest']==digest(final.model_dump())
    require_commentary_review(report['narrative_review'],final.clips[0])
    with pytest.raises(ValueError,match='不能交付'):
        require_commentary_review(whole_report(plan.clips[0]),final.clips[0])


def test_passing_preflight_cannot_approve_a_source_rejection(tmp_path,monkeypatch):
    import autoclip.series as series
    plan,lines,cfg=ready_fixture();cfg.repair_attempts=0;events=[]
    def preflight(clip,*args,**kwargs):events.append('whole');return whole_report(clip)
    def audit(*args):events.append('audit');return {'passed':False,'issues':[{'id':'beat-1','message':'原片不支持'}]}
    monkeypatch.setattr(series,'measure_sample_plan',measured_draft(events))
    monkeypatch.setattr(series,'review_commentary',preflight)
    monkeypatch.setattr(series,'audit_plan',audit)
    with pytest.raises(ValueError,match='自动修复达到上限'):
        series.complete_long_plan(tmp_path/'video.mp4',plan,lines,{},tmp_path,cfg,None)
    assert events==['measure','whole','audit']
    assert read_json(tmp_path/'editorial-preflight.json')['status']=='passed'
    assert not (tmp_path/'plan-approved.json').exists()
    assert not (tmp_path/'automatic-review.json').exists()


@pytest.mark.parametrize('fault',['stale','missing_selection'])
def test_invalid_preflight_cannot_reach_source_or_approval(tmp_path,monkeypatch,fault):
    import autoclip.series as series
    plan,lines,cfg=ready_fixture();events=[]
    def preflight(clip,*args,**kwargs):
        report=whole_report(clip)
        if fault=='stale':report['clip_digest']='old-clip'
        else:report['checks']=[c for c in report['checks'] if c['criterion']!='selection']
        return report
    monkeypatch.setattr(series,'measure_sample_plan',measured_draft(events))
    monkeypatch.setattr(series,'review_commentary',preflight)
    monkeypatch.setattr(series,'audit_plan',lambda *a:pytest.fail('旧稿或不完整五项不能进入最终原片审核'))
    with pytest.raises(ValueError):
        series.complete_long_plan(tmp_path/'video.mp4',plan,lines,{},tmp_path,cfg,None)
    assert not (tmp_path/'plan-approved.json').exists()
    assert not (tmp_path/'editorial-preflight.json').exists()  # Invalid self-declared passes are never persisted as passed.


def test_selection_intermediate_quantity_deficit_is_pending_then_blocked_by_final_balance_gate(tmp_path,monkeypatch):
    import autoclip.series as series
    plan,lines,cfg=ready_fixture();cfg.repair_attempts=0
    for beat in plan.clips[0].beats:
        if beat.mode=='narration':beat.text='据'*80
    clip=plan.clips[0];last=clip.beats[-1]
    finding={'id':'original-36','stage':'selection_review','action':'drop',
        'review_policy':NARRATIVE_REVIEW_POLICY,'clip_digest':digest(clip.model_dump()),
        'beat_digest':digest(last.model_dump()),'message':'删去无价值过场'}
    revised=repair_original_selection(plan,[finding],lines,tmp_path,cfg,None,0,1480)
    assert revised.clips[0].beats==clip.beats[:-1]
    assert read_json(tmp_path/'selection-repairs/0/result.json')['status']=='requires_full_measurement_and_review'
    assert not (tmp_path/'plan-approved.json').exists()
    events=[]
    monkeypatch.setattr(series,'measure_sample_plan',measured_draft(events))
    monkeypatch.setattr(series,'audit_plan',lambda *a:pytest.fail('分量不足不能进入最终原片审核'))
    monkeypatch.setattr(series,'review_commentary',lambda *a,**k:pytest.fail('分量不足仍须先自动平衡'))
    with pytest.raises(ValueError,match='自动修复达到上限'):
        series.complete_long_plan(tmp_path/'video.mp4',revised,lines,{},tmp_path,cfg,None)
    assert read_json(tmp_path/'repair-budget.json')['pending']=='commentary'
    assert not (tmp_path/'plan-approved.json').exists()
