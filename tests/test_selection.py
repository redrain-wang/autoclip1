import json

import pytest

from autoclip.config import Settings
from autoclip.models import Beat,Clip,Line,Plan
from autoclip.narrative import NARRATIVE_CRITERIA,NARRATIVE_REVIEW_POLICY,review_commentary,require_commentary_review
from autoclip.selection import original_runs,repair_original_selection
from autoclip.util import digest,read_json,write_json


def fixture():
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320,
        opening='original',repair_attempts=0)
    beats=[Beat(mode='original' if i%2==0 else 'narration',start=i*40,end=i*40+(12 if i==0 else 40),
        text='' if i%2==0 else '查明各处库存，再比较两项差事需要多少银粮。'*7,evidence_ids=[i],reason='剧情')
        for i in range(32)]
    lines=[Line(id=i,start=b.start+1,end=b.start+11,text='生活过场闲聊' if i==2 else '拨粮的争执与答复') for i,b in enumerate(beats)]
    return Plan(episode_summary='',genre='',style='',clips=[Clip(title='筹粮受阻',angle='筹粮主线',hook='',beats=beats)]),lines,cfg


def finding(plan,index,action):
    b=plan.clips[0].beats[index]
    return {'id':f'original-{index}','stage':'selection_review','action':action,
        'review_policy':NARRATIVE_REVIEW_POLICY,'clip_digest':digest(plan.clips[0].model_dump()),
        'beat_digest':digest(b.model_dump()),'message':'压缩不推进筹粮主线的生活过场'}


def test_output_original_run_accumulates_source_gaps_and_fallbacks():
    spans=[(1967.12,1972.86),(1998.54,2012.52),(2012.52,2051.24),
        (2051.24,2087.82),(2146.68,2185.58),(2185.58,2225.38)]
    clip=Clip(title='筹粮',angle='',hook='',beats=[Beat(mode='original',start=a,end=b,evidence_ids=[i],
        reason='局部口播修稿未通过，恢复原声' if i in (0,1,4) else '剧情') for i,(a,b) in enumerate(spans)])
    runs=original_runs(clip)
    assert len(runs)==1 and runs[0]['seconds']==173.72 and runs[0]['review_focus']
    assert runs[0]['restored_beats']==[0,1,4]
    timing={'beats':[{'output_seconds':25} for _ in spans]}
    assert original_runs(clip,timing)[0]['seconds']==150
    clip.beats[2].mode='narration';clip.beats[2].text='交代新的选择。'
    assert [r['beats'] for r in original_runs(clip)]==[[0,1],[3,4,5]]


@pytest.mark.parametrize('reject',[False,True])
def test_long_conflict_can_pass_but_short_irrelevant_original_can_be_flagged(tmp_path,reject):
    plan,lines,cfg=fixture();clip=plan.clips[0]
    # Four adjoining output shots may be a worthwhile long exchange, even
    # when they are not adjacent in source time.
    for i in (2,3,4,5):clip.beats[i].mode='original';clip.beats[i].text=''
    class Model:
        def json(self,system,prompt):
            runs=json.JSONDecoder().raw_decode(prompt.split('\noriginal_runs=',1)[1])[0]
            assert any(r['seconds']>=160 and r['review_focus'] for r in runs)
            assert '不是长度禁令' in prompt and '洗澡' in prompt and '修稿失败' in prompt
            assert 'editorial_goal=' in prompt
            return {'checks':[{'criterion':c,'passed':c!='selection' or not reject,'reason':'有冲突推进' if not reject else '过场无关主线'} for c in sorted(NARRATIVE_CRITERIA)],
                'issues':[{'beat':2,'quote':'生活过场闲聊','problem':'不推进主线','direction':'删去不影响理解','action':'drop'}] if reject else []}
    report=review_commentary(clip,lines,cfg,Model(),tmp_path)
    assert report['passed'] is not reject
    if reject:
        with pytest.raises(ValueError,match='不能交付'):require_commentary_review(report,clip)
    else:require_commentary_review(report,clip)


def test_selection_can_drop_original_without_inventing_bridge_or_changing_other_shots(tmp_path):
    plan,lines,cfg=fixture();before=plan.model_dump()
    class NoCalls:
        def json(self,*a):pytest.fail('明确删去的无价值原声不应再写空旁白')
    revised=repair_original_selection(plan,[finding(plan,2,'drop')],lines,tmp_path,cfg,NoCalls(),0,1300)
    assert revised.clips[0].beats==plan.clips[0].beats[:2]+plan.clips[0].beats[3:]
    assert plan.model_dump()==before
    assert read_json(tmp_path/'selection-repairs/0/result.json')['status']=='requires_full_measurement_and_review'


@pytest.mark.parametrize('case',['stale','opening','wrong_mode'])
def test_selection_rejects_stale_or_out_of_scope_requests(tmp_path,case):
    plan,lines,cfg=fixture();issue=finding(plan,0 if case=='opening' else 2,'drop')
    if case=='stale':issue['clip_digest']='old'
    if case=='wrong_mode':issue=finding(plan,1,'drop')
    with pytest.raises(ValueError):repair_original_selection(plan,[issue],lines,tmp_path,cfg,None,0,1300)


@pytest.mark.parametrize('case',['valid','cut_sentence','unchanged','outside','unknown_id','extra_target'])
def test_trim_original_only_keeps_complete_source_dialogue(tmp_path,case):
    plan,lines,cfg=fixture();line=lines[2]
    row={'beat':2,'action':'trim','start':line.start,'end':line.end,'evidence_ids':[2]}
    if case=='cut_sentence':row['start']+=1
    if case=='unchanged':row.update(start=80,end=120)
    if case=='outside':row['start']=79
    if case=='unknown_id':row['evidence_ids']=[999]
    class Model:
        def json(self,*a):return {'changes':[row,*([dict(row,beat=4)] if case=='extra_target' else [])]}
    args=(plan,[finding(plan,2,'trim')],lines,tmp_path,cfg,Model(),0,1300)
    if case!='valid':
        with pytest.raises(ValueError):repair_original_selection(*args)
        return
    revised=repair_original_selection(*args)
    assert revised.clips[0].beats[2].start==line.start and revised.clips[0].beats[2].end==line.end
    assert revised.clips[0].beats[2].mode=='original'


@pytest.mark.parametrize('borrow_future',[False,True])
def test_original_can_be_replaced_by_narration_but_not_borrow_future_evidence(tmp_path,borrow_future):
    plan,lines,cfg=fixture()
    class Model:
        def json(self,*a):return {'changes':[{'beat':2,'action':'narrate','start':80,'end':100,
            'text':'有人来通知，众人随后动身。','evidence_ids':[3 if borrow_future else 2]}]}
    args=(plan,[finding(plan,2,'narrate')],lines,tmp_path,cfg,Model(),0,1300)
    if borrow_future:
        with pytest.raises(ValueError,match='引用只能来自本段'):repair_original_selection(*args)
    else:
        revised=repair_original_selection(*args)
        assert revised.clips[0].beats[2].mode=='narration'
        assert revised.clips[0].beats[3:]==plan.clips[0].beats[3:]


def test_four_dimension_approval_cannot_skip_new_selection_check():
    plan,_,_=fixture();clip=plan.clips[0]
    report={'policy':'connected-commentary-2','review_policy':NARRATIVE_REVIEW_POLICY,
        'clip_digest':digest(clip.model_dump()),'passed':True,'issues':[],
        'checks':[{'criterion':c,'passed':True,'reason':'旧四项通过'} for c in sorted(NARRATIVE_CRITERIA-{'selection'})]}
    with pytest.raises(ValueError,match='未完整通过'):require_commentary_review(report,clip)


def test_restoring_source_audio_does_not_reuse_selection_approval(tmp_path):
    from autoclip.automatic import retain_short_source_audio
    plan,lines,cfg=fixture();lines[3].text='生活过场闲聊'
    old=plan.clips[0]
    old_report={'policy':'connected-commentary-2','review_policy':NARRATIVE_REVIEW_POLICY,
        'clip_digest':digest(old.model_dump()),'passed':True,'issues':[],
        'checks':[{'criterion':c,'passed':True,'reason':'修改前通过'} for c in sorted(NARRATIVE_CRITERIA)]}
    revised=retain_short_source_audio(plan,'beat-3',lines,cfg,1300)
    assert revised is not None
    run=next(r for r in original_runs(revised.clips[0]) if 3 in r['beats'])
    assert run['beats']==[2,3,4] and run['review_focus'] and run['restored_beats']==[3]
    with pytest.raises(ValueError,match='不能交付'):require_commentary_review(old_report,revised.clips[0])


def test_pipeline_remeasures_reaudits_and_rechecks_selection_after_drop(tmp_path,monkeypatch):
    import autoclip.series as module
    plan,lines,cfg=fixture();cfg.repair_attempts=1
    measurements=[];audits=[];heard=[]
    def measure(video,path,transcript,cfg,root):
        current=read_json(path);measurements.append(current);write_json(root/'plan-timed.json',current)
        return {'issues':[],'beats':[{'output_seconds':36 if b['mode']=='narration' else b['end']-b['start']} for b in current['clips'][0]['beats']]}
    def audit(video,current,*args):audits.append(current.model_dump());return {'passed':True,'issues':[]}
    class Model:
        def json(self,system,prompt):
            audience=json.JSONDecoder().raw_decode(prompt.split('\naudience=',1)[1])[0];heard.append(audience)
            bad=next((r for r in audience if r['text']=='生活过场闲聊'),None)
            return {'checks':[{'criterion':c,'passed':c!='selection' or bad is None,'reason':'过场应删' if bad else '通过'} for c in sorted(NARRATIVE_CRITERIA)],
                'issues':[{'beat':bad['beat'],'quote':bad['text'],'problem':'过场无关筹粮','direction':'删掉，保留其后交锋','action':'drop'}] if bad else []}
    monkeypatch.setattr(module,'measure_sample_plan',measure);monkeypatch.setattr(module,'audit_plan',audit)
    monkeypatch.setattr(module,'duration',lambda _:1300)
    final,report=module.complete_long_plan(tmp_path/'source.mp4',plan,lines,{},tmp_path,cfg,Model())
    assert report['passed'] and len(final.clips[0].beats)==31
    assert len(measurements)==len(audits)==len(heard)==2
    assert read_json(tmp_path/'repair-budget.json')['used']=={'timing':0,'content':0,'selection':1}
    assert read_json(tmp_path/'pending-repair.json')['status']=='requires_full_measurement_and_review'
    require_commentary_review(report['narrative_review'],final.clips[0])
