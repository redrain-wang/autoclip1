"""Editorial cleanup has its own allowance and may pass through a short draft."""
import pytest

from autoclip.models import Plan
from autoclip.narrative import (
    NARRATIVE_CRITERIA,
    NARRATIVE_POLICY,
    NARRATIVE_REVIEW_POLICY,
    require_commentary,
)
from autoclip.util import digest, read_json, source_identity, write_json
from test_commentary_balance import fixture


def healthy_fixture():
    plan,lines,cfg,_=fixture()
    plan.clips[0].beats[0].text+='据'*30
    assert require_commentary(plan.clips[0],cfg)['narration_chars']==1812
    return plan,lines,cfg


def whole_report(clip,issues):
    return {'policy':NARRATIVE_POLICY,'review_policy':NARRATIVE_REVIEW_POLICY,
        'clip_digest':digest(clip.model_dump()),'passed':not issues,'issues':issues,
        'checks':[{'criterion':c,'passed':not issues or c not in {'continuity','selection'},
                   'reason':'已通过' if not issues else '有具体问题'} for c in sorted(NARRATIVE_CRITERIA)]}


@pytest.mark.parametrize('mixed_selection',[False,True])
def test_exhausted_content_budget_does_not_prevent_editorial_cleanup(tmp_path,monkeypatch,mixed_selection):
    import autoclip.series as series
    import autoclip.selection as selection
    plan,lines,cfg=healthy_fixture();events=[];audits=[];whole=[]
    def measure(video,path,transcript,cfg,root):
        current=Plan.model_validate(read_json(path));events.append('measure')
        write_json(root/'plan-timed.json',current.model_dump())
        return {'issues':[],'beats':[{'output_seconds':len(b.text)/4.5 if b.mode=='narration' else b.end-b.start}
                                    for b in current.clips[0].beats]}
    def audit(video,current,*args):
        audits.append(current.model_dump());events.append('audit')
        return {'passed':len(audits)>1,'issues':[] if len(audits)>1 else [{'id':'beat-1','message':'事实需要纠正'}]}
    def review(clip,*args,**kwargs):
        whole.append(clip.model_dump());events.append('whole')
        assert not (tmp_path/'plan-approved.json').exists()
        narration={'beat':1,'quote':clip.beats[1].text,'problem':'需要衔接','direction':'补清承接','action':'rewrite'}
        if len(whole)==1:return whole_report(clip,[])
        if mixed_selection and len(whole)==2:
            return whole_report(clip,[narration,{'beat':36,'quote':'原声过场','problem':'不推进主线',
                'direction':'删除','action':'drop'}])
        if len(whole)==(3 if mixed_selection else 2):return whole_report(clip,[narration])
        return whole_report(clip,[])
    def repair(current,issues,*args,**kwargs):
        kind='editorial' if all(i.get('stage')=='narrative_review' for i in issues) else 'content'
        events.append('repair_'+kind)
        budget=read_json(tmp_path/'repair-budget.json')['used']
        assert budget['content']==1  # The single factual repair allowance is spent.
        if kind=='editorial':assert budget['editorial']==1
        else:assert budget['editorial']==0
        revised=current.model_copy(deep=True);revised.clips[0].beats[1].text+='据'
        return revised
    def drop(current,issues,*args,**kwargs):
        events.append('selection')
        assert len(issues)==1 and issues[0]['stage']=='selection_review'
        assert issues[0]['id']=='original-36'
        revised=current.model_copy(deep=True);revised.clips[0].beats.pop()
        return revised
    monkeypatch.setattr(series,'measure_sample_plan',measure)
    monkeypatch.setattr(series,'audit_plan',audit)
    monkeypatch.setattr(series,'review_commentary',review)
    monkeypatch.setattr(series,'repair_plan',repair)
    monkeypatch.setattr(series,'duration',lambda _:1480)
    monkeypatch.setattr(selection,'repair_original_selection',drop)
    final,report=series.complete_long_plan(tmp_path/'video.mp4',plan,lines,{},tmp_path,cfg,None)
    assert report['passed'] and report['issues']==[]
    assert events.count('repair_content')==events.count('repair_editorial')==1
    assert events.count('selection')==int(mixed_selection)
    if mixed_selection:assert events.index('selection')<events.index('repair_editorial')
    assert len(audits)==2  # Only the editorially complete draft reaches source review.
    assert len(whole)==(4 if mixed_selection else 3)  # Source changes invalidate the preflight.
    assert events.index('audit')<events.index('repair_content')<events.index('repair_editorial')
    assert events[-2:]==['whole','audit']
    used=read_json(tmp_path/'repair-budget.json')['used']
    assert used=={'timing':0,'content':1,'selection':int(mixed_selection),'commentary':0,'editorial':1}
    assert require_commentary(final.clips[0],cfg)['narration_chars']>=1800
    assert (tmp_path/'plan-approved.json').exists()


def test_verified_editorial_condensation_can_create_a_temporary_global_deficit(tmp_path,monkeypatch):
    import autoclip.automatic as automatic
    plan,lines,cfg=healthy_fixture();before=plan.model_dump();checks=[];duration_checks=[]
    video=tmp_path/'video.mp4';video.write_bytes(b'source')
    frame=tmp_path/'frame.jpg';frame.write_bytes(b'pixels')
    issue={'id':'beat-0','stage':'narrative_review','text_digest':digest(plan.clips[0].beats[0].text),
           'message':'删除重复的原声复述，只保留必要承接'}
    class Model:
        def json(self,*args):return {'changes':[{'id':'beat-0','text':'这件事仍需对方先作交代。'}]}
    def editorial(clip,index,issues,*args,**kwargs):
        checks.append('editorial')
        assert index==0 and issues==[issue]
        assert kwargs['retries']==cfg.repair_attempts
        assert not (tmp_path/'plan-approved.json').exists()
    def source(video,candidate,uid,*args,**kwargs):
        checks.append('source');assert uid=='beat-0'
    original_validate=automatic.validate_plan
    def validate(*args,**kwargs):
        duration_checks.append(kwargs.get('check_duration',True))
        return original_validate(*args,**kwargs)
    monkeypatch.setattr(automatic,'contact_sheet',lambda *a:frame)
    monkeypatch.setattr(automatic,'subtitle_evidence_sheet',lambda *a:frame)
    monkeypatch.setattr(automatic,'verify_editorial_patch',editorial)
    monkeypatch.setattr(automatic,'verify_narration_patch',source)
    monkeypatch.setattr(automatic,'validate_plan',validate)
    revised=automatic.repair_plan(plan,[issue],lines,{'source':source_identity(video),'lines':[]},
                                  tmp_path,cfg,Model(),0,source_seconds=1480)
    assert checks==['editorial','source']
    assert duration_checks==[False]  # Actual duration is measured after the local edit.
    assert revised.clips[0].beats[1:]==plan.clips[0].beats[1:] and plan.model_dump()==before
    with pytest.raises(ValueError,match='碎片过场'):require_commentary(revised.clips[0],cfg)
    assert read_json(tmp_path/'pending-repair.json')['status']=='requires_full_measurement_and_review'
    assert not (tmp_path/'plan-approved.json').exists()

    # Even after both local checks, a deficient film cannot become approved.
    import autoclip.series as series
    def measure(video,path,transcript,cfg,root):
        current=Plan.model_validate(read_json(path))
        write_json(root/'plan-timed.json',current.model_dump())
        return {'issues':[],'beats':[{'output_seconds':len(b.text)/4.5 if b.mode=='narration' else b.end-b.start}
                                    for b in current.clips[0].beats]}
    class Picker:
        def json(self,*args):return {'choices':[{'beat':1,'reason':'主要交锋还需讲清约束','evidence_ids':[2,3]}]}
    monkeypatch.setattr(series,'measure_sample_plan',measure)
    monkeypatch.setattr(series,'audit_plan',lambda *a:pytest.fail('未补足前不能宣布进入最终审核通过'))
    with pytest.raises(ValueError,match='自动修复达到上限'):
        series.complete_long_plan(video,revised,lines,{},tmp_path,cfg.model_copy(update={'repair_attempts':0}),Picker())
    assert read_json(tmp_path/'repair-budget.json')['pending']=='commentary'
    assert not (tmp_path/'plan-approved.json').exists()


@pytest.mark.parametrize('fault',['stale_quote','malformed'])
def test_editorial_verifier_retries_protocol_without_rewriting_or_clearing_real_rejection(tmp_path,fault):
    from copy import deepcopy
    from autoclip.narrative import verify_editorial_patch
    plan,lines,_=healthy_fixture();clip=plan.clips[0]
    clip.beats[0].text='他把前面已经说过的理由又完整讲了一遍。'
    before=clip.model_dump()
    bad=({'resolved':False,'reason':'仍有重复','remaining_quote':'这句已经从旧稿删除'} if fault=='stale_quote' else
         {'passed':False,'issues':[]})
    good={'resolved':False,'reason':'仍有重复，必须修改','remaining_quote':'前面已经说过的理由'}
    class CachedReviewer:
        def __init__(self):self.cache={};self.requests=[]
        def json(self,system,prompt):
            assert '局部整稿编辑' in system and '局部修稿编辑' not in system
            if prompt in self.cache:return self.cache[prompt]
            self.requests.append(prompt)
            self.cache[prompt]=deepcopy(bad if len(self.requests)<3 else good)
            return self.cache[prompt]
    model=CachedReviewer()
    with pytest.raises(ValueError,match='整稿修订仍未落实'):
        verify_editorial_patch(clip,0,[{'message':'删除重复'}],lines,model,tmp_path,retries=2)
    assert len(model.requests)==3 and model.requests[1]!=model.requests[2]
    assert '第1次核验响应' in model.requests[1] and '第2次核验响应' in model.requests[2]
    for prompt in model.requests[1:]:
        assert clip.beats[0].text in prompt and '上轮核验：' in prompt
        assert '稿件不变' in prompt and '不能为了修格式宣布通过' in prompt
    check=read_json(tmp_path/'editorial-verification.json')
    assert check['resolved'] is False and check['remaining_quote']==good['remaining_quote']
    assert len(list(tmp_path.glob('editorial-protocol-*.json')))==2
    assert clip.model_dump()==before and not (tmp_path/'plan-approved.json').exists()
