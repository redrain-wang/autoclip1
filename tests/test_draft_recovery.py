import copy
from pathlib import Path

import pytest

from autoclip.budget import story_budget
from autoclip.config import Settings
from autoclip.director import compile_plan, create_plan
from autoclip.draft import repair_draft
from autoclip.models import Line, Plan, validate_plan
from autoclip.repair import timing_report
from test_pipeline import fixture_plan


def test_padding_does_not_destroy_narration_evidence_or_cut_original_speech():
    lines,plan=fixture_plan()
    # The first cited words begin at 9.95; the next utterance starts at 10.
    # Original-audio preroll must not erase the first shot's proof.
    lines[0]=Line(id=0,start=9.95,end=10,text='当前镜头事实')
    raw=plan.model_dump()
    raw['clips'][0]['beats'][0].update(evidence_ids=[0],text='事实')
    compiled=compile_plan(raw,lines,bounds=(0,40),fit_narration=False)
    first,second=compiled.clips[0].beats[:2]
    assert first.end==second.start==10
    assert second.start<=lines[1].start
    validate_plan(compiled,lines,40,1,45,check_duration=False)


def test_estimate_over_by_sixteen_seconds_reaches_actual_measurement_but_cannot_pass_long_audio():
    lines,plan=fixture_plan()
    for beat in plan.clips[0].beats:
        if beat.mode=='narration':beat.text='字'*531
    validate_plan(plan,lines,40,1,190,120,240)
    report=timing_report(plan.clips[0],{0:118,2:118},Settings(target_seconds=190,min_seconds=120,max_seconds=240))
    assert any(i['code']=='duration_long' for i in report['issues'])
    for beat in plan.clips[0].beats:
        if beat.mode=='narration':beat.text='字'*700
    with pytest.raises(ValueError,match=r'120–240秒，超出上限91.11秒'):
        validate_plan(plan,lines,40,1,190,120,240)


def test_local_patch_preserves_every_shot_outside_window(tmp_path):
    lines,plan=fixture_plan()
    raw=plan.model_dump();before=copy.deepcopy(raw)
    raw['clips'][0]['beats'][0]['evidence_ids']=[3]
    class Model:
        def json(self,system,prompt):
            return {'beats':before['clips'][0]['beats'][:2]}
    revised=repair_draft(raw,{'issues':[{'message':'clips[0].beats[0] 没有落在画面区间内的字幕证据'}]},
                         lines,(0,40),Settings(),Model(),tmp_path,'test',0)
    assert revised['clips'][0]['beats'][2:]==before['clips'][0]['beats'][2:]
    assert raw['clips'][0]['beats'][0]['evidence_ids']==[3]
    validate_plan(Plan.model_validate(revised),lines,40,1,45)


def test_measured_production_draft_is_cached_without_becoming_semantic_approval(tmp_path,monkeypatch):
    lines,plan=fixture_plan()
    class Writer:
        calls=0
        def json(self,*args):self.calls+=1;return plan.model_dump()
    writer=Writer()
    cfg=Settings(clips=1,vision=False,oral_edit=False,target_seconds=45)
    for _ in range(2):
        create_plan(Path('source.mp4'),tmp_path,lines,40,[],cfg,writer,[],bounds=(0,40),measured_review=True)
    assert writer.calls==1
    from autoclip import draft
    monkeypatch.setattr(draft,'VERSION','next-repair-version')
    create_plan(Path('source.mp4'),tmp_path,lines,40,[],cfg,writer,[],bounds=(0,40),measured_review=True)
    assert writer.calls==2
    assert not (tmp_path/'automatic-review.json').exists()
    assert not (tmp_path/'plan-approved.json').exists()


def test_overloaded_original_draft_does_not_set_new_narration_budget_to_zero():
    cfg=Settings(target_seconds=190,min_seconds=120,max_seconds=240)
    budget=story_budget([], (0,300), cfg, {'clip':{'beats':[{'mode':'original','start':0,'end':240}]}})
    assert budget['original_seconds']==240
    assert budget['planning_reference']['narration_characters']>0
    assert budget['original_excess_above_target_seconds']>0


def test_future_citation_repair_cannot_replace_original_dialogue_or_move_shots(tmp_path):
    lines,plan=fixture_plan()
    raw=plan.model_dump();raw['clips'][0]['beats'][0]['evidence_ids']=[0,3]
    class Model:
        def json(self,*args):return {'changes':[{'beat':0,'text':'当前画面中的事实','evidence_ids':[0]}]}
    revised=repair_draft(raw,{'issues':[{'code':'evidence','message':'clips[0].beats[0] 引用了未来对白'}]},
                         lines,(0,40),Settings(),Model(),tmp_path,'future',0)
    assert revised['clips'][0]['beats'][1:]==raw['clips'][0]['beats'][1:]
    assert revised['clips'][0]['beats'][0]['start']==0
    assert revised['clips'][0]['beats'][0]['end']==10
    assert revised['clips'][0]['beats'][0]['evidence_ids']==[0]


def test_resume_prefers_intact_failed_draft_to_broken_latest_rewrite(tmp_path):
    from autoclip.draft import resume_report
    from autoclip.util import write_json
    lines,plan=fixture_plan()
    good={'clip':plan.clips[0].model_dump(),'issues':[]}
    write_json(tmp_path/'attempt-history/old.json',{'repair':good})
    broken=copy.deepcopy(good);broken['clip']['beats'][2]['start']=1
    write_json(tmp_path/'attempt-0.json',{'repair':broken})
    chosen=resume_report(tmp_path,lines,40,Settings(target_seconds=45),(0,40))
    assert chosen==good


def test_invalid_future_patch_gets_bounded_feedback_then_can_recover(tmp_path):
    lines,plan=fixture_plan();raw=plan.model_dump()
    raw['clips'][0]['beats'][0]['evidence_ids']=[0,3]
    class Model:
        calls=0
        def json(self,system,prompt):
            self.calls+=1
            if self.calls==2:assert '只能使用' in prompt
            return {'changes':[{'beat':0,'text':'依据当前对白说明处境','evidence_ids':[3 if self.calls==1 else 0]}]}
    model=Model()
    fixed=repair_draft(raw,{'issues':[{'code':'evidence','message':'clips[0].beats[0] 引用了未来对白'}]},
                       lines,(0,40),Settings(repair_attempts=1),model,tmp_path,'bounded',0)
    assert model.calls==2
    assert fixed['clips'][0]['beats'][0]['evidence_ids']==[0]
    assert fixed['clips'][0]['beats'][1:]==raw['clips'][0]['beats'][1:]


def test_unit_repair_is_not_blocked_by_untouched_unit_pending_full_audit(tmp_path):
    from autoclip.automatic import repair_plan
    lines,plan=fixture_plan()
    plan.clips[0].beats[2].evidence_ids=[2,3]
    class Model:
        def json(self,*args):return {'changes':[{'id':'beat-0','text':'当前事实支持必要承接','evidence_ids':[0]}]}
    fixed=repair_plan(plan,[{'id':'beat-0','message':'修复当前承接'}],lines,{},tmp_path,
                      Settings(target_seconds=45),Model(),0,source_seconds=40)
    assert fixed.clips[0].beats[0].text=='当前事实支持必要承接'
    assert fixed.clips[0].beats[1:]==plan.clips[0].beats[1:]
    assert not (tmp_path/'plan-approved.json').exists()


def test_opening_repair_does_not_rewrite_other_failed_narrations(tmp_path):
    from test_opening import opening_plan
    plan,lines=opening_plan();raw=plan.model_dump()
    old=copy.deepcopy(raw['clips'][0]['beats'])
    raw['clips'][0]['beats'][0]['end']=5
    raw['clips'][0]['beats'].insert(1,{**old[0],'start':6,'end':12})
    class Model:
        def json(self,system,prompt):
            assert '"replace_indices": [0, 2]' in prompt
            assert '"source_window": [0, 25.0]' in prompt
            return {'beats':old[:2]}
    report={'issues':[{'code':'evidence','message':'clips[0] 原声开场必须连续\nclips[0].beats[5] 引用未来对白'}]}
    fixed=repair_draft(raw,report,lines,(0,140),Settings(),Model(),tmp_path,'opening',0)
    assert fixed['clips'][0]['beats']==old


def test_partial_draft_keeps_valid_local_change_when_later_unit_fails(tmp_path):
    from autoclip.repair import RepairNeeded
    lines,plan=fixture_plan();raw=plan.model_dump()
    raw['clips'][0]['beats'][0]['evidence_ids']=[0,3]
    raw['clips'][0]['beats'][2]['evidence_ids']=[2,3]
    class Model:
        def json(self,system,prompt):
            target=0 if '"current_target": 0' in prompt else 2
            return {'changes':[{'beat':target,'text':'已经发生的事实','evidence_ids':[0 if target==0 else 3]}]}
    report={'issues':[{'code':'evidence','message':'clips[0].beats[0] 未来对白\nclips[0].beats[2] 未来对白'}]}
    with pytest.raises(RepairNeeded) as error:
        repair_draft(raw,report,lines,(0,40),Settings(repair_attempts=0),Model(),tmp_path,'partial',0)
    partial=error.value.report['clip']
    assert partial['beats'][0]['text']=='已经发生的事实'
    assert partial['beats'][0]['evidence_ids']==[0]
    assert partial['beats'][1:]==raw['clips'][0]['beats'][1:]
    assert error.value.report['issues'][0]['beat']==2
    assert not (tmp_path/'plan-approved.json').exists()


def test_measured_overrun_enforces_real_shortening_before_next_tts_round(tmp_path):
    from autoclip.automatic import repair_plan
    lines,plan=fixture_plan()
    class Model:
        calls=0
        def json(self,system,prompt):
            self.calls+=1
            assert '有效字数必须不超过' in prompt
            return {'changes':[{'id':'beat-0','text':plan.clips[0].beats[0].text+'啊' if self.calls==1 else '先交代已经发生的事情','evidence_ids':[0]}]}
    model=Model()
    issue={'id':'beat-0','message':'口播超过画面余量','measurement':{'voice_seconds':20}}
    revised=repair_plan(plan,[issue],lines,{},tmp_path,Settings(target_seconds=45),model,0,source_seconds=40)
    assert model.calls==2
    assert revised.clips[0].beats[0].text=='先交代已经发生的事情'
    assert revised.clips[0].beats[1:]==plan.clips[0].beats[1:]


def test_incomplete_model_beat_yields_validation_not_episode_attribute_crash():
    lines,plan=fixture_plan();raw=plan.model_dump()
    del raw['clips'][0]['beats'][0]['evidence_ids']
    with pytest.raises(ValueError,match='evidence_ids'):
        compile_plan(raw,lines)


def test_invalid_full_timeline_response_cannot_poison_small_opening_patch(tmp_path):
    from test_opening import opening_plan
    plan,lines=opening_plan();raw=plan.model_dump();old=copy.deepcopy(raw['clips'][0]['beats'])
    raw['clips'][0]['beats'][0]['end']=5
    raw['clips'][0]['beats'].insert(1,{**old[0],'start':6,'end':12})
    class Model:
        calls=0
        def json(self,system,prompt):
            self.calls+=1
            assert 'beat_schema' in prompt
            return {'beats':old if self.calls==1 else old[:2]}
    model=Model()
    fixed=repair_draft(raw,{'issues':[{'message':'原声开场必须连续'}]},lines,(0,140),
                       Settings(opening='original',repair_attempts=1),model,tmp_path,'window',0)
    assert model.calls==2
    assert fixed['clips'][0]['beats']==old


def test_cli_returns_failure_if_processing_finishes_without_any_deliverable(tmp_path,monkeypatch):
    from autoclip import production
    from autoclip.branding import BrandSettings
    from autoclip.cli import app
    from test_short_voices import reference
    from typer.testing import CliRunner
    video,library,_=reference(tmp_path)
    cfg=Settings(series='测试剧',output=tmp_path/'project')
    monkeypatch.setattr(Settings,'load',lambda _:cfg.model_copy(deep=True))
    monkeypatch.setattr(Settings,'api_key',lambda _:'test')
    monkeypatch.setattr('autoclip.cli.shutil.which',lambda _:'tool')
    monkeypatch.setattr(BrandSettings,'load',lambda _:BrandSettings(enabled=False,watermark=tmp_path/'none'))
    monkeypatch.setattr(production,'produce_episode',lambda *a,**kw:{'status':'no_delivery','clips':0,'rejected':[],'failures':[]})
    result=CliRunner().invoke(app,['produce',str(video),'--library',str(library)])
    assert result.exit_code==1
    assert '本批未生成成片' in result.output


def test_opening_patch_cannot_reach_back_to_unselected_earlier_scene(tmp_path):
    from test_opening import opening_plan
    plan,lines=opening_plan()
    for line in lines:line.start+=100;line.end+=100
    for beat in plan.clips[0].beats:beat.start+=100;beat.end+=100
    raw=plan.model_dump();old=copy.deepcopy(raw['clips'][0]['beats'])
    raw['clips'][0]['beats'][0]['end']=105
    raw['clips'][0]['beats'].insert(1,{**old[0],'start':106,'end':112})
    class Model:
        def json(self,system,prompt):
            assert '"source_window": [100.0, 125.0]' in prompt
            return {'beats':old[:2]}
    fixed=repair_draft(raw,{'issues':[{'message':'原声开场必须连续'}]},lines,(0,240),
                       Settings(opening='original'),Model(),tmp_path,'bounded-start',0)
    assert fixed['clips'][0]['beats']==old


def test_too_short_opening_window_includes_next_complete_shot_before_repair(tmp_path):
    from autoclip.models import Beat,Clip
    spans=[(0,1),(1,10),(10,25),(25,40),(40,55),(55,70)]
    lines=[Line(id=i,start=a,end=b,text='已有对白') for i,(a,b) in enumerate(spans)]
    clip=Clip(title='完整故事',angle='前因后果',hook='会怎么回应',beats=[
        Beat(mode='original' if i%2==0 else 'narration',start=a,end=b,
             text='' if i%2==0 else '已有事实的必要承接',evidence_ids=[i],reason='依据') for i,(a,b) in enumerate(spans)])
    raw=Plan(episode_summary='',genre='',style='',clips=[clip]).model_dump()
    class Model:
        def json(self,system,prompt):
            assert '"replace_indices": [0, 2]' in prompt
            assert '"source_window": [0, 25.0]' in prompt
            return {'beats':[dict(mode='original',start=0,end=10,text='',evidence_ids=[0,1],reason='完整原声'),
                             dict(mode='narration',start=10,end=25,text='已有事实的必要承接',evidence_ids=[2],reason='当前画面') ]}
    fixed=repair_draft(raw,{'issues':[{'message':'原声开场实长1秒'}]},lines,(0,70),
                       Settings(opening='original',target_seconds=45),Model(),tmp_path,'room',0)
    validate_plan(Plan.model_validate(fixed),lines,70,1,45,opening='original')
    assert fixed['clips'][0]['beats'][2:]==raw['clips'][0]['beats'][3:]
