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
                for c in ['continuity','analysis','non_repetition']],
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
