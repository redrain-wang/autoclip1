import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoclip import production, short_voices
from autoclip.automatic import validate_voice_library
from autoclip.branding import BrandSettings
from autoclip.config import Settings
from autoclip.models import Beat, Clip, Line, Plan
from autoclip.util import digest, read_json, run, source_identity, write_json


def reference(tmp_path):
    video=tmp_path/'10.mp4';video.write_bytes(b'video')
    bank={'series':'测试剧','model':{'id':'fixture'},'dataset_id':'bank','revision':1,'characters':[
        {'name':'甲','samples':[{'source':source_identity(video),'status':'human_confirmed','embedding':[1.,0.]}]}]}
    library=tmp_path/'library.json';write_json(library,bank)
    return video,library,bank


@pytest.mark.parametrize('problem',['unconfirmed','changed_source','different_series','empty'])
def test_short_library_validation_rejects_untrusted_references(tmp_path,problem):
    video,library,bank=reference(tmp_path)
    if problem=='unconfirmed':bank['characters'][0]['samples'][0]['status']='machine_candidate'
    elif problem=='changed_source':video.write_bytes(b'changed')
    elif problem=='different_series':bank['series']='另一剧'
    else:bank['characters']=[]
    with pytest.raises(ValueError):validate_voice_library(bank,'测试剧')


def test_short_project_remembers_library_and_prepares_only_voice_confirmation_if_missing(tmp_path,monkeypatch):
    from autoclip import series
    video,library,bank=reference(tmp_path)
    cfg=Settings(series='测试剧',output=tmp_path/'project')
    first=short_voices.prepare_project(video,[video],cfg,library)
    assert first['status']=='ready' and first['library']==library
    assert short_voices.prepare_project(video,[video],cfg)['bank']==bank
    missing=Settings(series='测试剧',output=tmp_path/'new-project')
    monkeypatch.setattr(series,'ingest_episode',lambda v,b,c:(b,[],150))
    prepared=[]
    monkeypatch.setattr(series,'prepare_audio_candidates',lambda *a:prepared.append(a))
    waiting=short_voices.prepare_project(video,[video],missing)
    assert waiting['status']=='waiting_voice_confirmation' and len(prepared)==1
    assert not list(missing.output.rglob('development-review.json'))


def test_no_library_cannot_bypass_gate_by_calling_episode_directly(tmp_path):
    with pytest.raises(ValueError,match='人工确认声纹库'):
        production.produce_episode(tmp_path/'missing.mp4',Settings(),BrandSettings(enabled=False,watermark=tmp_path/'none'),tmp_path)
    assert list(tmp_path.iterdir())==[]


def test_short_defaults_to_shared_full_scan_and_links_current_candidates(tmp_path,monkeypatch):
    from autoclip import series
    video,library,bank=reference(tmp_path)
    bank['characters'][0]['samples'][0].update(id='known',source_id='source',start=1.,end=4.)
    write_json(library,bank)
    manifest={'series':'测试剧','model':bank['model'],'dataset_id':'scan','sources':[{'id':'source','source':source_identity(video)}],
        'samples':[{'id':'speech','source_id':'source','start':1.,'end':4.,'embedding':[1.,0.]}]}
    scans=[]
    def scan(*args):scans.append(args);return manifest
    monkeypatch.setattr(series,'scan_production_audio',scan)
    _,voices=short_voices.episode_evidence(video,[Line(id=0,start=1,end=4,text='原片对白')],library,tmp_path/'episode',Settings(series='测试剧'))
    assert scans[0][:2]==(video,library)
    assert voices['lines'][0]['samples'][0]['name']=='甲'
    assert voices['library_digest']==digest(bank)


def test_library_file_and_reference_source_changes_cannot_pass_final_gate(tmp_path):
    video,library,bank=reference(tmp_path)
    short_voices.ensure_current_library(library,bank)
    write_json(library,{**bank,'revision':2})
    with pytest.raises(RuntimeError,match='运行期间改变'):
        short_voices.ensure_current_library(library,bank)
    write_json(library,bank);video.write_bytes(b'changed source')
    with pytest.raises(ValueError,match='原片已改变'):
        short_voices.ensure_current_library(library,bank)


def test_cli_passes_voice_options_and_reuses_project_library(tmp_path,monkeypatch):
    from autoclip.cli import app
    from typer.testing import CliRunner
    video,library,bank=reference(tmp_path)
    cfg=Settings(series='测试剧',output=tmp_path/'project')
    monkeypatch.setattr(Settings,'load',lambda _:cfg.model_copy(deep=True))
    monkeypatch.setattr(Settings,'api_key',lambda _:'test')
    monkeypatch.setattr('autoclip.cli.shutil.which',lambda _:'tool')
    monkeypatch.setattr(BrandSettings,'load',lambda _:BrandSettings(enabled=False,watermark=tmp_path/'none'))
    calls=[]
    def episode(*args,**kwargs):
        calls.append((args,kwargs))
        return {'status':'scouted','candidates':0}
    monkeypatch.setattr(production,'produce_episode',episode)
    for extra in (['--library',str(library)],[]):
        result=CliRunner().invoke(app,['produce',str(video),'--stage','scout',*extra])
        assert result.exit_code==0,result.output
    assert len(calls)==2 and all(c[1]['library']==library for c in calls)
    assert all(not c[0][1].identity_gate and c[0][1].identity_registry is None for c in calls)


def test_missing_bank_cli_stops_at_voice_confirmation_before_model_key(tmp_path,monkeypatch):
    from autoclip.cli import app
    from autoclip import series
    from typer.testing import CliRunner
    video=tmp_path/'10.mp4';video.write_bytes(b'video')
    cfg=Settings(series='测试剧',output=tmp_path/'project')
    monkeypatch.setattr(Settings,'load',lambda _:cfg.model_copy(deep=True))
    monkeypatch.setattr(Settings,'api_key',lambda _:pytest.fail('建档前不能开始收费模型流程'))
    monkeypatch.setattr('autoclip.cli.shutil.which',lambda _:'tool')
    monkeypatch.setattr(BrandSettings,'load',lambda _:BrandSettings(enabled=False,watermark=tmp_path/'none'))
    monkeypatch.setattr(series,'ingest_episode',lambda v,b,c:(b,[],150))
    monkeypatch.setattr(series,'prepare_audio_candidates',lambda *a:None)
    result=CliRunner().invoke(app,['produce',str(video)])
    assert result.exit_code==2 and 'waiting_voice_confirmation' in result.output
    assert not list(cfg.output.rglob('results.json'))


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='requires FFmpeg')
@pytest.mark.parametrize('unknown',[False,True,'wrong_subject'])
def test_short_voice_gate_real_export_cache_and_reference_change(tmp_path,monkeypatch,unknown):
    from autoclip import render
    from autoclip.deliver import export_finished
    video=tmp_path/'10.mp4'
    run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=navy:s=320x180:r=1:d=240',
         '-f','lavfi','-i','sine=frequency=220:duration=240','-c:v','libx264','-preset','ultrafast','-c:a','aac','-shortest',video])
    voice=tmp_path/'voice.mp3'
    run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=10',voice])
    source=source_identity(video)
    bank={'series':'测试剧','model':{'id':'test'},'dataset_id':'bank','revision':1,'characters':[
        {'name':'甲','samples':[{'id':'known','source_id':'source','source':source,'start':1.,'end':4.,
                              'embedding':[1.,0.],'status':'human_confirmed'}]}]}
    library=tmp_path/'bank.json';manifest=tmp_path/'manifest.json';write_json(library,bank)
    starts=[offset+i*12 for offset in (0,120) for i in range(4)]
    lines=[Line(id=i,start=start+1,end=start+10,text='有人提出问题，对方随后回应。') for i,start in enumerate(starts)]
    write_json(manifest,{'series':'测试剧','model':bank['model'],'dataset_id':'samples','sources':[{'id':'source','source':source}],
        'samples':[{'id':f's{i}','source_id':'source','start':start+1.,'end':start+4.,'embedding':[1.,0.]} for i,start in enumerate(starts)]})
    cfg=Settings(series='测试剧',target_seconds=45,min_seconds=30,max_seconds=90,opening='original',
        vision=True,identity_gate=True,identity_registry=tmp_path/'must-not-use-face-bank',
        portrait=False,repair_attempts=0,output=tmp_path/'project')
    monkeypatch.setattr(production,'transcribe',lambda *a:lines)
    monkeypatch.setattr(production,'analyze',lambda *a:[])
    stories=[production.Story(title=f'完整故事{n}',angle='问答',start=offset,end=offset+120,evidence_ids=[n*4,n*4+1],reason='完整故事')
             for n,offset in enumerate((0,120))]
    def selection(root,*args):
        selected=production.Selection(episode_summary='',stories=stories[:1] if unknown else stories)
        write_json(root/'selections/fixture.json',selected.model_dump());return selected
    monkeypatch.setattr(production,'select_stories',selection)
    plans=[]
    def create(video,root,lines,seconds,maps,cfg,*args,**kwargs):
        assert not cfg.identity_gate and cfg.identity_registry is None
        assert any(row.get('voice_candidates') for row in maps)
        offset=maps[0]['start'];n=0 if offset==0 else 4
        plans.append(offset)
        return Plan(episode_summary='',genre='',style='',clips=[Clip(title=maps[0]['title'],angle='问答',hook='事情怎样展开',beats=[
            Beat(mode='original' if i%2==0 else 'narration',start=offset+i*12,end=offset+(i+1)*12,
                text='' if i%2==0 else '有人提出了请求，对方随后作出回应，这次谈话仍在继续。',evidence_ids=[n+i],reason='完整问答') for i in range(4)])])
    monkeypatch.setattr(production,'create_plan',create)
    monkeypatch.setattr(render,'synthesize',lambda *a:voice)
    # Keep real encoding/export; use a deterministic cover wrapper in this fixture.
    def package(video,root,clean,*args):
        cover=Path(clean['video']).with_suffix('.jpg');shutil.copy2(clean['poster'],cover)
        return {**clean,'cover':str(cover),'qa':{**clean['qa'],'branded_full_decode':'passed'}}
    monkeypatch.setattr(production,'package_clip',package)
    reviews=[]
    class Model:
        def __init__(self,*a):self.calls=0;self.client=SimpleNamespace(close=lambda:None)
        def usage(self):return {'calls_this_run':self.calls}
        def json(self,system,prompt,images=None):
            self.calls+=1;reviews.append(prompt)
            group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
            supplied=json.JSONDecoder().raw_decode(prompt.split('\n字幕=',1)[1])[0]
            return {'issues':[],'units':[{'id':u['id'],'verdict':'unknown' if unknown is True and u['id']=='beat-1' else 'supported',
                'reason':'人物依据不足' if unknown else '测试证据相符','evidence_ids':u.get('evidence_ids',[supplied[0]['id']]),
                'frame_ids':[0],
                'identity_basis':'voice_and_context' if unknown=='wrong_subject' and u['id']=='beat-1' else 'no_identity_claim',
                'subjects':['乙'] if unknown=='wrong_subject' and u['id']=='beat-1' else []} for u in group]}
    monkeypatch.setattr(production,'LLM',Model)
    brand=BrandSettings(watermark=tmp_path/'unused.png',enabled=False)
    def produce():return production.produce_episode(video,cfg,brand,tmp_path/'history',library=library,voice_manifest=manifest)
    result=produce()
    root=cfg.output/(video.stem+'-'+digest(source)[:8])
    if unknown:
        assert result['clips']==0 and len(result['rejected'])==1
        assert not list(root.rglob('video.mp4')) and read_json(root/'results.json')==[]
        return
    assert result['clips']==2 and not result['failures'],result
    export_finished(cfg.output)
    assert len(list((cfg.output/'成片').glob('*.mp4')))==2
    assert not list(cfg.output.rglob('development-review.json'))
    finished=read_json(root/'results.json');before={c['video']:Path(c['video']).read_bytes() for c in finished}
    assert all(c['qa']['semantic_review']=='automatic_source_review_not_human' for c in finished)
    count=len(reviews)
    assert produce()['clips']==2 and len(reviews)==count and len(plans)==2
    bank['revision']=2;bank['characters'][0]['name']='乙';write_json(library,bank)
    assert produce()['clips']==2 and len(reviews)>count and len(plans)==4
    assert all(Path(p).read_bytes()==content for p,content in before.items())
    assert (root/'voice-revalidation.json').exists()
    for clip in read_json(root/'results.json'):
        approval=read_json(Path(clip['voice_review']['approval']))
        assert approval['voice_digest']==digest(read_json(root/'voice-evidence.json'))
