from pathlib import Path

import pytest

from autoclip.config import Settings
from autoclip.metrics import audio_mix
from autoclip.models import Beat,Clip,Line,Plan,validate_plan
from autoclip.render import write_ass
from autoclip.repair import timing_report


def opening_plan():
    spans=[(0,12),(12,25),(25,65),(65,82),(82,115),(115,140)]
    lines=[Line(id=i,start=a,end=b,text='原片对白') for i,(a,b) in enumerate(spans)]
    text='这里的矛盾在于差事和责任同时交出，而在场的人都想先看看其他人的真实反应。'
    clip=Clip(title='精彩原声开场',angle='责任与处境',hook='差事归谁',beats=[
        Beat(mode='original' if i%2==0 else 'narration',start=a,end=b,
             text='' if i%2==0 else text,evidence_ids=[i],reason='依据')
        for i,(a,b) in enumerate(spans)])
    return Plan(episode_summary='',genre='',style='',clips=[clip]),lines


def test_production_config_accepts_two_minute_story_and_original_opening():
    cfg=Settings.load(Path(__file__).parents[1]/'configs.production.toml')
    assert cfg.min_seconds == 120 and cfg.opening=='original'
    plan,lines=opening_plan()
    validate_plan(plan,lines,140,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening)
    # 142.6 seconds now passes total duration, rather than triggering expansion.
    report=timing_report(plan.clips[0],{1:14.65,3:21.65,5:20.25},cfg)
    assert report['measured_total_seconds']==142.6
    assert not report['issues']


@pytest.mark.parametrize('start,end',[(0,8),(0,23)])
def test_original_opening_must_be_long_enough_but_not_drag_on(start,end):
    plan,lines=opening_plan();plan.clips[0].beats[0].start=start;plan.clips[0].beats[0].end=end
    with pytest.raises(ValueError,match='原声开场实长'):
        validate_plan(plan,lines,140,1,190,120,240,'original')


def test_original_opening_is_explicit_and_legacy_narration_mode_remains():
    plan,lines=opening_plan()
    with pytest.raises(ValueError,match='解说钩子'):
        validate_plan(plan,lines,140,1,190,120,240)
    plan.clips[0].beats[0].mode='narration';plan.clips[0].beats[0].text='开场解说'
    with pytest.raises(ValueError,match='精彩原声'):
        validate_plan(plan,lines,140,1,190,120,240,'original')


def test_original_opening_cannot_join_disconnected_scenes_to_hit_duration():
    plan,lines=opening_plan()
    plan.clips[0].beats[0].end=5
    plan.clips[0].beats.insert(1,Beat(mode='original',start=6,end=12,evidence_ids=[0],reason='另一段'))
    with pytest.raises(ValueError,match='必须连续'):
        validate_plan(plan,lines,140,1,190,120,240,'original')


def test_original_segments_do_not_display_editorial_watch_instructions(tmp_path):
    target=tmp_path/'subtitles.ass'
    write_ass(target,'标题','剧名',30,[(0,12,'听这句原声','original'),(12,20,'必要解释','narration')])
    text=target.read_text()
    assert '听这句原声' not in text and '必要解释' in text
    assert 'Style: Original' not in text
    label = next(row for row in text.splitlines() if ',Label,' in row)
    assert label.endswith('剧名') and '剧情解读' not in text


def test_audio_mix_uses_encoded_duration_and_only_initial_block():
    mix=audio_mix([
        {'mode':'original','start':0,'end':12,'output_start':0,'output_end':12},
        {'mode':'narration','start':12,'end':32,'output_start':12,'output_end':22},
        {'mode':'original','start':32,'end':50,'output_start':22,'output_end':40}])
    assert mix['original_ratio']==.75
    assert mix['narration_seconds']==10
    assert mix['opening_seconds']==12
    assert mix['opening_mode']=='original'
    assert audio_mix([])['original_ratio']==0
