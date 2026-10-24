import pytest

from autoclip.config import Settings
from autoclip.models import Beat, Clip, Line
from autoclip.narrative import require_commentary, narration_minimum
from autoclip.planning import Chapter, chapter_windows, compile_window_choices


def test_long_commentary_cannot_pass_with_short_bridges_or_only_long_raw_audio():
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320)
    clip=Clip(title='标题',angle='',hook='',beats=[
        Beat(mode='narration' if i%4==0 else 'original',start=i*40,end=(i+1)*40,
             text='他又开口说了一句话。' if i%4==0 else '',evidence_ids=[i],reason='test') for i in range(32)])
    with pytest.raises(ValueError,match='碎片过场'):require_commentary(clip,cfg)
    with pytest.raises(ValueError,match='实测'):require_commentary(clip,cfg,{'issues':[]})
    assert narration_minimum({'target_chars':150},cfg,True)==112
    assert narration_minimum({'target_chars':30},Settings(),True)==5


def test_actual_voice_duration_not_estimated_text_controls_final_ratio():
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320)
    clip=Clip(title='标题',angle='',hook='',beats=[
        Beat(mode='narration' if i<16 else 'original',start=i*40,end=(i+1)*40,
             text='有依据的完整分析'*16 if i<16 else '',evidence_ids=[i],reason='test') for i in range(40)])
    timing={'beats':[{'output_seconds':10 if i<16 else 40} for i in range(40)]}
    with pytest.raises(ValueError,match='碎片过场'):require_commentary(clip,cfg,timing)
    timing={'beats':[{'output_seconds':32 if i<16 else 40} for i in range(40)]}
    assert require_commentary(clip,cfg,timing)['narration_ratio']>.3


def test_precomputed_windows_keep_safe_cuts_and_reject_invented_or_future_choices():
    lines=[Line(id=i,start=i*5,end=i*5+4,text='完整对白') for i in range(24)]
    windows=chapter_windows(Chapter(title='一章',start=0,end=120,purpose='完整冲突'),lines)
    assert all(1<=w['end']-w['start']<=40 for w in windows)
    assert all(w['original_allowed'] for w in windows)
    choices={'beats':[{'window':i,'mode':'narration' if i%2==0 else 'original',
                       'instruction':'解释已经发生的选择与后果','evidence_ids':w['evidence_ids']} for i,w in enumerate(windows)]}
    plan=compile_window_choices(choices,windows)
    assert plan['beats'][0]['target_chars']>=90
    assert plan['beats'][1]['target_chars']==0
    choices['beats'][0]['evidence_ids']=[23]
    with pytest.raises(ValueError,match='未来'):compile_window_choices(choices,windows)
    choices['beats'][0]['window']=99
    with pytest.raises(ValueError,match='窗口'):compile_window_choices(choices,windows)


def test_narration_can_refer_back_to_all_retained_original_dialogue():
    windows=[{'window':0,'start':0,'end':30,'evidence_ids':[1,2],'original_allowed':True,'target_chars':120},
             {'window':1,'start':30,'end':60,'evidence_ids':[3],'original_allowed':True,'target_chars':120}]
    raw={'beats':[{'window':0,'mode':'original','instruction':'关键原声','evidence_ids':[1]},
                  {'window':1,'mode':'narration','instruction':'解释刚才的回应','evidence_ids':[2,3]}]}
    result=compile_window_choices(raw,windows)
    assert result['beats'][0]['evidence_ids']==[1,2]
    assert result['beats'][1]['evidence_ids']==[2,3]


def test_first_narration_can_recall_actual_opening_without_borrowing_unshown_dialogue():
    windows=[{'window':0,'start':30,'end':60,'evidence_ids':[3,4],
              'original_allowed':True,'target_chars':120}]
    raw={'beats':[{'window':0,'mode':'narration','instruction':'接上开场的提问', 'evidence_ids':[1,3]}]}
    with pytest.raises(ValueError,match=r'未来的证据\[1\]'):compile_window_choices(raw,windows)
    assert compile_window_choices(raw,windows,shown_evidence=[1,2])['beats'][0]['evidence_ids']==[1,3]
    raw['beats'][0]['evidence_ids']=[1,5]
    with pytest.raises(ValueError,match=r'未来的证据\[5\]'):compile_window_choices(raw,windows,shown_evidence=[1,2])


def test_brief_scene_can_use_a_short_bridge_but_whole_film_cannot_be_only_bridges():
    from autoclip.planning import Structure,validate_chapter
    cfg=Settings(duration_profile='long',opening='original',target_seconds=1200,min_seconds=1080,max_seconds=1320)
    structure=Structure(title='标题',angle='',hook='',opening_start=80,opening_end=100,opening_reason='开场',
        chapters=[{'title':'承接场景','start':100,'end':220,'purpose':'交代已发生的结果'}])
    lines=[Line(id=i,start=101+i*30,end=129+i*30,text='原片中的完整对白') for i in range(4)]
    raw={'beats':[{'mode':'narration' if i==0 else 'original','start':100+i*30,'end':130+i*30,
        'instruction':'交代承接','evidence_ids':[i],'target_chars':20 if i==0 else 0} for i in range(4)]}
    validate_chapter(raw,structure.chapters[0],structure,lines,0,150,cfg,max_slots=10)
    clip=Clip(title='标题',angle='',hook='',beats=[Beat(**{k:v for k,v in b.items() if k not in ('instruction','target_chars')},
        text='简洁交代当前处境。' if b['mode']=='narration' else '',reason='') for b in raw['beats']])
    with pytest.raises(ValueError,match='碎片过场'):require_commentary(clip,cfg)


def test_title_and_caption_styles_keep_text_and_do_not_overlay_original_dialogue(tmp_path):
    from autoclip.render import write_ass
    path=tmp_path/'style.ass'
    write_ass(path,'人物、行动、选择、代价、结果','剧名',20,
              [(0,10,'原片对白不用另盖字幕','original'),(10,20,'事实先讲清，再解释选择','narration')])
    text=path.read_text()
    assert all(word in text for word in ['人物','行动','选择','代价','结果'])
    assert '原片对白不用另盖字幕' not in text
    assert '事实先讲清，再解释选择' in text
    assert 'Kaiti SC' in text and 'Hiragino Sans GB' in text
    assert r'\c&H007EC0E1&' in text and r'\alpha&H35&' in text


def test_long_asr_span_never_claims_a_safe_original_cut():
    lines=[Line(id=0,start=0,end=100,text='一段无法可靠细分的转写')]
    windows=chapter_windows(Chapter(title='一章',start=0,end=100,purpose='test'),lines)
    assert not windows[0]['original_allowed']
    with pytest.raises(ValueError,match='原声边界'):
        compile_window_choices({'beats':[{'window':0,'mode':'original','instruction':'保留','evidence_ids':[0]}]},windows)


@pytest.mark.parametrize('series',['另一部剧','没有集数的新项目'])
def test_long_enough_but_repetitive_script_is_rejected_and_bound_to_actual_text(tmp_path,series):
    from autoclip.narrative import review_commentary,require_commentary_review
    from autoclip.util import digest
    cfg=Settings(series=series,duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320)
    clip=Clip(title='冲突标题',angle='',hook='',beats=[
        Beat(mode='narration',start=i*40,end=(i+1)*40,text='他答应之后又答应，这件事说明他答应了。'*8,evidence_ids=[i],reason='内部说明不是正文') for i in range(20)])
    # Mechanical quantity checks are deliberately insufficient.
    assert require_commentary(clip,cfg)['narration_chars']>=1800
    class Model:
        def json(self,system,prompt):
            assert '内部说明不是正文' not in prompt
            return {'checks':[{'criterion':c,'passed':c=='continuity','reason':'有顺序，但只在重复原声'}
                for c in ['continuity','analysis','non_repetition','naturalness']],
                'issues':[{'beat':0,'quote':'他答应之后又答应','problem':'换词复述，没有解释行动的约束',
                           'direction':'用已展示的条件解释为什么答应，删除重复复述'}]}
    result=review_commentary(clip,[],cfg,Model(),tmp_path)
    assert not result['passed'] and result['clip_digest']==digest(clip.model_dump())
    with pytest.raises(ValueError,match='不能交付'):require_commentary_review(result,clip)
    result['passed']=True;result['issues']=[]
    with pytest.raises(ValueError,match='未完整通过'):require_commentary_review(result,clip)
    for c in result['checks']:c['passed']=True
    require_commentary_review(result,clip)
    clip.beats[0].text='事实修复以后稿件已经改变。'
    with pytest.raises(ValueError,match='不能交付'):require_commentary_review(result,clip)


def test_short_transitions_do_not_trigger_formal_long_quantity_rule(tmp_path):
    from autoclip.narrative import review_commentary
    clip=Clip(title='短片',angle='',hook='',beats=[Beat(mode='narration',start=i*10,end=(i+1)*10,text='他拒绝了。',evidence_ids=[i],reason='') for i in range(4)])
    assert review_commentary(clip,[],Settings(),None,tmp_path) is None


def test_paragraph_guard_does_not_force_short_transitions_or_short_films_to_expand():
    from autoclip.narrative import require_paragraph_preserved
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320)
    require_paragraph_preserved('与此同时，另一边的人还在等消息。','另一边还在等待。',cfg)
    require_paragraph_preserved('已经展开的有据分析'*12,'简洁短片。',Settings())


def test_whole_script_editor_can_remove_redundancy_without_weakening_fact_repair_guard():
    from autoclip.narrative import require_paragraph_preserved, editorial_repair
    from autoclip.util import digest
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320)
    before='旁白逐句预告随后原声的问答。'*10
    after='府门前，求见的人还在等候。'
    issue={'stage':'narrative_review','text_digest':digest(before),'message':'删除与随后原声重复的问答'}
    require_paragraph_preserved(before,after,cfg,issues=[issue])
    assert editorial_repair([issue],before)
    # Factual repairs, stale reviews and mixed repair requests retain the guard.
    for issues in [[],[{'message':'删去错误归属'}],[{**issue,'text_digest':'old'}],
                   [issue,{'message':'还有事实问题'}]]:
        with pytest.raises(ValueError,match='短过场'):
            require_paragraph_preserved(before,after,cfg,issues=issues)


def test_editorial_condensation_still_cannot_turn_whole_film_into_bridges():
    from autoclip.narrative import require_paragraph_preserved
    from autoclip.util import digest
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320)
    before='这是被整稿编辑要求删除的重复对白。'*10
    after='来人仍在等候。'
    require_paragraph_preserved(before,after,cfg,issues=[{'stage':'narrative_review','text_digest':digest(before)}])
    clip=Clip(title='标题',angle='',hook='',beats=[Beat(mode='narration',start=i*40,end=(i+1)*40,
        text=after,evidence_ids=[i],reason='') for i in range(20)])
    with pytest.raises(ValueError,match='碎片过场'):require_commentary(clip,cfg)


def test_narrative_context_distinguishes_two_commands_and_dubbed_dialogue():
    from autoclip.narrative import narrative_context
    clip=Clip(title='标题',angle='',hook='',beats=[
        Beat(mode='narration',start=0,end=20,text='第一次命令被拦住，随后开始追问。',evidence_ids=[1,2],reason='不展示内部说明'),
        Beat(mode='original',start=20,end=30,text='',evidence_ids=[3],reason=''),
        *[Beat(mode='original',start=i*10,end=(i+1)*10,text='',evidence_ids=[i+1],reason='') for i in (3,4)]])
    lines=[Line(id=1,start=1,end=3,text='拿下'),Line(id=2,start=10,end=15,text='请回答从何得知'),
           Line(id=3,start=22,end=25,text='还不拿下')]
    heard,source=narrative_context(clip,lines)
    assert heard[0]['text']==clip.beats[0].text and heard[1]['text']=='还不拿下'
    assert [r['id'] for r in source[0]['dialogue']]==[1,2]
    assert source[0]['dialogue'][0]['start']<heard[1]['source_start']
    assert len(source)==1 and 'reason' not in heard[0]


@pytest.mark.parametrize('resolved',[False,True])
def test_editorial_patch_cannot_pass_by_merely_rephrasing_repeated_dialogue(tmp_path,resolved):
    import json
    from autoclip.narrative import verify_editorial_patch
    clip=Clip(title='标题',angle='',hook='',beats=[
        Beat(mode='narration',start=0,end=10,text='他命人赶快拿下。',evidence_ids=[1],reason=''),
        Beat(mode='original',start=10,end=20,text='',evidence_ids=[2],reason=''),
        *[Beat(mode='original',start=i*10,end=(i+1)*10,text='',evidence_ids=[i+1],reason='') for i in (2,3)]])
    lines=[Line(id=1,start=1,end=3,text='拿下'),Line(id=2,start=11,end=13,text='还不拿下')]
    class Model:
        def json(self,system,prompt):
            actual=json.JSONDecoder().raw_decode(prompt.split('\naudience=',1)[1])[0]
            source=json.JSONDecoder().raw_decode(prompt.split('\nsource_context=',1)[1])[0]
            assert actual[1]['text']=='还不拿下' and source[0]['dialogue'][0]['asr_text']=='拿下'
            return {'resolved':resolved,'reason':'已区分两次命令' if resolved else '仍然逐句预告原声',
                    'remaining_quote':'' if resolved else '他命人赶快拿下'}
    args=(clip,0,[{'message':'删去重复'}],lines,Model(),tmp_path)
    if resolved:verify_editorial_patch(*args)
    else:
        with pytest.raises(ValueError,match='仍未落实'):verify_editorial_patch(*args)
    assert json.loads((tmp_path/'editorial-verification.json').read_text())['resolved'] is resolved


def test_old_narrative_approval_cannot_skip_source_timeline_review():
    from autoclip.narrative import require_commentary_review,NARRATIVE_POLICY
    from autoclip.util import digest
    clip=Clip(title='标题',angle='',hook='',beats=[Beat(mode='narration',start=i*10,end=(i+1)*10,text='讲述',evidence_ids=[i+1],reason='') for i in range(4)])
    report={'policy':NARRATIVE_POLICY,'passed':True,'clip_digest':digest(clip.model_dump()),'issues':[],
            'checks':[{'criterion':c,'passed':True,'reason':'旧核定'} for c in ['continuity','analysis','non_repetition']]}
    with pytest.raises(ValueError,match='不能交付'):require_commentary_review(report,clip)


def test_naturalness_failure_blocks_otherwise_approved_long_script(tmp_path):
    from autoclip.narrative import review_commentary, require_commentary_review, NARRATIVE_CRITERIA
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320)
    clip=Clip(title='标题',angle='',hook='',beats=[Beat(mode='narration',start=i*30,end=(i+1)*30,
        text='来意尚未说开，一个等着开口，一个掌握着谈话的节奏。',evidence_ids=[i],reason='') for i in range(4)])
    class Model:
        def json(self,system,prompt):
            assert 'naturalness' in prompt and '普通动作硬拔高' in prompt
            assert '不能因没有“字幕里说”等禁词就认定自然' in prompt
            return {'checks':[{'criterion':c,'passed':c!='naturalness','reason':'普通请安被硬加意义' if c=='naturalness' else '通过'}
                for c in sorted(NARRATIVE_CRITERIA)],'issues':[{'beat':0,'quote':'一个掌握着谈话的节奏',
                    'problem':'没有具体交锋支撑这句判断','direction':'直接交代请安与来意，删除空分析'}]}
    report=review_commentary(clip,[],cfg,Model(),tmp_path)
    assert not report['passed']
    with pytest.raises(ValueError,match='不能交付'):require_commentary_review(report,clip)


def test_listening_review_only_receives_actual_audio_not_covered_source_dialogue(tmp_path):
    from autoclip.narrative import review_commentary,require_commentary_review,NARRATIVE_CRITERIA
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320)
    clip=Clip(title='标题',angle='',hook='',beats=[Beat(mode='narration' if i==0 else 'original',
        start=i*20,end=(i+1)*20,text='这段用自然讲述代替原片对白。' if i==0 else '',evidence_ids=[i],reason='') for i in range(4)])
    lines=[Line(id=i,start=i*20+1,end=i*20+10,text='这句话只在被配音覆盖的素材里' if i==0 else '这是观众实际听到的原声') for i in range(4)]
    class Model:
        def json(self,system,prompt):
            assert '这句话只在被配音覆盖的素材里' not in prompt
            assert '这段用自然讲述代替原片对白。' in prompt and '这是观众实际听到的原声' in prompt
            return {'checks':[{'criterion':c,'passed':True,'reason':'实际音轨没有重复'} for c in sorted(NARRATIVE_CRITERIA)],'issues':[]}
    report=review_commentary(clip,lines,cfg,Model(),tmp_path)
    require_commentary_review(report,clip)


@pytest.mark.parametrize('criteria',[
    ['continuity','analysis','non_repetition'],
    ['continuity','analysis','non_repetition','continuity'],
])
def test_current_approval_cannot_omit_or_replace_naturalness(criteria):
    from autoclip.narrative import require_commentary_review,NARRATIVE_POLICY,NARRATIVE_REVIEW_POLICY
    from autoclip.util import digest
    clip=Clip(title='标题',angle='',hook='',beats=[Beat(mode='narration',start=i*10,end=(i+1)*10,
        text='具体讲清已发生的事情。',evidence_ids=[i],reason='') for i in range(4)])
    report={'policy':NARRATIVE_POLICY,'review_policy':NARRATIVE_REVIEW_POLICY,'passed':True,
        'clip_digest':digest(clip.model_dump()),'issues':[],
        'checks':[{'criterion':c,'passed':True,'reason':'通过'} for c in criteria]}
    with pytest.raises(ValueError,match='未完整通过'):require_commentary_review(report,clip)
