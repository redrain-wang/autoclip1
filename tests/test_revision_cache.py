from pathlib import Path
import pytest
from autoclip.config import Settings
from autoclip.models import Beat, Clip, Plan
from autoclip.media_cache import shot_inputs, cached_shot
from autoclip.revision import edit_narration
from autoclip.util import write_json, read_json


def test_shot_key_changes_only_for_actual_media_dependencies(tmp_path):
    voice=tmp_path/'voice.mp3';voice.write_bytes(b'audio A')
    b=Beat(mode='narration',start=1,end=10,text='一段文字',reason='选段理由',evidence_ids=[1])
    cfg=Settings()
    old=shot_inputs({'source':'A'},b,9,voice,cfg)
    b.reason='理由变了';b.evidence_ids=[2]
    assert shot_inputs({'source':'A'},b,9,voice,cfg)==old
    assert shot_inputs({'source':'B'},b,9,voice,cfg)!=old
    voice.write_bytes(b'audio B')
    assert shot_inputs({'source':'A'},b,9,voice,cfg)!=old
    b.mode='original';b.text=''
    original=shot_inputs({'source':'A'},b,9,None,cfg)
    cfg.voice='zh-CN-YunjianNeural';cfg.original_volume=.1
    assert shot_inputs({'source':'A'},b,9,None,cfg)==original
    cfg.portrait=False
    assert shot_inputs({'source':'A'},b,9,None,cfg)!=original


def test_corrupt_or_partial_cached_media_is_rebuilt(tmp_path,monkeypatch):
    import autoclip.media_cache as media
    monkeypatch.setattr(media,'duration',lambda path:10)
    writes=[]
    def encode(path): writes.append(path);path.write_bytes(b'complete media')
    inputs={'target_seconds':10}
    path,seconds,reused=cached_shot(tmp_path,inputs,encode)
    assert not reused
    assert cached_shot(tmp_path,inputs,encode)[2] and len(writes)==1
    path.write_bytes(b'corrupt')
    assert not cached_shot(tmp_path,inputs,encode)[2] and len(writes)==2
    def failed(path): path.write_bytes(b'partial');raise RuntimeError('ffmpeg failed')
    with pytest.raises(RuntimeError): cached_shot(tmp_path,{'target_seconds':11},failed)
    assert len(list(tmp_path.glob('*.mp4')))==1


def test_edit_by_finished_time_changes_only_one_paragraph_and_keeps_original(tmp_path):
    beats=[Beat(mode='original' if i==0 else 'narration',start=i*10,end=i*10+10,
                text='' if i==0 else '这段解说需要再推敲',evidence_ids=[i],reason='画面依据') for i in range(4)]
    plan=Plan(episode_summary='承诺与兑现',genre='历史',style='解读',clips=[Clip(title='还款选择',hook='为何难还',angle='代价',beats=beats)])
    source=tmp_path/'plan.json';write_json(source,plan.model_dump())
    manifest=tmp_path/'manifest.json';write_json(manifest,{'timeline':[{**b.model_dump(),'output_start':i*10,'output_end':i*10+10} for i,b in enumerate(beats)]})
    before=source.read_bytes()
    result=edit_narration(source,tmp_path/'new.json','承诺已经说出口，银子却还没有到账',at='00:25',manifest=manifest)
    assert result['beat']==2 and source.read_bytes()==before
    changed=read_json(tmp_path/'new.json');expected=plan.model_dump()
    expected['clips'][0]['beats'][2]['text']='承诺已经说出口，银子却还没有到账'
    assert changed==expected
    with pytest.raises(ValueError,match='原声'): edit_narration(source,tmp_path/'other.json','新稿',at='00:05',manifest=manifest)
    with pytest.raises(ValueError,match='已存在'): edit_narration(source,source,'新稿',beat=2)
    raw=read_json(manifest);raw['timeline'][2]['text']='另一版配音稿';write_json(manifest,raw)
    with pytest.raises(ValueError,match='不匹配'): edit_narration(source,tmp_path/'bad.json','新稿',at='00:25',manifest=manifest)


def test_shared_voice_cache_reuses_across_revision_roots_and_preserves_voice_choice(tmp_path,monkeypatch):
    import autoclip.render as render
    calls=[]
    def synth(text,path,cfg): calls.append((text,cfg.voice));path.write_bytes((text+cfg.voice).encode()*20);return path
    monkeypatch.setattr(render,'_synthesize',synth)
    cfg=Settings(media_cache=tmp_path/'shared')
    first=render.synthesize('原有口播',tmp_path/'draft1',cfg)
    assert render.synthesize('原有口播',tmp_path/'draft2',cfg)==first and len(calls)==1
    render.synthesize('改了一句',tmp_path/'draft2',cfg)
    cfg.voice='zh-CN-YunjianNeural'
    assert render.synthesize('原有口播',tmp_path/'draft3',cfg)!=first and len(calls)==3
