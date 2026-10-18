from pathlib import Path

import pytest

from autoclip.config import Settings
from autoclip.models import Beat, Clip, Line, Plan, validate_plan
from autoclip.repair import timing_report
from autoclip.util import source_identity, write_json


def test_long_duration_is_explicit_and_short_default_stays_bounded():
    with pytest.raises(ValueError, match='duration_profile=long'):
        Settings(target_seconds=1050, min_seconds=900, max_seconds=1200)
    cfg = Settings.load(Path(__file__).parents[1] / 'configs.long.toml')
    assert cfg.duration_profile == 'long' and cfg.min_seconds == 900 and cfg.max_seconds == 1200
    assert cfg.llm_base_url == 'https://opencode.ai/zen/go/v1'
    short = Settings.load(Path(__file__).parents[1] / 'configs.production.toml')
    assert short.duration_profile == 'short' and short.target_seconds == 190 and short.max_seconds == 240


def test_explicit_long_ceiling_can_preserve_a_story_over_twenty_minutes():
    from autoclip.models import Beat, Clip
    from autoclip.repair import timing_report

    story = Settings(duration_profile='long', target_seconds=1050,
                     min_seconds=60, max_seconds=2700)
    beats = [Beat(mode='original', start=i*35, end=(i+1)*35,
                  evidence_ids=[1], reason='连续对白') for i in range(40)]
    beats.append(Beat(mode='narration', start=1400, end=1420,
                      text='说明转场的原因', evidence_ids=[1], reason='转场'))
    clip = Clip(title='完整故事', angle='顺叙', hook='事情如何发生', beats=beats)
    assert timing_report(clip, {40: 20}, story)['issues'] == []
    legacy = story.model_copy(update={'min_seconds': 900, 'max_seconds': 1200})
    assert any(x['code'] == 'duration_long' for x in timing_report(clip, {40: 20}, legacy)['issues'])
    assert any(x['code'] == 'voice_alignment' for x in timing_report(clip, {40: 40}, story)['issues'])
    with pytest.raises(ValueError, match='duration_profile=long'):
        Settings(target_seconds=190, max_seconds=2700)


def test_more_shots_do_not_relax_real_measured_duration_or_chronology():
    beats = [Beat(mode='original', start=0, end=16, evidence_ids=[0], reason='连续开场')]
    beats += [Beat(mode='narration', start=16+i*40, end=56+i*40, text='分析原片人物的选择以及代价并明确推断边界。'*8,
                   evidence_ids=[i+1], reason='分析') for i in range(29)]
    lines = [Line(id=i, start=b.start, end=b.end, text='原片证据') for i,b in enumerate(beats)]
    plan = Plan(episode_summary='', genre='', style='', clips=[Clip(title='完整追款冲突', angle='', hook='', beats=beats)])
    with pytest.raises(ValueError, match='短片最多24个镜头'):
        validate_plan(plan,lines,1200,1,190,120,240,'original')
    cfg = Settings(duration_profile='long', target_seconds=1050, min_seconds=900, max_seconds=1200)
    validate_plan(plan,lines,1200,1,1050,900,1200,'original')
    report = timing_report(plan.clips[0], {i:50 for i in range(1,30)},cfg)
    assert any(issue['code']=='duration_long' for issue in report['issues'])
    beats[3].start=beats[2].start
    with pytest.raises(ValueError, match='不能重复或倒置'):
        validate_plan(plan,lines,1200,1,1050,900,1200,'original')


@pytest.mark.parametrize('outline_mode',[None,'analysis','story_first'])
def test_story_first_outline_keeps_a_short_scene_bridge_without_padding(tmp_path, monkeypatch,outline_mode):
    import autoclip.outline as module
    video = tmp_path/'09.mp4'
    video.write_bytes(b'fixture')
    marker = source_identity(video)
    write_json(tmp_path/'source.json', marker)
    lines = [Line(id=i, start=a, end=b, text='原片对白')
             for i, (a, b) in enumerate([(0, 20), (22, 26), (31, 44), (46, 49)])]
    transcript = tmp_path/'transcript.json'
    write_json(transcript, [x.model_dump() for x in lines])
    slots = [dict(id=i, mode=mode, start=a, end=b, evidence_ids=[i], allowed_evidence_ids=[i],
                  target_chars=120 if mode == 'narration' else 0, instruction='交代场景切换')
             for i, (mode, a, b) in enumerate([('original', 0, 21), ('narration', 21, 30),
                                               ('original', 30, 45), ('narration', 45, 55)])]
    outline = dict(source=marker, title='接着讲故事', angle='顺叙', hook='事情怎样发生',
                   chapters=[dict(title='事情经过', beats=slots)])
    if outline_mode is not None:
        outline['narrative_mode']=outline_mode
    path = tmp_path/'outline.json'
    write_json(path, outline)
    monkeypatch.setattr(module, 'duration', lambda *args: 60)
    class Model:
        calls = 0
        def json(self, system, prompt):
            self.calls += 1
            assert '允许旁白只交代行动' in prompt
            return {'items': [{'id': i, 'text': '到了夜里，他再次回到屋里。', 'evidence_ids': [i]}
                              for i in [1, 3]]}
    model = Model()
    cfg = Settings(target_seconds=45, min_seconds=30, max_seconds=90, opening='original',
                   vision=False, oral_edit=False, continuity_history=tmp_path/'history')
    result = module.compose_outline(video, transcript, path, tmp_path/'run', cfg, model)
    assert model.calls == 1
    assert all(len(b.text) < 54 for b in result.clips[0].beats if b.mode == 'narration')


@pytest.mark.parametrize('problem',['source','future','duplicate'])
def test_outline_rejects_unbound_or_future_evidence_before_api(tmp_path,monkeypatch,problem):
    import autoclip.outline as module
    video=tmp_path/'video.mp4';video.write_bytes(b'fixture')
    marker=source_identity(video)
    write_json(tmp_path/'source.json',marker)
    transcript=tmp_path/'transcript.json'
    write_json(transcript,[{'id':1,'start':20,'end':21,'text':'未来对白'}])
    slot={'id':0,'mode':'narration','start':10,'end':15,'evidence_ids':[1],'allowed_evidence_ids':[1],'instruction':'解说','target_chars':30}
    outline={'source':marker,'title':'例子','angle':'','hook':'','chapters':[{'title':'第一章','beats':[slot]}]}
    if problem=='source':outline['source']={}
    if problem=='duplicate':outline['chapters'][0]['beats'].append(dict(slot))
    path=tmp_path/'outline.json';write_json(path,outline)
    monkeypatch.setattr(module,'duration',lambda *args:50)
    class Model:
        def json(self,*args):pytest.fail('不应发送无效证据')
    with pytest.raises(ValueError):
        module.compose_outline(video,transcript,path,tmp_path/'run',Settings(),Model())


def test_measured_window_fit_keeps_text_originals_and_source_bounds(tmp_path,monkeypatch):
    import autoclip.sample as sample
    import autoclip.render as render
    from autoclip.util import read_json
    video=tmp_path/'source.mp4';video.write_bytes(b'fixture')
    write_json(tmp_path/'source.json',source_identity(video))
    beats=[Beat(mode='original',start=0,end=16,evidence_ids=[0],reason='开场'),
           Beat(mode='narration',start=16,end=56,text='分析人物选择及代价。'*9,evidence_ids=[1],reason='解释'),
           Beat(mode='original',start=56,end=96,evidence_ids=[2],reason='完整原声'),
           Beat(mode='narration',start=96,end=136,text='解释不同选择和收益。'*9,evidence_ids=[3],reason='解释')]
    lines=[Line(id=i,start=a,end=b,text='依据') for i,(a,b) in enumerate([(0,15),(17,18),(60,65),(110,111)])]
    path=tmp_path/'plan.json';transcript=tmp_path/'transcript.json'
    plan=Plan(episode_summary='',genre='',style='',clips=[Clip(title='场景分析',angle='',hook='',beats=beats)])
    write_json(path,plan.model_dump());write_json(transcript,[s.model_dump() for s in lines])
    monkeypatch.setattr(render,'synthesize',lambda *a:tmp_path/'fake-voice.mp3')
    monkeypatch.setattr(sample,'duration',lambda p:150 if p==video else 12)
    report=sample.measure_sample_plan(video,path,transcript,Settings(target_seconds=100,min_seconds=60,max_seconds=180,opening='original'),tmp_path/'run')
    after=Plan.model_validate(read_json(tmp_path/'run/plan-timed.json')).clips[0]
    assert report['status']=='measured_not_reviewed' and not report['issues']
    for old,new in zip(beats,after.beats):
        assert old.text==new.text and old.evidence_ids==new.evidence_ids
        assert old.start<=new.start<new.end<=old.end
        if old.mode=='original':assert old==new
    assert list(tmp_path.rglob('*.mp4')) == [video]
    assert video.read_bytes() == b'fixture'


def test_long_profile_rejects_unvalidated_automatic_story_path_before_io(tmp_path):
    from autoclip.pipeline import process
    from autoclip.production import produce_episode
    from autoclip.branding import BrandSettings
    cfg=Settings(duration_profile='long',target_seconds=1050)
    missing=tmp_path/'missing.mp4'
    with pytest.raises(ValueError,match='write-outline'):
        process(missing,cfg)
    with pytest.raises(ValueError,match='write-outline'):
        produce_episode(missing,cfg,BrandSettings(watermark=tmp_path/"logo.png"),tmp_path/'history')
    assert not list(tmp_path.iterdir())
