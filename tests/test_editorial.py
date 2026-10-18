from pathlib import Path

import pytest
from pydantic import ValidationError

from autoclip.config import Settings
from autoclip.editorial import OralProposal, apply_oral_changes, polish_clip, prose_signals
from autoclip.models import Clip, Beat, Line
from autoclip.repair import RepairNeeded
from autoclip.util import read_json
from autoclip.budget import spoken_characters


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


def test_oral_length_feedback_identifies_all_failed_beats_and_limits():
    clip=fixture_clip();before=clip.model_dump()
    raw={'changes':[{'beat':i,'text':'字'*100,'reason':'测试超长修订'} for i in (1,3)]}
    with pytest.raises(ValueError) as caught:
        apply_oral_changes(clip,OralProposal.model_validate(raw))
    message=str(caught.value)
    for i in (1,3):
        assert f'beat {i} 原稿{spoken_characters(clip.beats[i].text)}字，新稿100字，须' in message
    assert clip.model_dump()==before


def test_punctuation_does_not_consume_spoken_character_budget():
    clip=fixture_clip();clip.beats[1].text='有人提问对方回应事情继续'
    text='有人提问，对方回应；事情继续。'
    raw={'changes':[{'beat':1,'text':text,'reason':'只加正常停顿'}]}
    assert len(text)>int(len(clip.beats[1].text)*1.2)
    revised=apply_oral_changes(clip,OralProposal.model_validate(raw))
    assert revised.beats[1].text==text


def short_oral_fixture():
    clip=fixture_clip()
    clip.beats[1].text='有人提出问题，对方随后回应。'
    long='这段口播明显超出原定画面，需要仅修复这一处而保留其他段落。'
    return clip, {'changes':[{'beat':1,'text':long,'reason':'修顺口播'},
                            {'beat':3,'text':clip.beats[3].text,'reason':'原稿可保留'}]}


def test_length_repair_selects_actual_fit_preserves_valid_edits_and_sources(tmp_path):
    from autoclip.editorial import repair_oral_lengths
    clip,raw=short_oral_fixture();before=clip.model_dump()
    proposal=OralProposal.model_validate(raw)
    class Model:
        def json(self,*args):
            return {'candidates':[raw['changes'][0],{'beat':1,'text':'有人提问，对方回应。','reason':'精简过渡'}]}
    fixed=repair_oral_lengths(clip,proposal,[],tmp_path,Settings(),Model(),0)
    assert fixed.changes[0].text=='有人提问，对方回应。'
    assert fixed.changes[1]==proposal.changes[1]
    assert proposal.model_dump()==raw and clip.model_dump()==before
    record=read_json(next(tmp_path.glob('length-*.json')))
    assert record['counts'][0]>record['max_chars']
    assert record['selected']['text']==fixed.changes[0].text
    assert apply_oral_changes(clip,fixed).beats[1].evidence_ids==clip.beats[1].evidence_ids


def test_length_repair_exhaustion_changes_feedback_without_truncating_text(tmp_path):
    from autoclip.editorial import repair_oral_lengths
    clip,raw=short_oral_fixture();prompts=[]
    class Model:
        def json(self,system,prompt):
            prompts.append(prompt);return {'candidates':[raw['changes'][0]]}
    with pytest.raises(ValueError,match='达到2次上限'):
        repair_oral_lengths(clip,OralProposal.model_validate(raw),[],tmp_path,
                            Settings(repair_attempts=2),Model(),0)
    assert len(prompts)==2 and prompts[0]!=prompts[1]
    assert '候选实际字数' in prompts[1]
    assert all('selected' not in read_json(p) for p in tmp_path.glob('length-*.json'))


@pytest.mark.parametrize('case',['original','duplicate','new_shot'])
def test_invalid_targets_cannot_request_local_length_repairs(tmp_path,case):
    from autoclip.editorial import repair_oral_lengths
    clip,raw=short_oral_fixture()
    if case=='original':raw['changes'][0]['beat']=0
    if case=='duplicate':raw['changes'].append(raw['changes'][0])
    if case=='new_shot':raw['changes'][0]['beat']=99
    class Model:
        def json(self,*args):pytest.fail('无效目标不能请求修稿')
    with pytest.raises(ValueError):
        repair_oral_lengths(clip,OralProposal.model_validate(raw),[],tmp_path,Settings(),Model(),0)


def test_length_repaired_text_still_requires_independent_factual_approval(tmp_path):
    clip,raw=short_oral_fixture();events=[]
    class Model:
        def json(self,system,prompt):
            if '口播长度局部' in system:
                events.append('length')
                return {'candidates':[{'beat':1,'text':'有人提问，对方回应。','reason':'精简过渡'}]}
            if '独立事实核对员' in system:
                events.append('facts')
                assert ('有人提出问题，对方随后回应。' if '未采用任何精修建议' in prompt else '有人提问，对方回应。') in prompt
                return {'passed':False,'issues':[{'beat':1,'message':'声称对方已回应，原片仍在提问'}]}
            events.append('proposal');return {'changes':[raw['changes'][0]]}
    with pytest.raises(RepairNeeded,match='事实审核失败'):
        polish_clip(clip,[],tmp_path,Settings(),Model())
    assert events==['proposal','length','facts']*2+['facts']
    assert not list(tmp_path.rglob('result.json'))


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
            return proposal() if len(calls)%2==1 else {'passed':False,'issues':[{'beat':1,'message':'口播把借款动机写成已证实'}]}
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
    assert not list(tmp_path.rglob('original-review.json'))


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


def test_oral_review_does_not_audit_private_edit_instructions_as_spoken_claims(tmp_path):
    clip=fixture_clip();clip.beats[1].reason='内部尚未核实的因果备注'
    events=[]
    class Model:
        def json(self,system,prompt):
            assert '内部尚未核实的因果备注' not in prompt
            events.append(system)
            if '独立事实核对员' in system:
                assert '候选改写的内部理由' not in prompt
                return {'passed':True,'issues':[]}
            raw=proposal();raw['changes'][0]['reason']='候选改写的内部理由'
            return raw
    result=polish_clip(clip,[],tmp_path,Settings(),Model())
    assert len(events)==2 and result.beats[1].reason==clip.beats[1].reason
    assert read_json(next(tmp_path.rglob('result.json')))['changes'][0]['reason']=='候选改写的内部理由'


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
    def prepare_brief(*args,**kwargs):
        brief_calls.append(args)
        assert kwargs['notes'] == '本场问话者已根据连续问答核对'
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
                                               'characters_note':'本场问话者已根据连续问答核对',
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


def test_outline_local_repair_updates_feedback_after_bad_evidence(tmp_path,monkeypatch):
    import autoclip.outline as outline
    import autoclip.continuity as continuity
    monkeypatch.setattr(continuity,'load_continuity',lambda *a:{'signature':'prior','episodes':[]})
    slots=[{'id':i,'mode':'narration' if i in (1,2) else 'original','start':i*12,'end':(i+1)*12,
            'instruction':'依据当前字幕交代事情','evidence_ids':[i],'allowed_evidence_ids':[i],
            'target_chars':45} for i in range(4)]
    lines=[Line(id=i,start=i*12+1,end=i*12+10,text='当前剧情依据') for i in range(4)]
    data={'title':'事情经过','angle':'问答','hook':'事情怎样展开','narrative_mode':'story_first',
          'chapters':[{'title':'事情经过','beats':slots}]}
    monkeypatch.setattr(outline,'check_outline',lambda *a:(data,lines))
    monkeypatch.setattr(outline,'duration',lambda _:48)
    text='有人提出了问题，对方作了回应。'
    local_prompts=[]
    class Model:
        def json(self,system,prompt):
            if '局部修稿编辑' in system:
                local_prompts.append(prompt)
                return {'id':1,'text':text,'evidence_ids':[999] if len(local_prompts)==1 else [1]}
            return {'items':[{'id':i,'text':text,'evidence_ids':[999] if i==1 else [i]} for i in (1,2)]}
    cfg=Settings(target_seconds=100,min_seconds=30,max_seconds=180,opening='original')
    result=outline.compose_outline(tmp_path/'video.mp4',tmp_path/'transcript.json',tmp_path/'outline.json',
                                   tmp_path/'run',cfg,Model())
    assert len(local_prompts)==2 and local_prompts[0]!=local_prompts[1]
    assert '未允许证据[999]' in local_prompts[1]
    assert result.clips[0].beats[1].evidence_ids==[1]


def test_factually_unchanged_near_synonym_of_audit_language_cannot_pass(tmp_path):
    calls=[]
    class Model:
        def json(self,*args):
            calls.append(args)
            return proposal() if len(calls)%2==1 else {'passed':True,'issues':[],
                'oral_issues':[{'beat':1,'message':'改写只是把审稿用词换成近义词，仍不适合口播'}]}
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
                'oral_issues':[{'beat':3,'message':'未改段仍重复免责声明'}]}
    with pytest.raises(RepairNeeded,match='两轮仍未通过'):
        polish_clip(clip,[],tmp_path,Settings(),Model())
    assert len(calls)==5 and not list(tmp_path.rglob('result.json'))


def test_rejected_edits_can_keep_independently_reviewed_original_without_fact_certification(tmp_path):
    clip=fixture_clip();clip.beats[1].text=proposal()['changes'][0]['text'];before=clip.model_dump()
    class Model:
        def json(self,system,prompt):
            if '未采用任何精修建议' in prompt:
                return {'passed':True,'issues':[],'baseline_issues':['人物归属仍需完整画面审核']}
            if '独立事实核对员' in system:
                return {'passed':False,'issues':[{'beat':1,'message':'改稿新增了无据的动作'}]}
            raw=proposal();raw['changes'][0]['text']=raw['changes'][0]['text'].replace('借一两','借一两银子')
            return raw
    final=polish_clip(clip,[],tmp_path,Settings(),Model())
    assert final.model_dump()==before
    record=read_json(next(tmp_path.rglob('result.json')))
    assert record['retained_original_after_rejected_edits'] and record['changes']==[]
    assert record['baseline_issues']==['人物归属仍需完整画面审核']
    assert record['status']=='text_checked_not_timed_or_identity_certified'


@pytest.mark.parametrize('verdict',[{'passed':True,'issues':[{'beat':1,'message':'仍有新增错误'}]},
    {'passed':True,'issues':[],'oral_issues':[{'beat':3,'message':'原稿听感严重绕句'}]},
    {'passed':False,'issues':[]}, {'passed':'true','issues':[]}, {}])
def test_original_retention_requires_unambiguous_independent_review(tmp_path,verdict):
    clip=fixture_clip();clip.beats[1].text=proposal()['changes'][0]['text']
    class Model:
        def json(self,system,prompt):
            if '未采用任何精修建议' in prompt:return verdict
            if '独立事实核对员' in system:return {'passed':False,'issues':[{'beat':1,'message':'拒绝改稿'}]}
            return proposal()
    with pytest.raises(RepairNeeded):polish_clip(clip,[],tmp_path,Settings(),Model())
    assert not list(tmp_path.rglob('result.json'))


def test_fact_repair_focus_keeps_other_approved_edits(tmp_path):
    clip=fixture_clip();clip.beats[1].text=proposal()['changes'][0]['text']
    third='田大人收下银子，让陈大人先回去读书，现场问话就此结束。'
    proposals=[];reviews=[]
    class Model:
        def json(self,system,prompt):
            if '独立事实核对员' in system:
                reviews.append(prompt)
                return {'passed':False,'issues':[{'beat':1,'message':'这一段新增了无据的解释'}]} if len(reviews)==1 else {'passed':True,'issues':[]}
            proposals.append(prompt)
            first=proposal()['changes'][0]
            if len(proposals)==1:
                return {'changes':[first,{'beat':3,'text':third,'reason':'自然交代结果'}]}
            assert '本轮允许编号[1]' in prompt
            return {'changes':[first]}
    final=polish_clip(clip,[],tmp_path,Settings(),Model())
    assert len(proposals)==len(reviews)==2
    assert final.beats[3].text==third and final.beats[0]==clip.beats[0]
    assert third in reviews[-1]


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


@pytest.mark.parametrize('text',['字幕里说，这始终就没弄明白。','根据字幕，可以确定此事。',
                                 '镜头里有人应下差事。','声纹候选显示这就是六叔。',
                                 '侄儿始终就没弄明白。','这么多年，您当着朝廷的辅相。','把人带下去，朕就问罪他。',
                                 '小多子，你这碗水还浅着呢。'])
@pytest.mark.parametrize('frozen',[False,True])
def test_production_language_cannot_reach_render_even_with_polish_disabled(tmp_path,text,frozen):
    from autoclip.automatic import FrozenAutomaticScript
    from autoclip.render import render_clip
    clip=fixture_clip();clip.beats[1].text=text
    clip.beats[3].text='他收下银子，结束了问话。'
    with pytest.raises(RepairNeeded,match='制作或审查措辞'):
        render_clip(tmp_path/'missing.mp4',tmp_path,clip,0,Settings(oral_edit=False),
                    FrozenAutomaticScript() if frozen else object(),[])
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('text',['康熙下令核对账目。','这份证据让案情有了转机。','他让人把信上的字念出来。'])
def test_plot_actions_and_in_story_evidence_are_not_production_language(text):
    clip=fixture_clip();clip.beats[1].text=text
    assert prose_signals(clip)==[]


@pytest.mark.parametrize('text',['他答“行，我去”。','皇帝说：“朕就问罪他。”',
                                 '晚辈说，侄儿始终没弄明白。','他向六叔说，您当着辅相。',
                                 '皇帝反问：你死了，岂不更伤圣德？','来求差事的晚辈向长辈道谢。'])
def test_attributed_or_quoted_role_speech_is_allowed(text):
    clip=fixture_clip();clip.beats[1].text=text
    assert prose_signals(clip)==[]
