from pathlib import Path
import pytest
from autoclip import series as module
from autoclip.config import Settings
from autoclip.models import Line, Plan, Clip, Beat
from autoclip.util import write_json, read_json, source_identity


def test_stage_resume_rechecks_result_inputs_and_artifacts(tmp_path):
    calls=[]
    artifact=tmp_path/'artifact.json'
    def action():
        calls.append(1);write_json(artifact,{'v':len(calls)})
        return {'answer':len(calls)},[artifact]
    assert module.checkpoint(tmp_path,'one',{'input':1},action)['answer']==1
    assert module.checkpoint(tmp_path,'one',{'input':1},action)['answer']==1
    artifact.write_text('corrupted')
    assert module.checkpoint(tmp_path,'one',{'input':1},action)['answer']==2
    assert module.checkpoint(tmp_path,'one',{'input':2},action)['answer']==3
    marker=tmp_path/'stages/one.json';raw=read_json(marker);raw['result']['answer']=900;write_json(marker,raw)
    assert module.checkpoint(tmp_path,'one',{'input':2},action)['answer']==4


def test_failed_stage_is_retried_not_marked_completed(tmp_path):
    def fail():raise RuntimeError('service unavailable')
    with pytest.raises(RuntimeError):module.checkpoint(tmp_path,'one',{},fail)
    assert read_json(tmp_path/'stages/one.json')['status']=='failed'
    assert module.checkpoint(tmp_path,'one',{},lambda:('ready',[]))=='ready'


def test_discovery_naturally_sorts_and_excludes_output(tmp_path):
    for name in ['10.mp4','2.mp4','1.mp4']:(tmp_path/name).write_bytes(b'v')
    output=tmp_path/'output';output.mkdir();(output/'new.mp4').write_bytes(b'render')
    assert [p.name for p in module.discover(tmp_path,output)]==['1.mp4','2.mp4','10.mp4']


def test_only_voice_enrollment_requests_human_action(tmp_path,monkeypatch):
    source=tmp_path/'剧';source.mkdir();(source/'01.mp4').write_bytes(b'v')
    output=tmp_path/'project'
    monkeypatch.setattr(module.Settings,'load',lambda _:Settings(duration_profile='long',target_seconds=300,opening='original'))
    def ingest(video,base,cfg):
        base.mkdir(parents=True,exist_ok=True);write_json(base/'transcript.json',[])
        return base,[],100
    monkeypatch.setattr(module,'ingest_episode',ingest)
    def prepare(sources,transcripts,root,*args):
        write_json(root/'manifest.json',{'ready':True})
    monkeypatch.setattr(module,'prepare_audio_candidates',prepare)
    result=module.run_series(source,output)
    assert result['status']=='waiting_voice_confirmation'
    assert (output/'project.json').exists()
    assert not list(output.rglob('development-review.json'))


def test_auto_review_repairs_then_remeasures_without_human_record(tmp_path,monkeypatch):
    plan=Plan(episode_summary='',genre='',style='',clips=[Clip(title='测试标题',angle='',hook='',beats=[
        Beat(mode='original' if i%2==0 else 'narration',start=i*12,end=(i+1)*12,
             text='说明事情发生的原因' if i%2 else '',evidence_ids=[i],reason='剧情') for i in range(4)])])
    lines=[Line(id=i,start=i*12,end=(i+1)*12,text='对白') for i in range(4)]
    cfg=Settings(duration_profile='long',target_seconds=300,min_seconds=30,max_seconds=600,opening='original')
    measures=[];reviews=[]
    def measure(video,path,transcript,cfg,root):
        measures.append(read_json(path));write_json(root/'plan-timed.json',read_json(path))
        return {'issues':[]}
    def audit(video,plan,*args):
        reviews.append(plan.model_dump())
        return {'passed':len(reviews)>1,'issues':[] if len(reviews)>1 else [{'id':'beat-1','message':'主语错误'}]}
    monkeypatch.setattr(module,'measure_sample_plan',measure)
    monkeypatch.setattr(module,'audit_plan',audit)
    monkeypatch.setattr(module,'duration',lambda _:60)
    class Model:
        def json(self,*args):return {'changes':[{'id':'beat-1','text':'他先提出了办理事情的请求'}]}
    final,report=module.complete_long_plan(tmp_path/'video',plan,lines,{},tmp_path,cfg,Model())
    assert report['passed'] and len(measures)==len(reviews)==2
    assert final.clips[0].beats[1].text != plan.clips[0].beats[1].text
    assert not (tmp_path/'development-review.json').exists()


def test_unknown_review_exhausts_automatically_without_approval(tmp_path,monkeypatch):
    cfg=Settings(repair_attempts=0)
    plan=Plan(episode_summary='',genre='',style='',clips=[Clip(title='测试标题',angle='',hook='',beats=[
        Beat(mode='narration',start=i*10,end=i*10+10,text='未知人物不可强认',evidence_ids=[i],reason='test') for i in range(4)])])
    def measure(video,path,transcript,cfg,root):
        write_json(root/'plan-timed.json',read_json(path));return {'issues':[]}
    monkeypatch.setattr(module,'measure_sample_plan',measure)
    monkeypatch.setattr(module,'audit_plan',lambda *a:{'passed':False,'issues':[{'id':'beat-1','message':'unknown'}]})
    with pytest.raises(ValueError,match='自动修复达到上限'):
        module.complete_long_plan(tmp_path/'video',plan,[],{},tmp_path,cfg,None)
    assert not (tmp_path/'plan-approved.json').exists()


def test_chinese_episode_numbers_are_sorted_by_actual_number():
    names=['第十一集.mp4','第二集.mp4','第十集.mp4','第一集.mp4']
    assert sorted(names,key=module.episode_sort_key)==['第一集.mp4','第二集.mp4','第十集.mp4','第十一集.mp4']


def test_failed_episode_does_not_block_next_and_failed_run_can_resume(tmp_path,monkeypatch):
    source=tmp_path/'sources';source.mkdir()
    paths=[source/'01.mp4',source/'02.mp4']
    for path in paths:path.write_bytes(b'video')
    library=tmp_path/'bank.json'
    write_json(library,{'series':'test','model':{},'dataset_id':'lib','revision':1,
        'characters':[{'name':'甲','samples':[{'status':'human_confirmed','source':source_identity(paths[0])}]}]})
    manifest=tmp_path/'manifest.json'
    write_json(manifest,{'model':{},'sources':[{'source':source_identity(p)} for p in paths]})
    monkeypatch.setattr(module.Settings,'load',lambda _:Settings(duration_profile='long',target_seconds=300,opening='original'))
    monkeypatch.setattr(module,'ingest_episode',lambda video,base,cfg:(base,[],100))
    calls=[]
    def episode(video,*args):
        calls.append(video.name)
        if video.name=='01.mp4':raise RuntimeError('temporary error')
        return {'source':str(video),'status':'completed','calls_this_run':0}
    monkeypatch.setattr(module,'run_episode',episode)
    monkeypatch.setattr(module,'export_finished',lambda _:None)
    monkeypatch.setattr(module,'build_report',lambda _:None)
    for _ in range(2):
        result=module.run_series(source,tmp_path/'project',series='test',library=library,voice_manifest=manifest)
        assert result['status']=='partial_failure'
        assert [r['status'] for r in result['episodes']]==['failed','completed']
    assert calls==['01.mp4','02.mp4','01.mp4','02.mp4']
