"""Real media export with deterministic model/TTS fixtures, no external services."""
import json
import shutil
from pathlib import Path
import pytest
from autoclip.util import run, read_json, write_json, source_identity


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='requires FFmpeg')
def test_series_exports_resumes_and_invalidates_voice_version(tmp_path,monkeypatch):
    from autoclip import series, planning, render
    from autoclip.config import Settings
    from autoclip.branding import BrandSettings
    source=tmp_path/'素材';source.mkdir()
    video=source/'第一章.mp4'
    run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=navy:s=320x180:r=25:d=48',
         '-f','lavfi','-i','sine=frequency=220:duration=48','-c:v','libx264','-preset','ultrafast',
         '-c:a','aac','-shortest',video])
    video.with_suffix('.srt').write_text('\n\n'.join(f'{i+1}\n00:00:{i*12+1:02},000 --> 00:00:{i*12+10:02},000\n有人提出问题，对方作了回应。' for i in range(4)))
    voice=tmp_path/'voice.mp3'
    run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=10',voice])
    source_id=source_identity(video)
    bank={'series':'测试剧','model':{'id':'test'},'dataset_id':'bank','revision':1,'characters':[
        {'name':'甲','samples':[{'id':'known','source_id':'source','source':source_id,'start':1.,'end':4.,
                              'embedding':[1.,0.],'status':'human_confirmed'}]}]}
    manifest={'series':'测试剧','model':bank['model'],'dataset_id':'samples','sources':[{'id':'source','source':source_id}],
        'samples':[{'id':f's{i}','source_id':'source','start':i*12+1.,'end':i*12+4.,'embedding':[1.,0.]} for i in range(4)]}
    library=tmp_path/'library.json';voice_manifest=tmp_path/'manifest.json'
    write_json(library,bank);write_json(voice_manifest,manifest)
    cfg=Settings(duration_profile='long',narrative_mode='story_first',target_seconds=300,min_seconds=30,
                 max_seconds=600,opening='original',vision=True,oral_edit=False,conflict_brief=False,portrait=False)
    monkeypatch.setattr(series.Settings,'load',lambda _:cfg.model_copy(deep=True))
    monkeypatch.setattr(series.BrandSettings,'load',lambda _:BrandSettings(watermark=tmp_path/'unused.png',enabled=False))
    monkeypatch.setattr(render,'synthesize',lambda *a:voice)
    def research(video,root,*a):write_json(root/'research.json',{'events':[]})
    def analyze(video,root,*a):write_json(root/'analysis.json',[]);return []
    monkeypatch.setattr(series,'prepare_research',research)
    monkeypatch.setattr(series,'analyze',analyze)
    monkeypatch.setattr(planning,'prepare_conflict_brief',lambda *a,**k:{})
    monkeypatch.setattr(planning,'brief_context',lambda _: {})
    calls=[]
    class Model:
        def __init__(self,*args):self.calls=0;self.client=self
        def close(self):pass
        def usage(self):return {'calls_this_run':self.calls}
        def json(self,system,prompt,images=None):
            self.calls+=1;calls.append(system)
            if '先定章节' in prompt:
                return {'title':'一场问答的经过','angle':'问答','hook':'事情如何展开','opening_start':0,'opening_end':12,
                    'opening_reason':'开场问答','chapters':[{'title':'事情经过','start':12,'end':48,'purpose':'顺叙交代'}]}
            if '只安排第' in prompt:
                return {'beats':[{'mode':'narration' if i%2 else 'original','start':i*12,'end':(i+1)*12,
                    'instruction':'交代问答','evidence_ids':[i],'target_chars':45 if i%2 else 0} for i in range(1,4)]}
            if '本次只写第' in prompt:
                return {'items':[{'id':i,'text':'事情从这次问答展开。有人提出问题，对方随后作了回应，双方的谈话仍在继续。','evidence_ids':[i]} for i in (1,3)]}
            if '原片画面观察员' in system:
                times=json.JSONDecoder().raw_decode(prompt.split('\ntimes=',1)[1])[0]
                return {'frames':[{'frame_id':i,'visible':'测试素材中的颜色画面'} for i in range(len(times))]}
            if '\nunits=' in prompt:
                group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
                return {'issues':[],'units':[{'id':u['id'],'verdict':'supported','reason':'测试证据相符',
                    'evidence_ids':u.get('evidence_ids',[0]),'frame_ids':[0],'identity_basis':'no_identity_claim'} for u in group]}
            raise AssertionError(system)
    monkeypatch.setattr(series,'LLM',Model)
    output=tmp_path/'project'
    result=series.run_series(source,output,series='测试剧',library=library,voice_manifest=voice_manifest,test_sample=True)
    assert result['status']=='completed',result
    result_video=Path(result['episodes'][0]['video']);assert result_video.is_file()
    assert list((output/'episodes/成片').glob('*.mp4'))
    assert (output/'project-report.json').exists()
    exported=read_json(output/'episodes/成片/清单.json')[0]
    assert exported['task_kind']=='test_sample' and exported['file'].startswith('测试样片_')
    assert '不是正式长片交付' in (output/'episodes/成片/最终解说稿.md').read_text()
    assert read_json(Path(result['episodes'][0]['run'])/'delivery-check.json')['passed']
    # A valid short export must still be rejected by the production gate even
    # when its cached metadata falsely declares a 20-minute duration.
    production=cfg.model_copy(update=series.PRODUCTION_DURATION)
    with pytest.raises(ValueError,match='本集未完成'):
        series.check_delivery({'video':str(result_video),'duration':1200.},production,tmp_path/'production-check')
    rejected=read_json(tmp_path/'production-check/delivery-check.json')
    assert not rejected['passed'] and rejected['actual_seconds']<60
    before=result_video.read_bytes();count=len(calls)
    again=series.run_series(source,output,test_sample=True)
    assert again['status']=='completed' and len(calls)==count
    assert result_video.read_bytes()==before
    assert not list(output.rglob('development-review.json'))
    approval=read_json(Path(result['episodes'][0]['run'])/'automatic-review.json')
    assert approval['mode']=='automatic_source_review' and approval['passed']
    # A global narration policy update must re-audit cached long-form work,
    # while preserving the draft, source analysis and valid media bytes.
    count=len(calls)
    monkeypatch.setattr(series,'AUDIENCE_STYLE_POLICY','audience-next-test')
    refreshed=series.run_series(source,output,test_sample=True)
    assert refreshed['status']=='completed',refreshed
    assert len(calls)>count and all(any(role in call for role in ('审核员','画面观察员')) for call in calls[count:])
    assert result_video.read_bytes()==before
    # A review algorithm update must refresh the persisted evidence as well as
    # the approval, even when source, library and run directory do not change.
    from autoclip import automatic
    from autoclip.util import digest
    monkeypatch.setattr(automatic,'VERSION','review-next-test')
    monkeypatch.setattr(series,'REVIEW_VERSION','review-next-test')
    updated=series.run_series(source,output,test_sample=True)
    assert updated['status']=='completed',updated
    run_root=Path(updated['episodes'][0]['run'])
    assert run_root==Path(result['episodes'][0]['run'])
    persisted=read_json(run_root/'voice-evidence.json')
    approval=read_json(run_root/'automatic-review.json')
    assert persisted['version']==approval['version']=='review-next-test'
    assert approval['voice_digest']==digest(persisted)
    # Changing the scene gate rechecks the saved draft, without rebuilding source analysis.
    count=len(calls)
    monkeypatch.setattr(automatic,'SCENE_OBSERVATION_POLICY','scene-next-test')
    monkeypatch.setattr(series,'SCENE_OBSERVATION_POLICY','scene-next-test')
    rechecked=series.run_series(source,output,test_sample=True)
    assert rechecked['status']=='completed',rechecked
    assert len(calls)>count and all(any(role in call for role in ('审核员','画面观察员')) for call in calls[count:])
    assert read_json(run_root/'automatic-review.json')['scene_observation_policy']=='scene-next-test'
    assert result_video.read_bytes()==before
    bank['revision']=2;bank['characters'][0]['name']='乙';write_json(library,bank)
    # Prepare-only still records the impact and retires old approvals from current display.
    series.run_series(source,output,stage='prepare',test_sample=True)
    assert read_json(output/'reference-impact.json')['affected_episodes']==[str(video)]
    assert all(read_json(p)['stage']!='已完成' for p in (output/'episodes').glob('*/status.json'))
    assert result_video.read_bytes()==before
    count=len(calls)
    resumed=series.run_series(source,output,test_sample=True)
    assert resumed['status']=='completed',resumed
    resumed_root=Path(resumed['episodes'][0]['run'])
    assert resumed_root!=run_root and (resumed_root/'reference-resume.json').exists()
    assert len(calls)>count and all(any(role in call for role in ('审核员','画面观察员')) for call in calls[count:])
    latest=read_json(resumed_root/'automatic-review.json')
    assert latest['passed'] and latest['voice_digest']==digest(read_json(resumed_root/'voice-evidence.json'))
    assert result_video.read_bytes()==before
    bank['revision']=3;bank['characters'][0]['name']='丙';write_json(library,bank)
    count=len(calls)
    third=series.run_series(source,output,test_sample=True)
    assert third['status']=='completed',third
    third_root=Path(third['episodes'][0]['run'])
    assert Path(read_json(third_root/'reference-resume.json')['previous_plan']).parent==resumed_root
    assert len(calls)>count and all(any(role in call for role in ('审核员','画面观察员')) for call in calls[count:])
    # A service failure after a measured repair resumes that current draft,
    # not the older pre-enrollment reference, and successful reruns stay cached.
    real_complete=series.complete_long_plan
    attempts=[]
    fixed_text='有人提出问题，对方随后作了回应，谈话还在继续。'
    def interrupt_after_measurement(v,plan,rows,voices,root,*args,**kwargs):
        attempts.append(plan.model_dump())
        if len(attempts)==1:
            plan.clips[0].beats[1].text=fixed_text
            write_json(root/'plan-timed.json',plan.model_dump())
            raise RuntimeError('测试服务中断')
        assert plan.clips[0].beats[1].text==fixed_text
        return real_complete(v,plan,rows,voices,root,*args,**kwargs)
    monkeypatch.setattr(series,'complete_long_plan',interrupt_after_measurement)
    monkeypatch.setattr(series,'AUDIENCE_STYLE_POLICY','resume-after-service-test')
    assert series.run_series(source,output,test_sample=True)['status']=='partial_failure'
    count=len(calls)
    continued=series.run_series(source,output,test_sample=True)
    assert continued['status']=='completed',continued
    assert len(calls)>count and all(any(role in call for role in ('审核员','画面观察员')) for call in calls[count:])
    assert read_json(third_root/'plan-approved.json')['clips'][0]['beats'][1]['text']==fixed_text
    count=len(calls)
    assert series.run_series(source,output,test_sample=True)['status']=='completed'
    assert len(calls)==count and len(attempts)==2
