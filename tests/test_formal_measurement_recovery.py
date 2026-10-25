import pytest

from autoclip.config import Settings
from autoclip.models import Beat, Clip, Line, Plan
from autoclip.util import source_identity, write_json


@pytest.mark.parametrize('invalid_source',[False,True])
def test_formal_intermediate_uses_measured_duration_but_still_rejects_invalid_evidence(tmp_path,monkeypatch,invalid_source):
    from autoclip import sample, render
    video=tmp_path/'video.mp4';video.write_bytes(b'source')
    lines=[Line(id=i,start=a+1,end=b-1,text='原片对白') for i,(a,b) in enumerate([(0,15),(15,45),(45,75),(75,105)])]
    plan=Plan(episode_summary='',genre='',style='',clips=[Clip(title='完整主线',angle='',hook='',beats=[
        Beat(mode='original' if i%2==0 else 'narration',start=a,end=b,text='' if i%2==0 else '只保留有据的必要承接',
             evidence_ids=[99 if invalid_source and i==1 else i],reason='剧情')
        for i,(a,b) in enumerate([(0,15),(15,45),(45,75),(75,105)])])])
    write_json(tmp_path/'source.json',source_identity(video))
    transcript=tmp_path/'transcript.json';write_json(transcript,[s.model_dump() for s in lines])
    path=tmp_path/'plan.json';write_json(path,plan.model_dump())
    voices=[]
    def synthesize(text,*args):voices.append(text);return tmp_path/'voice.mp3'
    monkeypatch.setattr(render,'synthesize',synthesize)
    monkeypatch.setattr(sample,'duration',lambda p:1400 if p==video else 4)
    cfg=Settings(duration_profile='long',target_seconds=1200,min_seconds=1080,max_seconds=1320,opening='original')
    if invalid_source:
        with pytest.raises(ValueError,match='不存在的字幕'):sample.measure_sample_plan(video,path,transcript,cfg,tmp_path/'out')
        assert not voices
    else:
        result=sample.measure_sample_plan(video,path,transcript,cfg,tmp_path/'out')
        assert voices and result['measured_total_seconds']<1080 and result['issues']
        assert result['status']=='measured_not_reviewed'
        assert (tmp_path/'out/plan-timed.json').exists()
    assert not (tmp_path/'out/plan-approved.json').exists()
