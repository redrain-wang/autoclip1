from pathlib import Path
import pytest
from autoclip.config import Settings
from autoclip.models import Line
from autoclip.planning import Structure, compile_outline, validate_structure


def fixture(tmp_path):
    video=tmp_path/'source.mp4';video.write_bytes(b'fixture')
    cfg=Settings(duration_profile='long',opening='original_teaser',target_seconds=300,min_seconds=30,max_seconds=600)
    structure=Structure(title='一件案子的责任',angle='选择',hook='为何如此',opening_start=100,opening_end=120,
        opening_reason='连续高光',chapters=[{'title':'第一章','start':0,'end':80,'purpose':'完整冲突'}])
    lines=[Line(id=i,start=i*20+1,end=i*20+15,text='当前对白') for i in range(4)]
    lines.append(Line(id=10,start=100,end=119,text='已前置高光'))
    slots=[{'mode':'narration' if i%2==0 else 'original','start':i*20,'end':i*20+20,
            'instruction':'解释选择' if i%2==0 else '完整对白','evidence_ids':[i],
            'target_chars':90 if i%2==0 else 0} for i in range(4)]
    return video,structure,[{'beats':slots}],lines,140,cfg


def test_automatic_outline_uses_existing_timeline_rules_and_only_first_bridge_can_borrow_teaser(tmp_path):
    args=fixture(tmp_path);validate_structure(args[1],0,140,args[-1])
    result=compile_outline(*args)
    beats=result['chapters'][0]['beats']
    assert [b['id'] for b in beats]==list(range(5))
    assert 10 in beats[1]['allowed_evidence_ids']
    assert 10 not in beats[3]['allowed_evidence_ids']
    assert result['origin']=='automatic_outline_not_reviewed'


@pytest.mark.parametrize('problem',['future','repeat_teaser','out_of_chapter'])
def test_bad_automatic_shots_fail_before_writing_narration(tmp_path,problem):
    args=fixture(tmp_path);slot=args[2][0]['beats'][2]
    if problem=='future':slot['evidence_ids']=[10]
    elif problem=='repeat_teaser':
        args[1].chapters[0].end=140;slot.update(start=100,end=120,evidence_ids=[10])
    else:slot['end']=90
    with pytest.raises(ValueError):compile_outline(*args)


def test_structure_cannot_overlap_chapters_or_leave_source(tmp_path):
    args=fixture(tmp_path);structure=args[1]
    structure.chapters.append(structure.chapters[0].model_copy())
    with pytest.raises(ValueError,match='章节'):validate_structure(structure,0,140,args[-1])


def test_chronological_opening_feedback_identifies_actual_conflicting_times(tmp_path):
    _,structure,_,_,_,cfg=fixture(tmp_path)
    cfg.opening='original'
    with pytest.raises(ValueError) as caught:
        validate_structure(structure,0,140,cfg)
    error=str(caught.value)
    assert '100.00–120.00秒' in error and '从0.00秒开始' in error
    assert '其他章节保持不变' in error and '设为新的opening_end' in error


def test_short_original_utterances_merge_without_losing_intervening_dialogue():
    import copy
    from autoclip.planning import merge_continuous_originals
    raw={'beats':[{'mode':'original','start':10,'end':10.86,'instruction':'问话','evidence_ids':[1]},
                  {'mode':'original','start':12,'end':13.74,'instruction':'回答','evidence_ids':[2]}]}
    before=copy.deepcopy(raw)
    lines=[Line(id=1,start=10,end=10.86,text='问'),Line(id=3,start=11,end=11.5,text='中间真实对白'),
           Line(id=2,start=12,end=13.74,text='答')]
    merged=merge_continuous_originals(raw,lines)['beats']
    assert len(merged)==1 and merged[0]['start']==10 and merged[0]['end']==13.74
    assert set(merged[0]['evidence_ids'])=={1,2,3} and raw==before
    raw['beats'][1]['evidence_ids'].append(999)
    assert 999 in merge_continuous_originals(raw,lines)['beats'][0]['evidence_ids']


@pytest.mark.parametrize('second_start,second_end',[(12,15),(10,45)])
def test_original_merge_does_not_span_large_gaps_or_exceed_shot_limit(second_start,second_end):
    from autoclip.planning import merge_continuous_originals
    raw={'beats':[{'mode':'original','start':0,'end':10,'instruction':'原声','evidence_ids':[1]},
                  {'mode':'original','start':second_start,'end':second_end,'instruction':'原声','evidence_ids':[2]}]}
    assert len(merge_continuous_originals(raw,[])['beats'])==2


def test_reports_all_slot_errors_for_one_bounded_repair(tmp_path):
    args=fixture(tmp_path)
    args[2][0]['beats'][0]['end']=45
    args[2][0]['beats'][2]['evidence_ids']=[10]
    with pytest.raises(ValueError) as caught:
        compile_outline(*args)
    assert '须1–40秒' in str(caught.value)
    assert '却引用其后台词' in str(caught.value)
    assert '重叠' in str(caught.value)


@pytest.mark.parametrize('problem, message', [
    ('overlap', '不能重复或倒置'),
    ('repeat_teaser', '正文不能重复播放'),
    ('missing_direct_evidence', '不存在的证据'),
    ('unavailable_direct_evidence', '直接证据未包含'),
    ('empty', '非空章节'),
    ('unrelated_evidence', '画面区间内'),
    ('low_confidence', '低置信度'),
])
def test_supplied_outline_fails_before_continuity_or_model(tmp_path, monkeypatch, problem, message):
    from autoclip import outline as module, continuity
    from autoclip.util import write_json
    args = fixture(tmp_path)
    video, _, _, lines, seconds, cfg = args
    outline = compile_outline(*args)
    slots = outline['chapters'][0]['beats']
    if problem == 'overlap':
        slots[3]['start'] = 39
    elif problem == 'repeat_teaser':
        slots[-1].update(start=100, end=120)
    elif problem == 'missing_direct_evidence':
        slots[2]['evidence_ids'] = [999]
    elif problem == 'unavailable_direct_evidence':
        slots[2]['allowed_evidence_ids'] = []
    elif problem == 'empty':
        outline['chapters'] = []
    elif problem == 'unrelated_evidence':
        slots[-1]['evidence_ids'] = [0]
    elif problem == 'low_confidence':
        lines[1].confidence = .1
    write_json(tmp_path/'source.json', outline['source'])
    write_json(tmp_path/'transcript.json', [s.model_dump() for s in lines])
    write_json(tmp_path/'outline.json', outline)
    monkeypatch.setattr(module, 'duration', lambda _: seconds)
    monkeypatch.setattr(continuity, 'load_continuity', lambda *a: pytest.fail('检查失败前不能准备前情'))
    class Model:
        def json(self, *args):
            pytest.fail('无效时间线不能调用模型')
    with pytest.raises(ValueError, match=message):
        module.compose_outline(video, tmp_path/'transcript.json', tmp_path/'outline.json',
                               tmp_path/'run', cfg, Model())
    assert not (tmp_path/'run').exists()


def test_outline_precheck_allows_teaser_bridge_and_defers_unwritten_duration(tmp_path, monkeypatch):
    from autoclip import outline as module
    from autoclip.util import write_json
    args = fixture(tmp_path)
    video, _, _, lines, seconds, cfg = args
    outline = compile_outline(*args)
    outline['chapters'][0]['beats'][1]['evidence_ids'] = [10]
    write_json(tmp_path/'source.json', outline['source'])
    write_json(tmp_path/'transcript.json', [s.model_dump() for s in lines])
    write_json(tmp_path/'outline.json', outline)
    monkeypatch.setattr(module, 'duration', lambda _: seconds)
    cfg.min_seconds = 500  # An unwritten narration placeholder cannot establish duration.
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    checked, _ = module.check_outline(video, tmp_path/'transcript.json', tmp_path/'outline.json', cfg)
    assert checked == outline
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir()}


def test_story_first_supports_chronological_original_opening(tmp_path):
    from autoclip.planning import chapter_budgets, validate_chapter
    args=fixture(tmp_path)
    video,structure,drafts,lines,seconds,cfg=args
    cfg.opening='original';cfg.narrative_mode='story_first'
    structure.opening_start=0;structure.opening_end=20
    structure.chapters[0].start=20;structure.chapters[0].end=100
    for slot in drafts[0]['beats']:
        slot['start']+=20;slot['end']+=20;slot['evidence_ids']=[slot['evidence_ids'][0]+1]
    lines.append(Line(id=4,start=81,end=95,text='最后一句'))
    validate_structure(structure,0,140,cfg)
    budgets=chapter_budgets(structure,cfg)
    validate_chapter(drafts[0],structure.chapters[0],structure,lines,0,budgets[0],cfg)
    result=compile_outline(video,structure,drafts,lines,seconds,cfg)
    assert result['narrative_mode']=='story_first'
    assert result['chapters'][0]['beats'][0]['start']==0
    assert all(b['start']>=20 for b in result['chapters'][0]['beats'][1:])


def test_chapter_budget_and_bad_evidence_repaired_before_whole_compilation(tmp_path):
    from autoclip.planning import validate_chapter
    _,structure,drafts,lines,_,cfg=fixture(tmp_path)
    with pytest.raises(ValueError,match='超过预算'):
        validate_chapter(drafts[0],structure.chapters[0],structure,lines,0,10,cfg)
    drafts[0]['beats'][2]['evidence_ids']=[999]
    with pytest.raises(ValueError,match='无效或未来'):
        validate_chapter(drafts[0],structure.chapters[0],structure,lines,0,100,cfg)


def test_chapter_repair_reports_numeric_limits_and_zero_length_shot(tmp_path):
    from autoclip.planning import validate_chapter
    _,structure,drafts,lines,_,cfg=fixture(tmp_path)
    drafts[0]['beats'][-1].update(mode='narration',start=80,end=80,target_chars=30)
    with pytest.raises(ValueError) as caught:
        validate_chapter(drafts[0],structure.chapters[0],structure,lines,0,100,cfg)
    message=str(caught.value)
    assert '范围80.0–80.0' in message
    assert '零秒旁白' in message
    assert '字数预算30需适配画面0.00秒' in message


def test_only_failed_chapter_is_retried(tmp_path,monkeypatch):
    import autoclip.planning as module
    args=fixture(tmp_path)
    video,structure,_,lines,seconds,cfg=args
    structure.chapters=[module.Chapter(title='前半',start=0,end=40,purpose='起因'),
                        module.Chapter(title='后半',start=40,end=80,purpose='结果')]
    monkeypatch.setattr(module,'load_continuity',lambda *a:{'signature':'prior'})
    monkeypatch.setattr(module,'continuity_prompt',lambda *a:'')
    monkeypatch.setattr(module,'prepare_conflict_brief',lambda *a,**k:{})
    monkeypatch.setattr(module,'brief_context',lambda _: {})
    counts={1:0,2:0}
    class Model:
        def json(self,system,prompt):
            if '先定章节' in prompt:return structure.model_dump()
            chapter=1 if '只安排第1章' in prompt else 2
            counts[chapter]+=1
            beats=[{'mode':'narration' if i%2==0 else 'original','start':i*20,'end':(i+1)*20,
                    'instruction':'叙事','evidence_ids':[i],'target_chars':90 if i%2==0 else 0}
                   for i in range((chapter-1)*2,chapter*2)]
            if chapter==1 and counts[1]==1:beats[0]['start']=-1
            return {'beats':beats}
    result=module.plan_outline(video,tmp_path/'run',lines,seconds,cfg,Model())
    assert counts=={1:2,2:1}
    assert result['chapters'][1]['beats'][0]['start']==40


@pytest.mark.parametrize('opening,minimum', [('original',10),('original_teaser',18)])
def test_opening_error_reports_actual_duration_and_configured_range(tmp_path,opening,minimum):
    _,structure,_,_,_,cfg=fixture(tmp_path)
    cfg.opening=opening
    structure.opening_end=122.48
    with pytest.raises(ValueError) as caught:
        validate_structure(structure,0,140,cfg)
    assert '22.48秒' in str(caught.value)
    assert f'{minimum}–22秒' in str(caught.value)


def test_structure_repair_uses_configured_retry_limit(tmp_path,monkeypatch):
    import autoclip.planning as module
    video,structure,drafts,lines,seconds,cfg=fixture(tmp_path)
    monkeypatch.setattr(module,'load_continuity',lambda *a:{'signature':'prior'})
    monkeypatch.setattr(module,'continuity_prompt',lambda *a:'')
    monkeypatch.setattr(module,'prepare_conflict_brief',lambda *a,**k:{})
    monkeypatch.setattr(module,'brief_context',lambda _: {})
    prompts=[]
    class Model:
        def json(self,system,prompt):
            if '先定章节' in prompt:
                prompts.append(prompt)
                raw=structure.model_dump()
                if len(prompts)<3: raw['opening_end']=122.48
                return raw
            return drafts[0]
    result=module.plan_outline(video,tmp_path/'run',lines,seconds,cfg,Model())
    assert len(prompts)==3
    assert '22.48秒' in prompts[1]
    assert result['chapters'][0]['beats'][0]['end']==120


def test_production_story_budget_targets_twenty_minutes_instead_of_maximum():
    from autoclip.planning import chapter_budgets
    cfg=Settings(duration_profile='long',narrative_mode='story_first',target_seconds=1200,
                 min_seconds=1080,max_seconds=1320,opening='original')
    structure=Structure(title='本集剧情',angle='',hook='',opening_start=0,opening_end=20,opening_reason='',
        chapters=[{'title':'起因','start':20,'end':1500,'purpose':''},
                  {'title':'结果','start':1500,'end':2700,'purpose':''}])
    assert sum(chapter_budgets(structure,cfg,enforce_duration=True))==pytest.approx(1180)
    assert sum(chapter_budgets(structure,cfg))==pytest.approx(1300)  # historical natural-length path
    short=structure.model_copy(deep=True);short.chapters=short.chapters[:1];short.chapters[0].end=200
    with pytest.raises(ValueError,match='仅能提供约.*不足成片下限'):
        validate_structure(short,0,2800,cfg,enforce_duration=True)


def test_short_chapter_is_rejected_before_writing_narration(tmp_path):
    from autoclip.planning import validate_chapter
    _,structure,drafts,lines,_,cfg=fixture(tmp_path)
    with pytest.raises(ValueError,match='不足预算下限90.0秒'):
        validate_chapter(drafts[0],structure.chapters[0],structure,lines,0,100,cfg,minimum=90.)


def test_unequal_chapters_have_feasible_slot_limits_without_exceeding_clip_cap():
    from autoclip.planning import chapter_slot_limits, validate_chapter
    budgets=[74,48,53,29,186,69,323,25,26,102,38,211]
    limits=chapter_slot_limits(budgets)
    assert sum(limits)==95 and min(limits)>=4
    assert limits[6]>limits[3] and limits[6]*40>=budgets[6]
    # The old 7-slot cap rejected this valid 320-second key scene.
    structure=Structure(title='完整事件',angle='',hook='',opening_start=0,opening_end=20,opening_reason='',
        chapters=[{'title':'场景','start':20+i*400,'end':420+i*400,'purpose':''} for i in range(12)])
    start=structure.chapters[6].start
    lines=[Line(id=i,start=start+i*40+1,end=start+i*40+39,text='完整对白') for i in range(8)]
    draft={'beats':[dict(mode='narration' if i==0 else 'original',start=start+i*40,end=start+(i+1)*40,
                        instruction='叙事',evidence_ids=[i],target_chars=180 if i==0 else 0) for i in range(8)]}
    validate_chapter(draft,structure.chapters[6],structure,lines,6,323,Settings(),minimum=290,max_slots=limits[6])


def test_chapter_repair_has_distinct_attempts_even_when_model_repeats_invalid_output(tmp_path,monkeypatch):
    import autoclip.planning as module
    video,structure,drafts,lines,seconds,cfg=fixture(tmp_path)
    monkeypatch.setattr(module,'load_continuity',lambda *a:{'signature':'prior'})
    monkeypatch.setattr(module,'continuity_prompt',lambda *a:'')
    monkeypatch.setattr(module,'prepare_conflict_brief',lambda *a,**k:{})
    monkeypatch.setattr(module,'brief_context',lambda _: {})
    prompts=[]
    drafts[0]['beats'][0]['target_chars']=300
    class Model:
        def json(self,system,prompt):
            if '先定章节' in prompt:return structure.model_dump()
            prompts.append(prompt)
            return drafts[0]
    with pytest.raises(ValueError,match='字数预算'):
        module.plan_outline(video,tmp_path/'run',lines,seconds,cfg,Model())
    assert len(prompts)==cfg.repair_attempts+1
    assert len(set(prompts))==len(prompts)


def test_chapter_failure_stops_scheduling_remaining_paid_work():
    from threading import Event, Timer
    from autoclip.planning import bounded_chapters
    release=Event();started=[]
    def action(pair):
        started.append(pair[0])
        if pair[0]==0:
            raise ValueError('修复已用尽')
        assert release.wait(2)
        return pair[0]
    timer=Timer(.1,release.set);timer.start()
    try:
        with pytest.raises(ValueError,match='修复已用尽'):
            bounded_chapters(action,list(range(12)))
    finally:
        release.set();timer.join()
    assert set(started).issubset({0,1,2})
    assert bounded_chapters(lambda pair:pair[1]*2,[3,1,4,2])==[6,2,8,4]


def test_chapter_voice_notes_exclude_unseen_future_ids():
    import json
    from autoclip.planning import chapter_voice_notes
    notes=json.dumps({'policy':'候选不是定论','lines':[{'id':1,'names':['甲']},{'id':90,'names':['乙']}]})
    result=json.loads(chapter_voice_notes(notes,[Line(id=1,start=1,end=2,text='当场对白')]))
    assert result=={'policy':'候选不是定论','lines':[{'id':1,'names':['甲']}]}
    assert chapter_voice_notes('开发备注',[])=='开发备注'


def timed_chapter(seconds):
    beats=[]
    while seconds:
        part=min(40,seconds)
        beats.append(dict(mode='original',start=len(beats)*40,end=len(beats)*40+part,
                          instruction='完整对白',evidence_ids=[len(beats)]))
        seconds-=part
    return {'beats':beats}


def test_chapter_overrun_can_borrow_time_when_whole_film_meets_delivery_range(tmp_path):
    from autoclip.planning import balance_chapter_durations
    cfg=Settings(duration_profile="long",target_seconds=1200,min_seconds=1080,max_seconds=1320)
    drafts=[timed_chapter(65),timed_chapter(1115)]
    def forbidden(*a,**k):pytest.fail('全片合格，无需为局部5秒重写')
    assert balance_chapter_durations(drafts,[60,1120],20,cfg,forbidden,[],tmp_path)==drafts


@pytest.mark.parametrize('estimates,expected_index', [([800,600],0),([400,500],0)])
def test_whole_film_budget_repairs_only_largest_mismatch_and_rechecks_total(tmp_path,estimates,expected_index):
    from autoclip.planning import balance_chapter_durations, Chapter, chapter_seconds
    cfg=Settings(duration_profile="long",target_seconds=1200,min_seconds=1080,max_seconds=1320)
    chapters=[Chapter(title='事件',start=0,end=1400,purpose='完整起因结果') for _ in range(2)]
    calls=[]
    def rewrite(pair,**kwargs):
        calls.append((pair,kwargs))
        return timed_chapter(590)
    original=[timed_chapter(s) for s in estimates]
    result=balance_chapter_durations(original,[590,590],20,cfg,rewrite,chapters,tmp_path)
    assert len(calls)==1 and calls[0][0][0]==expected_index
    assert result[1] is original[1]
    assert 1080<=20+sum(chapter_seconds(d) for d in result)<=1320
    assert calls[0][1]['previous']==original[0]
    if sum(estimates)<1080:assert calls[0][1]['minimum_override']==590


def test_whole_film_budget_cannot_be_bypassed_by_repeating_same_chapter(tmp_path):
    from autoclip.planning import balance_chapter_durations, Chapter
    cfg=Settings(duration_profile="long",target_seconds=1200,min_seconds=1080,max_seconds=1320,repair_attempts=2)
    chapter=Chapter(title='事件',start=0,end=1600,purpose='完整事件')
    calls=[]
    def unchanged(pair,**kwargs):calls.append(pair);return kwargs['previous']
    with pytest.raises(ValueError,match='预算协调已达上限'):
        balance_chapter_durations([timed_chapter(1400)],[1180],20,cfg,unchanged,[chapter],tmp_path)
    assert len(calls)==2


def test_long_original_split_preserves_exact_continuous_audio_and_bad_citations():
    from autoclip.planning import split_long_originals
    lines=[Line(id=i,start=i*10,end=i*10+9,text='完整对白') for i in range(6)]
    raw={'beats':[dict(mode='original',start=0,end=59,instruction='连续对话',evidence_ids=[0,1,2,3,4,5,999])]}
    result=split_long_originals(raw,lines)['beats']
    assert len(result)==2
    assert result[0]['start']==0 and result[-1]['end']==59
    assert result[0]['end']==result[1]['start']
    assert all(1<=b['end']-b['start']<=40 for b in result)
    assert 999 in result[0]['evidence_ids']
    assert all(not s.start+.2<b['end']<s.end-.2 for s in lines for b in result)
    # No invented cut within an indivisible long utterance.
    impossible=[Line(id=0,start=0,end=59,text='一个长句')]
    assert split_long_originals(raw,impossible)=={'beats':[{**raw['beats'][0],'target_chars':0}]}


def test_film_slot_budget_is_checked_even_when_duration_passes(tmp_path):
    from autoclip.planning import balance_chapter_durations, Chapter
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320)
    chapters=[Chapter(title='事件',start=0,end=1500,purpose='完整剧情') for _ in range(2)]
    drafts=[{'beats':[dict(mode='original',start=i*12,end=(i+1)*12,instruction='完整对白',evidence_ids=[i]) for i in range(48)]} for _ in range(2)]
    calls=[]
    def rewrite(pair,**kwargs):
        calls.append(kwargs)
        return {'beats':kwargs['previous']['beats'][:kwargs['max_slots_override']]}
    result=balance_chapter_durations(drafts,[590,590],20,cfg,rewrite,chapters,tmp_path)
    assert len(calls)==1 and sum(len(d['beats']) for d in result)<=95


def test_duration_and_slot_overruns_are_repaired_together_in_bounded_round(tmp_path):
    from autoclip.planning import balance_chapter_durations, Chapter, chapter_seconds
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320,repair_attempts=1)
    chapters=[Chapter(title='事件',start=0,end=1500,purpose='完整剧情') for _ in range(3)]
    drafts=[{'beats':[dict(mode='original',start=i*15,end=(i+1)*15,instruction='完整对白',evidence_ids=[i]) for i in range(36)]} for _ in range(3)]
    calls=[]
    def rewrite(pair,**kwargs):
        calls.append(kwargs)
        assert kwargs['budget_override']<540 and kwargs['max_slots_override']<36
        return timed_chapter(int(kwargs['budget_override']))
    result=balance_chapter_durations(drafts,[394,393,393],20,cfg,rewrite,chapters,tmp_path)
    assert len(calls)==3
    assert 1080<=20+sum(chapter_seconds(d) for d in result)<=1320
    assert sum(len(d['beats']) for d in result)<=95


def test_cut_and_low_confidence_feedback_include_actual_source_boundaries(tmp_path):
    from autoclip.planning import validate_chapter
    _,structure,drafts,lines,_,cfg=fixture(tmp_path)
    drafts[0]['beats'][1]['start']=25
    lines[0].confidence=.2
    with pytest.raises(ValueError) as caught:
        validate_chapter(drafts[0],structure.chapters[0],structure,lines,0,100,cfg)
    error=str(caught.value)
    assert '(25.0, 1, 21.0, 35.0' in error
    assert '置信度须>=0.4' in error and '附近可信对白' in error


def test_budget_selection_preserves_source_intervals_and_evidence(tmp_path):
    from autoclip.planning import apply_budget_selection
    _,_,drafts,_,_,_=fixture(tmp_path)
    edit={'keep':[0,1,3],'narration':[{'index':1,'instruction':'转述已经发生的问答','target_chars':45,'evidence_ids':[1]}],
          'rationale':'保留起因结果，转述重复问答'}
    result=apply_budget_selection(drafts[0],edit)
    assert [(b['start'],b['end']) for b in result['beats']]==[(0,20),(20,40),(60,80)]
    assert result['beats'][1]['mode']=='narration'
    assert drafts[0]['beats'][1]['mode']=='original'
    edit['narration'][0]['evidence_ids']=[999]
    with pytest.raises(ValueError,match='不能新增证据'):apply_budget_selection(drafts[0],edit)
    edit['keep']=[1,0,1]
    with pytest.raises(ValueError,match='不重复'):apply_budget_selection(drafts[0],edit)


def test_budget_editor_checks_real_cost_and_retries_only_selection(tmp_path):
    from autoclip.planning import revise_chapter_budget, chapter_seconds
    _,structure,drafts,lines,_,cfg=fixture(tmp_path)
    calls=[]
    repairs=[]
    class Model:
        def review_json(self,system,prompt):
            repairs.append(prompt)
            return self.json(system,prompt)
        def json(self,system,prompt):
            calls.append(prompt)
            return {'keep':[0,1,2,3] if len(calls)==1 else [0,1,3],
                    'narration':[] if len(calls)==1 else [{'index':1,'instruction':'转述问答','target_chars':45,'evidence_ids':[1]}],
                    'rationale':'保留起因结果与必要转场'}
    result=revise_chapter_budget(drafts[0],structure.chapters[0],structure,lines,0,50,0,3,cfg,Model(),tmp_path,1)
    assert len(calls)==2 and chapter_seconds(result)==50
    assert len(repairs)==1
    assert '超过预算' in calls[1] and '不要重写时间戳' in calls[0]


def test_window_map_compiles_once_in_source_order_without_inventing_cuts():
    from autoclip.planning import compile_window_choices
    windows=[dict(window=i,start=i*30,end=(i+1)*30,evidence_ids=[i],
                  original_allowed=True,target_chars=120) for i in range(3)]
    raw={'windows':{'2':dict(mode='narration',instruction='承接已经播放的交锋',evidence_ids=[0,2]),
                    '0':dict(mode='original',instruction='关键交锋',evidence_ids=[0])}}
    result=compile_window_choices(raw,windows)['beats']
    assert [(b['start'],b['end'],b['mode']) for b in result]==[(0,30,'original'),(60,90,'narration')]
    assert result[1]['evidence_ids']==[0,2] and result[1]['target_chars']==120
    raw['windows']['2']['evidence_ids']=[1,2]
    with pytest.raises(ValueError,match='未选用或未来'):compile_window_choices(raw,windows)
    raw['windows']['2']['evidence_ids']=[0,2]
    windows[0]['original_allowed']=False
    with pytest.raises(ValueError,match='完整原声边界'):compile_window_choices(raw,windows)


@pytest.mark.parametrize('key',['-1','01','1.0','3','first'])
def test_window_map_rejects_invented_or_aliased_window_numbers(key):
    from autoclip.planning import compile_window_choices
    windows=[dict(window=0,start=0,end=30,evidence_ids=[0],original_allowed=True,target_chars=120)]
    with pytest.raises(ValueError,match='窗口键'):
        compile_window_choices({'windows':{key:dict(mode='narration',instruction='叙事',evidence_ids=[0])}},windows)


@pytest.mark.parametrize('needs_repair',[False,True])
def test_formal_planner_uses_window_map_and_window_local_dialogue(tmp_path,monkeypatch,needs_repair):
    import json
    from autoclip import planning as module
    video=tmp_path/'source.mp4';video.write_bytes(b'fixture')
    cfg=Settings(duration_profile='long',opening='original',target_seconds=1200,min_seconds=1080,max_seconds=1320)
    structure=Structure(title='完整剧情',angle='',hook='',opening_start=0,opening_end=20,opening_reason='完整对白',
        chapters=[dict(title='开端',start=20,end=100,purpose='说明处境'),
                  dict(title='转折',start=100,end=180,purpose='交锋与结果')])
    lines=[Line(id=i,start=i*20,end=(i+1)*20,text=f'本地对白{i}') for i in range(9)]
    monkeypatch.setattr(module,'load_continuity',lambda *a:{'signature':'prior'})
    monkeypatch.setattr(module,'continuity_prompt',lambda *a:'')
    monkeypatch.setattr(module,'prepare_conflict_brief',lambda *a,**k:{})
    monkeypatch.setattr(module,'brief_context',lambda _: {})
    prompts=[]
    repairs=[]
    class Model:
        def json(self,system,prompt):
            if '先定章节' in prompt:return structure.model_dump()
            if needs_repair:
                return {'windows':{'999':dict(mode='narration',instruction='虚构窗口',evidence_ids=[1])}}
            return self.choose(prompt)
        def review_json(self,system,prompt):
            repairs.append(prompt)
            assert '窗口键只能使用程序给出的编号' in prompt
            return self.choose(prompt)
        def choose(self,prompt):
            prompts.append(prompt)
            offered=json.loads(prompt.split('\n窗口及各自对白',1)[1].split('\n人物候选',1)[0])
            assert len(offered)==2
            schema,_=json.JSONDecoder().raw_decode(prompt.split('\n格式',1)[1])
            assert schema['properties']['windows']['additionalProperties'] is False
            assert set(schema['properties']['windows']['properties'])=={'0','1'}
            assert all(w['evidence_ids']==[s['id'] for s in w['source_lines']] for w in offered)
            assert '不要求每章凑两段90字' in prompt
            assert '不要返回beats列表' in prompt
            return {'windows':{str(w['window']):dict(mode='narration' if w['window']==0 else 'original',
                instruction='讲清剧情',evidence_ids=w['evidence_ids']) for w in reversed(offered)}}
    result=module.plan_outline(video,tmp_path/'run',lines,180,cfg,Model())
    assert len(prompts)==2
    assert len(repairs)==(2 if needs_repair else 0)
    assert [(b['start'],b['end']) for c in result['chapters'] for b in c['beats']]==[
        (0,20),(20,60),(60,100),(100,140),(140,180)]
