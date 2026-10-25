import json

import pytest

from autoclip.config import Settings
from autoclip.models import Beat, Clip, Line, Plan
from autoclip.narrative import commentary_balance_issues, require_commentary, NARRATIVE_CRITERIA
from autoclip.util import read_json, write_json, source_identity


def fixture():
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320,
                 opening='narration',repair_attempts=1)
    beats=[Beat(mode='narration' if i<18 else 'original',start=i*40,end=(i+1)*40,
        text='据'*99 if i<18 else '',evidence_ids=[i*2,i*2+1],reason='主要交锋的依据与约束') for i in range(37)]
    plan=Plan(episode_summary='',genre='',style='',clips=[Clip(title='主要交锋为何转向',angle='',hook='',beats=beats)])
    lines=[Line(id=i*2+j,start=i*40+1+j*12,end=i*40+4+j*12,text='原片提供的行动与回应') for i in range(37) for j in range(2)]
    timing={'issues':[],'beats':[{'output_seconds':22 if i<18 else 40} for i in range(37)]}
    return plan,lines,cfg,timing


def test_small_deficit_becomes_bounded_source_based_work_without_lowering_thresholds():
    plan,lines,cfg,timing=fixture();before=plan.model_dump()
    with pytest.raises(ValueError,match='碎片过场'):require_commentary(plan.clips[0],cfg,timing)
    issues=commentary_balance_issues(plan.clips[0],cfg,timing,lines)
    assert len(issues)==1 and issues[0]['stage']=='commentary_balance'
    assert issues[0]['minimum_chars']>=123
    assert '不重复原声' in issues[0]['message'] and '整稿五项' in issues[0]['message']
    assert plan.model_dump()==before
    assert commentary_balance_issues(plan.clips[0],cfg,timing,[])==[]
    assert commentary_balance_issues(plan.clips[0],Settings(),timing,lines)==[]


def test_editor_selects_main_conflict_and_cannot_invent_targets_or_citations(tmp_path):
    plan,lines,cfg,timing=fixture();prompts=[]
    class Model:
        def json(self,system,prompt):
            prompts.append(prompt)
            if len(prompts)==1:return {'choices':[{'beat':999,'reason':'不存在的片段','evidence_ids':[999]}]}
            assert '位置选择无效' in prompt and '普通待客' in prompt
            return {'choices':[{'beat':2,'reason':'还缺必要约束','evidence_ids':[4,5]}]}
    issues=commentary_balance_issues(plan.clips[0],cfg,timing,lines,Model(),tmp_path)
    assert len(prompts)==2 and issues[0]['id']=='beat-2'
    assert '还缺必要约束' in issues[0]['message']
    assert not (tmp_path/'plan-approved.json').exists()


@pytest.mark.parametrize('fault',['future','duplicate','original'])
def test_commentary_position_selection_rejects_scope_violations(fault):
    plan,lines,cfg,timing=fixture();cfg.repair_attempts=0
    class Model:
        def json(self,*args):
            choice={'beat':0,'reason':'补必要因果','evidence_ids':[72] if fault=='future' else [0,1]}
            if fault=='original':choice['beat']=36
            return {'choices':[choice,choice] if fault=='duplicate' else [choice]}
    with pytest.raises(ValueError,match='位置选择未通过'):
        commentary_balance_issues(plan.clips[0],cfg,timing,lines,Model())


@pytest.mark.parametrize('retry_writer',[False,True])
def test_public_pipeline_balances_then_remeasures_source_checks_and_reviews_whole_script(tmp_path,monkeypatch,retry_writer):
    import autoclip.series as module
    import autoclip.automatic as automatic
    plan,lines,cfg,timing=fixture();events=[];writer_calls=[];reasoning_writes=[]
    video=tmp_path/'video.mp4';video.write_bytes(b'source')
    frame=tmp_path/'frame.jpg';frame.write_bytes(b'pixels')
    write_json(tmp_path/'source.json',source_identity(video))
    def measure(video,path,transcript,cfg,root):
        current=Plan.model_validate(read_json(path));events.append('measure')
        write_json(root/'plan-timed.json',current.model_dump())
        return {'issues':[],'beats':[{'output_seconds':len(b.text)/4.5 if b.mode=='narration' else b.end-b.start}
                                    for b in current.clips[0].beats]}
    def audit(*args):events.append('audit');return {'passed':True,'issues':[]}
    class Model:
        def review_json(self,system,prompt,images=None):
            if '局部修稿编辑' in system:reasoning_writes.append(prompt)
            return self.json(system,prompt,images)
        def json(self,system,prompt,images=None):
            if '只选择有真实讲述价值' in system:
                events.append('choose');return {'choices':[{'beat':0,'reason':'补清当前约束','evidence_ids':[0,1]}]}
            if '局部修稿编辑' in system:
                writer_calls.append(prompt);events.append('write')
                return {'changes':[{'id':'beat-0','text':'据'*(100 if retry_writer and len(writer_calls)==1 else 123)}]}
            if '局部修订核验员' in system:
                events.append('verify')
                return {'issues':[],'units':[{'id':'beat-0','verdict':'supported','reason':'原片支持',
                    'evidence_ids':[0,1],'frame_ids':[0],'identity_basis':'no_identity_claim','subjects':[]}]}
            events.append('whole')
            return {'checks':[{'criterion':c,'passed':True,'reason':'通过'} for c in sorted(NARRATIVE_CRITERIA)],'issues':[]}
    monkeypatch.setattr(module,'measure_sample_plan',measure)
    monkeypatch.setattr(module,'audit_plan',audit)
    monkeypatch.setattr(module,'duration',lambda _:1480)
    monkeypatch.setattr(automatic,'contact_sheet',lambda *a:frame)
    monkeypatch.setattr(automatic,'subtitle_evidence_sheet',lambda *a:frame)
    final,report=module.complete_long_plan(video,plan,lines,{'source':source_identity(video),'lines':[]},tmp_path,cfg,Model())
    assert report['passed'] and require_commentary(final.clips[0],cfg)['narration_chars']==1806
    assert events==(['measure','whole','choose','write','write','verify','measure','whole','audit'] if retry_writer else
                    ['measure','whole','choose','write','verify','measure','whole','audit'])
    assert len(reasoning_writes)==int(retry_writer)
    assert read_json(tmp_path/'repair-budget.json')['used']=={'timing':0,'content':0,'selection':0,'commentary':1,'editorial':0}
    assert final.clips[0].beats[1:]==plan.clips[0].beats[1:]
    assert (tmp_path/'plan-approved.json').exists()
