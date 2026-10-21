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
    # Enrollment itself is tested separately; synthetic tones are not real speakers.
    monkeypatch.setattr(short_voices,'prepare_voice_followup',lambda *a:None)
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
            if '原片画面观察员' in system:
                times=json.JSONDecoder().raw_decode(prompt.split('\ntimes=',1)[1])[0]
                return {'frames':[{'frame_id':i,'visible':'测试素材中的颜色画面'} for i in range(len(times))]}
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
    effective_cfg=cfg.model_copy(update={'identity_gate':False,'identity_registry':None,'narrative_mode':'story_first'})
    write_json(root/'voice-followup.json',{'status':'waiting_voice_confirmation','source':source,
        'library':str(library.resolve()),'library_digest':digest(bank),
        'voice_digest':digest(read_json(root/'voice-evidence.json')),
        'transcript_digest':digest([s.model_dump() for s in lines]),
        'settings_digest':digest(effective_cfg.model_dump(mode='json'))})
    assert produce()['status']=='waiting_voice_confirmation' and len(reviews)==count
    export_finished(cfg.output)
    assert len(list((cfg.output/'成片').glob('*.mp4')))==2
    bank['revision']=2;bank['characters'][0]['name']='乙';write_json(library,bank)
    assert produce()['clips']==2 and len(reviews)>count and len(plans)==2
    assert all(Path(p).read_bytes()==content for p,content in before.items())
    assert (root/'voice-revalidation.json').exists()
    for clip in read_json(root/'results.json'):
        approval=read_json(Path(clip['voice_review']['approval']))
        assert approval['voice_digest']==digest(read_json(root/'voice-evidence.json'))


def followup_fixture(tmp_path, monkeypatch):
    from autoclip import series
    from autoclip.voices import candidate_spans
    video,library,bank=reference(tmp_path)
    bank['characters'][0]['samples'][0].update(id='a'*16,source_id='old',start=1.,end=4.)
    write_json(library,bank)
    cfg=Settings(series='测试剧',output=tmp_path/'project')
    root=cfg.output/(video.stem+'-'+digest(source_identity(video))[:8])
    lines=[Line(id=i,start=10.+i*6,end=14.+i*6,text='清晰的原片对白') for i in range(4)]
    plan=Plan(episode_summary='',genre='',style='',clips=[Clip(title='测试故事',angle='',hook='',beats=[
        Beat(mode='narration',start=s.start,end=s.end,text='有依据的讲述',evidence_ids=[s.id],reason='依据') for s in lines])])
    voices={'source':source_identity(video),'transcript_digest':digest([s.model_dump() for s in lines]),'lines':[]}
    story=root/'stories'/'story'
    write_json(story/'plan-timed.json',plan.model_dump())
    report={'source':source_identity(video),'plan_digest':digest(plan.model_dump()),
            'transcript_digest':digest([s.model_dump() for s in lines]),'voice_digest':digest(voices),
            'passed':False,'issues':[{'id':'beat-1','message':'声音候选缺失，人物身份不能确定'},
                                    {'id':'beat-2','message':'事实不符'}]}
    write_json(story/'automatic-review.json',report)
    calls=[]
    def prepare(videos,transcripts,folder,settings,*args):
        calls.append(folder)
        chosen=[Line.model_validate(s) for s in read_json(transcripts[0])]
        samples=[{'id':digest([s['line_id'],'sample']),'source_id':'new','embedding':[0.,1.],
                  'group_id':'voice-001',**s} for s in candidate_spans(chosen,100)]
        manifest={'series':settings.series,'model':bank['model'],'dataset_id':'new-batch',
                  'sources':[{'id':'new','label':'10','duration':100.,'source':source_identity(video)}],
                  'samples':samples,'groups':[{'id':'voice-001','sample_ids':[s['id'] for s in samples],
                                              'preview_ids':[s['id'] for s in samples]}]}
        write_json(folder/'manifest.json',manifest)
        return manifest
    monkeypatch.setattr(series,'prepare_audio_candidates',prepare)
    return video,library,bank,cfg,root,lines,voices,story,report,calls


def test_automatic_voice_followup_is_anonymous_cached_and_only_selects_identity_evidence(tmp_path,monkeypatch):
    video,library,bank,cfg,root,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    args=(video,lines,voices,root,cfg,library,[story])
    task=short_voices.prepare_voice_followup(*args)
    folder=Path(task['review_root'])
    assert task['sample_lines']==[1] and read_json(library)==bank
    assert read_json(folder/'registry.json')['characters']==[]
    assert read_json(folder/'baseline-library.json')==bank
    assert read_json(folder/'registry.expanded.json')['characters'][0]['samples']==bank['characters'][0]['samples']
    assert short_voices.prepare_voice_followup(*args)==task and len(calls)==1
    assert short_voices.pending_voice_followup(video,lines,voices,root,cfg,library)==task
    assert short_voices.pending_voice_followup(video,lines,voices,root,cfg.model_copy(update={'target_seconds':180}),library) is None


@pytest.mark.parametrize('binding',['source','transcript_digest','plan_digest','voice_digest'])
def test_stale_failed_review_cannot_trigger_voice_enrollment(tmp_path,monkeypatch,binding):
    video,library,bank,cfg,root,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    report[binding]='stale';write_json(story/'automatic-review.json',report)
    assert short_voices.prepare_voice_followup(video,lines,voices,root,cfg,library,[story]) is None
    assert calls==[] and read_json(library)==bank


def test_only_finished_human_voice_decisions_append_with_backup_and_resume(tmp_path,monkeypatch):
    from autoclip.voice_review import ReviewStore
    video,library,bank,cfg,root,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    task=short_voices.prepare_voice_followup(video,lines,voices,root,cfg,library,[story])
    store=ReviewStore(task['review_root'])
    payload={'dataset_id':store.manifest['dataset_id'],'revision':0}
    with pytest.raises(ValueError,match='确认至少一位'):
        store.finish(payload)  # An existing baseline cannot finish an empty follow-up.
    sid=store.manifest['samples'][0]['id']
    saved=store.save({**payload,'changes':{sid:{'status':'named','name':'乙'}}})
    short_voices.apply_confirmed_followups(cfg.output,library,cfg.series)
    assert read_json(library)==bank  # Saving a name alone does not finish enrollment.
    store.finish({**payload,'revision':saved['revision']})
    short_voices.prepare_project(video,[video],cfg,library)
    merged=read_json(library)
    assert {p['name'] for p in merged['characters']}=={'甲','乙'}
    assert read_json(library.parent/'history'/f'{library.stem}-{digest(bank)}.json')==bank
    assert read_json(root/'voice-followup.json')['status']=='applied'
    assert short_voices.pending_voice_followup(video,lines,voices,root,cfg,library) is None
    short_voices.apply_confirmed_followups(cfg.output,library,cfg.series)
    assert read_json(library)==merged
    # Simulate a crash after bank replacement but before pointer update.
    write_json(root/'voice-followup.json',task)
    short_voices.apply_confirmed_followups(cfg.output,library,cfg.series)
    assert read_json(library)==merged


def test_pending_voice_confirmation_stops_before_paid_production(tmp_path,monkeypatch):
    video,library,bank,cfg,root,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    cfg=cfg.model_copy(update={'identity_gate':False,'identity_registry':None,'narrative_mode':'story_first'})
    task=short_voices.prepare_voice_followup(video,lines,voices,root,cfg,library,[story])
    monkeypatch.setattr(production,'duration',lambda _:100)
    monkeypatch.setattr(production,'transcribe',lambda *a:lines)
    monkeypatch.setattr(production,'episode_evidence',lambda *a:(bank,voices))
    monkeypatch.setattr(production,'LLM',lambda *a:pytest.fail('声纹待确认时不应继续收费改稿'))
    result=production.produce_episode(video,cfg,BrandSettings(enabled=False,watermark=tmp_path/'none'),tmp_path/'history',library=library)
    assert result['status']=='waiting_voice_confirmation' and result['voice_confirmation']==task
    assert read_json(root/'status.json')['stage']=='等待新增声纹确认'


def test_waiting_voice_cli_exports_completed_film_and_returns_two(tmp_path,monkeypatch):
    from autoclip.cli import app
    from typer.testing import CliRunner
    video,library,bank=reference(tmp_path)
    cfg=Settings(series='测试剧',output=tmp_path/'project')
    monkeypatch.setattr(Settings,'load',lambda _:cfg.model_copy(deep=True))
    monkeypatch.setattr(Settings,'api_key',lambda _:'test')
    monkeypatch.setattr('autoclip.cli.shutil.which',lambda _:'tool')
    monkeypatch.setattr(BrandSettings,'load',lambda _:BrandSettings(enabled=False,watermark=tmp_path/'none'))
    monkeypatch.setattr(production,'produce_episode',lambda *a,**k:{'status':'waiting_voice_confirmation','clips':1,
        'voice_confirmation':{'instruction':'试听本集新增声音'}})
    exports=[]
    monkeypatch.setattr('autoclip.deliver.export_finished',lambda root:exports.append(root))
    monkeypatch.setattr('autoclip.cli.build_report',lambda *a:'report')
    result=CliRunner().invoke(app,['produce',str(video),'--library',str(library)])
    assert result.exit_code==2,result.output
    assert exports==[cfg.output] and '成片 1 条' in result.output and '试听本集新增声音' in result.output


@pytest.mark.parametrize('problem',['changed_source','different_batch','wrong_source'])
def test_finished_followup_must_still_match_reviewed_source_and_batch(tmp_path,monkeypatch,problem):
    from autoclip.voice_review import ReviewStore
    video,library,bank,cfg,root,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    task=short_voices.prepare_voice_followup(video,lines,voices,root,cfg,library,[story])
    folder=Path(task['review_root']);store=ReviewStore(folder)
    saved=store.save({'dataset_id':store.manifest['dataset_id'],'revision':0,
        'changes':{store.manifest['samples'][0]['id']:{'status':'named','name':'乙'}}})
    store.finish({'dataset_id':store.manifest['dataset_id'],'revision':saved['revision']})
    if problem=='changed_source':video.write_bytes(b'changed')
    elif problem=='different_batch':
        review=read_json(folder/'review.json');review['dataset_id']='different';write_json(folder/'review.json',review)
    else:
        manifest=read_json(folder/'manifest.json');manifest['sources'][0]['source']={'path':'other'}
        write_json(folder/'manifest.json',manifest)
    with pytest.raises(ValueError):short_voices.apply_confirmed_followups(cfg.output,library,cfg.series)
    assert read_json(library)==bank


@pytest.mark.parametrize('problem',[None,'current','source','transcript','configuration','story','future'])
def test_reference_change_reuses_bound_draft_only_and_never_old_approval(tmp_path,monkeypatch,problem):
    video,library,bank,cfg,root,lines,voices,folder,report,calls=followup_fixture(tmp_path,monkeypatch)
    cfg=cfg.model_copy(update={'target_seconds':45,'min_seconds':1,'max_seconds':90,'opening':'narration'})
    story=production.Story(title='测试故事',angle='对话',start=0,end=120,evidence_ids=[0,1],reason='完整对话')
    old_voices={**voices,'library_digest':'old'}
    voices={**voices,'library_digest':'new'}
    previous=root/'runs'/digest({'voices':old_voices,'cfg':cfg.model_dump(mode='json'),'story':story.model_dump()})
    current=root/'runs'/digest({'voices':voices,'cfg':cfg.model_dump(mode='json'),'story':story.model_dump()});current.mkdir(parents=True)
    write_json(previous/'source.json',source_identity(video))
    write_json(previous/'transcript.json',[s.model_dump() for s in lines])
    write_json(previous/'voice-evidence.json',old_voices)
    plan=read_json(folder/'plan-timed.json')
    for beat in plan['clips'][0]['beats'][2:]:beat.update(mode='original',text='')
    if problem=='current':
        write_json(current/'plan-timed.json',plan)
        write_json(current/'source.json',source_identity(video))
        write_json(current/'transcript.json',[s.model_dump() for s in lines])
        write_json(current/'voice-evidence.json',voices)
    elif problem=='source':write_json(previous/'source.json',{'path':'other'})
    elif problem=='transcript':write_json(previous/'transcript.json',[])
    elif problem=='configuration':cfg=cfg.model_copy(update={'target_seconds':46})
    elif problem=='story':story=story.model_copy(update={'title':'其他故事'})
    elif problem=='future':plan['clips'][0]['beats'][0]['evidence_ids']=[0,3]
    write_json(previous/'plan-timed.json',plan)
    resumed=short_voices.resume_reference_plan(video,lines,voices,story,cfg,current,100)
    if problem in {None,'current'}:
        assert resumed.model_dump()==plan
        assert read_json(current/'reference-resume.json')['status']=='requires_full_measurement_and_review'
        if problem=='current':assert Path(read_json(current/'reference-resume.json')['previous_plan']).parent==current
    else:assert resumed is None
    assert not (current/'automatic-review.json').exists() and not (current/'plan-approved.json').exists()
