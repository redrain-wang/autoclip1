from pathlib import Path

import pytest
from pydantic import ValidationError

from autoclip.config import Settings
from autoclip.editorial import OralProposal, apply_oral_changes, polish_clip, prose_signals
from autoclip.models import Clip, Beat, Line
from autoclip.repair import RepairNeeded
from autoclip.util import read_json


def fixture_clip():
    return Clip(title='一两银子为何还要问',angle='田的指责与还款',hook='还清为何追问',beats=[
        Beat(mode='original',start=0,end=16,evidence_ids=[0],reason='开场'),
        Beat(mode='narration',start=16,end=36,evidence_ids=[1],reason='说明指责',
             text='这是控诉方的推论，不等于客观定论。田大人说他借一两，是想合群又给自己留退路。'),
        Beat(mode='original',start=36,end=56,evidence_ids=[2],reason='当事人回答'),
        Beat(mode='narration',start=56,end=76,evidence_ids=[3],reason='未知结果',
             text='田大人收下银子，让陈大人回去读书，这结束了现场的问话。')])


def proposal():
    return {'changes':[{'beat':1,'text':'在田大人的解释里，借一两既能合群，又能给自己留退路。他追问的，是这笔借款的用意。','reason':'自然交代指责来源'}]}


def test_oral_edit_preserves_originals_citations_timing_and_does_not_mutate_input():
    clip=fixture_clip();before=clip.model_dump()
    after=apply_oral_changes(clip,OralProposal.model_validate(proposal()))
    assert clip.model_dump()==before
    assert not prose_signals(after) and prose_signals(clip)
    for i,(a,b) in enumerate(zip(clip.beats,after.beats)):
        a,b=a.model_dump(),b.model_dump()
        if i==1:a.pop('text');b.pop('text')
        assert a==b
    assert after.title==clip.title and after.hook==clip.hook and after.angle==clip.angle


@pytest.mark.parametrize('case',['original','duplicate','new_shot','new_field'])
def test_oral_edit_cannot_change_source_or_invent_editable_structure(case):
    raw=proposal()
    if case=='original':raw['changes'][0]['beat']=0
    if case=='duplicate':raw['changes'].append(raw['changes'][0].copy())
    if case=='new_shot':raw['changes'][0]['beat']=20
    if case=='new_field':raw['changes'][0]['evidence_ids']=[999]
    with pytest.raises((ValueError,ValidationError)):
        apply_oral_changes(fixture_clip(),OralProposal.model_validate(raw))


def test_factual_check_can_reject_smoother_but_unfaithful_text(tmp_path):
    clip=fixture_clip();calls=[]
    class Model:
        def json(self,system,prompt):
            calls.append(system)
            return proposal() if len(calls)%2==1 else {'passed':False,'issues':['口播把借款动机写成已证实']}
    with pytest.raises(RepairNeeded,match='事实审核失败'):
        polish_clip(clip,[Line(id=1,start=18,end=20,text='依我看来')],tmp_path,Settings(),Model())
    assert len(calls)==4 and prose_signals(clip)
    assert not list(tmp_path.rglob('result.json')) and not list(tmp_path.rglob('*.mp4'))


def test_repeated_clinical_proposal_stops_after_two_attempts(tmp_path):
    calls=[]
    class Model:
        def json(self,*args):calls.append(args);return {'changes':[]}
    with pytest.raises(RepairNeeded,match='精修或文案事实审核失败'):
        polish_clip(fixture_clip(),[],tmp_path,Settings(),Model())
    assert len(calls)==2 and not list(tmp_path.rglob('result.json'))


def test_success_keeps_attribution_and_unknowns_instead_of_stripping_hedges(tmp_path):
    clip=fixture_clip();clip.beats[3].text='这笔钱后来是否到账，眼下还不知道；不能把答应还钱当成已经还清。'
    calls=[]
    class Model:
        def json(self,*args):
            calls.append(args)
            return proposal() if len(calls)==1 else {'passed':True,'issues':[]}
    after=polish_clip(clip,[Line(id=1,start=18,end=20,text='依我看来')],tmp_path,Settings(),Model())
    assert after.beats[1].text.startswith('在田大人的解释里')
    assert after.beats[3].text==clip.beats[3].text
    record=read_json(next(tmp_path.rglob('result.json')))
    assert record['before_signals'] and not record['after_signals']
    assert record['status']=='text_checked_not_timed_or_identity_certified'


def test_production_editor_is_enabled_but_rendering_frozen_script_is_not_rewritten(tmp_path):
    from autoclip.sample import FrozenScript
    from autoclip.render import render_clip
    cfg=Settings.load(Path(__file__).parents[1]/'configs.production.toml')
    assert cfg.oral_edit and FrozenScript().frozen_script
    # Stop before opening the source or calling any service on unpolished prose.
    with pytest.raises(RepairNeeded,match='审查措辞'):
        render_clip(tmp_path/'missing.mp4',tmp_path,fixture_clip(),0,cfg,object(),[])
    assert not list(tmp_path.iterdir())


def test_short_director_polishes_before_final_identity_check(tmp_path,monkeypatch):
    import autoclip.director as director
    import autoclip.identity as identity
    clip=fixture_clip();clip.beats[1].text='田大人的问话指向借款原因，这与银子是否还清，是需要分开看待的两个问题。'
    lines=[Line(id=i,start=b.start+.5,end=b.end-.5,text='原片依据') for i,b in enumerate(clip.beats)]
    raw={'episode_summary':'例子','genre':'历史','style':'分析','clips':[clip.model_dump()]}
    events=[]
    class Model:
        def json(self,*args):
            events.append('model')
            return raw if len(events)==1 else {'passed':True,'issues':[]}
    def edit(c,*args):
        events.append('oral_edit');c=c.model_copy(deep=True)
        c.beats[1].text='在田大人的解释里，追问的是借款用意，银子还清只是问话的一部分。'
        return c
    def audit(video,root,c,*args):
        events.append('identity');assert c.beats[1].text.startswith('在田大人的解释里')
    monkeypatch.setattr(director,'polish_clip',edit);monkeypatch.setattr(identity,'enforce_identity',audit)
    cfg=Settings(clips=1,vision=False,target_seconds=100,min_seconds=30,max_seconds=180,opening='original',oral_edit=True)
    director.create_plan(tmp_path/'video.mp4',tmp_path,lines,100,[],cfg,Model(),[])
    assert events==['model','oral_edit','model','identity']


@pytest.mark.parametrize('char_budget',[45,90])
@pytest.mark.parametrize('use_brief',[False,True])
def test_outline_writer_runs_oral_editor_before_saving_plan(tmp_path,monkeypatch,char_budget,use_brief):
    import autoclip.outline as outline
    import autoclip.continuity as continuity
    import autoclip.conflict as conflict
    brief_calls=[]
    def prepare_brief(*args):
        brief_calls.append(args)
        return {'question':'承诺之后有没有实际归还', 'ending':'本段止于答应还款'}
    monkeypatch.setattr(conflict,'prepare_conflict_brief',prepare_brief)
    monkeypatch.setattr(continuity,'load_continuity',lambda *a: {'signature':'prior-episode', 'episodes':[
        {'episode':3,'events':[], 'dialogue':[{'id':55,'start':20,'end':24,'text':'前集曾答应归还银子'}]}]})
    from autoclip.util import write_json,source_identity
    video=tmp_path/'video.mp4';video.write_bytes(b'fixture')
    write_json(tmp_path/'source.json',source_identity(video))
    slots=[{'id':0,'mode':'original','start':0,'end':16,'evidence_ids':[0],'allowed_evidence_ids':[0],'instruction':'完整开场'}]
    lines=[Line(id=0,start=1,end=15,text='原声')]
    text='借款人的回答解释了他为什么愿意拖延，但执行者还要考虑这会如何影响其他人对同一条规矩的理解。'
    for i in range(1,5):
        start=16+(i-1)*20
        slots.append({'id':i,'mode':'narration','start':start,'end':start+20,'evidence_ids':[i],
                      'allowed_evidence_ids':[i],'instruction':'说明选择代价','target_chars':char_budget})
        lines.append(Line(id=i,start=start+1,end=start+5,text='剧情依据'))
    transcript=tmp_path/'transcript.json';write_json(transcript,[s.model_dump() for s in lines])
    path=tmp_path/'outline.json';write_json(path,{'source':source_identity(video),'title':'追款选择','angle':'选择代价','hook':'为何难办',
                                               'chapters':[{'title':'第一章','beats':slots}]})
    class Model:
        def json(self,*args):
            assert '前集曾答应归还银子' in args[1]
            assert ('承诺之后有没有实际归还' in args[1]) == use_brief
            return {'items':[{'id':i,'text':text,'evidence_ids':[i]} for i in range(1,5)]}
    seen=[]
    def polish(clip,*args):
        seen.append(clip.model_dump());clip=clip.model_copy(deep=True)
        clip.beats[1].text='在问话者的解释里，这个借款理由只说清了个人处境，却没有回答同一条还款要求为什么要给他例外。'
        return clip
    monkeypatch.setattr(outline,'polish_clip',polish);monkeypatch.setattr(outline,'duration',lambda p:100)
    cfg=Settings(target_seconds=100,min_seconds=60,max_seconds=180,opening='original',oral_edit=True,conflict_brief=use_brief)
    result=outline.compose_outline(video,transcript,path,tmp_path/'run',cfg,Model())
    saved=read_json(tmp_path/'run/plan.json')
    assert len(seen)==1 and result.clips[0].beats[1].text.startswith('在问话者的解释里')
    assert saved==result.model_dump()
    assert len(brief_calls)==int(use_brief)
    assert saved['clips'][0]['beats'][0]==seen[0]['beats'][0]
    assert read_json(tmp_path/'run/draft-plan.json')['clips'][0]['beats'][1]['text']==text


def test_existing_doubts_are_retained_for_full_review_not_marked_resolved(tmp_path):
    calls=[]
    class Model:
        def json(self,*args):
            calls.append(args)
            return proposal() if len(calls)==1 else {'passed':True,'issues':[],
                'baseline_issues':['旧稿借款用途仍需完整内容核对']}
    polish_clip(fixture_clip(),[],tmp_path,Settings(),Model())
    result=read_json(next(tmp_path.rglob('result.json')))
    assert result['baseline_issues']==['旧稿借款用途仍需完整内容核对']
    assert result['status']=='text_checked_not_timed_or_identity_certified'


def test_factually_unchanged_near_synonym_of_audit_language_cannot_pass(tmp_path):
    calls=[]
    class Model:
        def json(self,*args):
            calls.append(args)
            return proposal() if len(calls)%2==1 else {'passed':True,'issues':[],
                'oral_issues':['改写只是把审稿用词换成近义词，仍不适合口播']}
    with pytest.raises(RepairNeeded,match='两轮仍未通过'):
        polish_clip(fixture_clip(),[],tmp_path,Settings(),Model())
    assert len(calls)==4 and not list(tmp_path.rglob('result.json'))


def test_no_change_proposal_still_requires_whole_script_listening_review(tmp_path):
    clip=fixture_clip();clip.beats[1].text=proposal()['changes'][0]['text']
    calls=[]
    class Model:
        def json(self,*args):
            calls.append(args)
            return {'changes':[]} if len(calls)%2==1 else {'passed':True,'issues':[],
                'oral_issues':['beat 3未改段仍重复免责声明']}
    with pytest.raises(RepairNeeded,match='两轮仍未通过'):
        polish_clip(clip,[],tmp_path,Settings(),Model())
    assert len(calls)==4 and not list(tmp_path.rglob('result.json'))


def test_only_empty_metadata_placeholder_is_tolerated_source_fields_stay_forbidden():
    from autoclip.editorial import oral_proposal
    raw=proposal();raw['changes'][0]['reason_placeholder']=''
    assert oral_proposal(raw).changes[0].beat==1
    assert 'reason_placeholder' in raw['changes'][0]
    raw['changes'][0]['evidence_ids']=[999]
    with pytest.raises(ValidationError):oral_proposal(raw)


def test_optional_listening_suggestions_do_not_reject_a_clear_script(tmp_path):
    clip=fixture_clip();clip.beats[1].text=proposal()['changes'][0]['text']
    calls=[]
    class Model:
        def json(self,*args):
            calls.append(args)
            return {'changes':[]} if len(calls)==1 else {'passed':True,'issues':[],
                'oral_suggestions':['结尾可以略微压缩']}
    after=polish_clip(clip,[],tmp_path,Settings(),Model())
    assert len(calls)==2 and after.model_dump()==clip.model_dump()
    assert read_json(next(tmp_path.rglob('result.json')))['oral_suggestions']==['结尾可以略微压缩']
