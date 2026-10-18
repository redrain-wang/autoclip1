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
            if '\nunits=' in prompt:
                group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
                return {'passed':True,'issues':[],'units':[{'id':u['id'],'verdict':'supported','reason':'测试证据相符',
                    'evidence_ids':u.get('evidence_ids',[0]),'frame_ids':[0],'identity_basis':'no_identity_claim'} for u in group]}
            raise AssertionError(system)
    monkeypatch.setattr(series,'LLM',Model)
    output=tmp_path/'project'
    result=series.run_series(source,output,series='测试剧',library=library,voice_manifest=voice_manifest)
    assert result['status']=='completed',result
    result_video=Path(result['episodes'][0]['video']);assert result_video.is_file()
    assert list((output/'episodes/成片').glob('*.mp4'))
    assert (output/'project-report.json').exists()
    before=result_video.read_bytes();count=len(calls)
    again=series.run_series(source,output)
    assert again['status']=='completed' and len(calls)==count
    assert result_video.read_bytes()==before
    assert not list(output.rglob('development-review.json'))
    approval=read_json(Path(result['episodes'][0]['run'])/'automatic-review.json')
    assert approval['mode']=='automatic_source_review' and approval['passed']
    # A review algorithm update must refresh the persisted evidence as well as
    # the approval, even when source, library and run directory do not change.
    from autoclip import automatic
    from autoclip.util import digest
    monkeypatch.setattr(automatic,'VERSION','review-next-test')
    monkeypatch.setattr(series,'REVIEW_VERSION','review-next-test')
    updated=series.run_series(source,output)
    assert updated['status']=='completed',updated
    run_root=Path(updated['episodes'][0]['run'])
    assert run_root==Path(result['episodes'][0]['run'])
    persisted=read_json(run_root/'voice-evidence.json')
    approval=read_json(run_root/'automatic-review.json')
    assert persisted['version']==approval['version']=='review-next-test'
    assert approval['voice_digest']==digest(persisted)
    bank['revision']=2;bank['characters'][0]['name']='乙';write_json(library,bank)
    # Prepare-only still records the impact and retires old approvals from current display.
    series.run_series(source,output,stage='prepare')
    assert read_json(output/'reference-impact.json')['affected_episodes']==[str(video)]
    assert all(read_json(p)['stage']!='已完成' for p in (output/'episodes').glob('*/status.json'))
    assert result_video.read_bytes()==before
