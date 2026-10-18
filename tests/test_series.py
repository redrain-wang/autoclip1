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


@pytest.mark.parametrize('failed',[False,True])
def test_production_voice_uses_full_scan_at_existing_match_threshold(tmp_path,monkeypatch,failed):
    video=tmp_path/'01.mp4';video.write_bytes(b'video')
    library=tmp_path/'library.json';library.write_text('{}')
    output=tmp_path/'scan';calls=[]
    def run(args,**kwargs):
        calls.append(args)
        write_json(output/'manifest.json',{'summary':{'failed':[{'episode':'01'}] if failed else []},'samples':[]})
    monkeypatch.setattr(module,'run',run)
    if failed:
        with pytest.raises(ValueError,match='部分扫描结果'):
            module.scan_production_audio(video,library,output,tmp_path/'model')
    else:
        assert module.scan_production_audio(video,library,output,tmp_path/'model')['samples']==[]
    args=calls[0]
    assert args[3]=='voice-scan-series' and args[4]==video
    assert args[args.index('--threshold')+1]=='0.65'
    assert args[args.index('--margin')+1]=='0.10'


def test_partial_production_scan_is_retried_before_reusing_complete_cache(tmp_path,monkeypatch):
    video=tmp_path/'01.mp4';video.write_bytes(b'video')
    output=tmp_path/'scan'
    write_json(output/'manifest.json',{'summary':{'failed':[{'episode':'01'}]},'samples':[]})
    calls=[]
    def run(*args,**kwargs):
        calls.append(1)
        write_json(output/'manifest.json',{'summary':{'failed':[]},'samples':[{'id':'complete'}]})
    monkeypatch.setattr(module,'run',run)
    for _ in range(2):
        assert module.scan_production_audio(video,tmp_path/'library.json',output)['samples']==[{'id':'complete'}]
    assert calls==[1]


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


@pytest.mark.parametrize('content_passes',[False,True])
def test_timing_repairs_leave_separate_bounded_content_allowance(tmp_path,monkeypatch,content_passes):
    plan=Plan(episode_summary='',genre='',style='',clips=[Clip(title='自动修复边界',angle='',hook='',beats=[
        Beat(mode='original' if i%2==0 else 'narration',start=i*15,end=(i+1)*15,
             text='' if i%2==0 else '说明当前事件的原因',evidence_ids=[i],reason='衔接') for i in range(4)])])
    lines=[Line(id=i,start=i*15+.5,end=(i+1)*15-.5,text='当前对白') for i in range(4)]
    cfg=Settings(opening='original',repair_attempts=1,min_seconds=30,max_seconds=180)
    measures=[];audits=[];edits=[]
    def measure(video,path,transcript,cfg,root):
        current=read_json(path);measures.append(current);write_json(root/'plan-timed.json',current)
        return {'issues':[{'beat':1,'code':'voice_alignment','message':'实测太长'}] if len(measures)==1 else [],
                'beats':[{'beat':i,'mode':'narration','source_seconds':10,'output_seconds':11} for i in range(4)]}
    def audit(*args):
        audits.append(1)
        passed=content_passes and len(audits)>1
        return {'passed':passed,'issues':[] if passed else [{'id':'beat-1','message':'新增动作无据'}]}
    class Model:
        def json(self,*args):
            edits.append(1)
            return {'changes':[{'id':'beat-1','text':'先交代事情缘由' if len(edits)==1 else '对方随后作出了回应'}]}
    monkeypatch.setattr(module,'measure_sample_plan',measure)
    monkeypatch.setattr(module,'audit_plan',audit)
    monkeypatch.setattr(module,'duration',lambda _:60)
    monkeypatch.setattr('autoclip.recut.cut_candidates',lambda *a,**k:[])
    if content_passes:
        assert module.complete_long_plan(tmp_path/'source',plan,lines,{},tmp_path,cfg,Model())[1]['passed']
    else:
        with pytest.raises(ValueError,match='自动修复达到上限'):
            module.complete_long_plan(tmp_path/'source',plan,lines,{},tmp_path,cfg,Model())
        assert not (tmp_path/'plan-approved.json').exists()
    assert len(measures)==3 and len(audits)==len(edits)==2
    assert read_json(tmp_path/'repair-budget.json')['used']=={'timing':1,'content':1}


def test_measured_overrun_uses_gap_before_rewriting_and_still_requires_audit(tmp_path,monkeypatch):
    from autoclip.repair import timing_report
    plan=Plan(episode_summary='',genre='',style='',clips=[Clip(title='配音空隙衔接',angle='',hook='',beats=[
        Beat(mode='original',start=0,end=12,evidence_ids=[0],reason='开场'),
        Beat(mode='narration',start=12,end=15,text='当前旁白只需要稍长一点的画面',evidence_ids=[1],reason='承接'),
        Beat(mode='original',start=18,end=30,evidence_ids=[2],reason='完整原声'),
        Beat(mode='narration',start=32,end=40,text='对方回应之后事情仍在继续发展',evidence_ids=[3],reason='收束')])])
    lines=[Line(id=i,start=a,end=b,text='完整对白') for i,(a,b) in enumerate([(1,11),(12.2,14.5),(18.2,29),(33,39)])]
    cfg=Settings(duration_profile='long',target_seconds=300,min_seconds=30,max_seconds=600,opening='original')
    measures=[];reviews=[]
    def measure(video,path,transcript,cfg,root):
        current=Plan.model_validate(read_json(path));measures.append(current)
        write_json(root/'plan-timed.json',current.model_dump())
        return timing_report(current.clips[0],{1:5.,3:6.},cfg)
    def audit(video,current,*args):
        reviews.append(current)
        return {'passed':True,'issues':[]}
    monkeypatch.setattr(module,'measure_sample_plan',measure)
    monkeypatch.setattr(module,'audit_plan',audit)
    class Model:
        def json(self,*args):pytest.fail('有合法空隙时不应改稿')
    final,report=module.complete_long_plan(tmp_path/'source.mp4',plan,lines,{},tmp_path,cfg,Model())
    assert report['passed'] and len(measures)==2 and len(reviews)==1
    assert final.clips[0].beats[1].end>15
    assert final.clips[0].beats[0]==plan.clips[0].beats[0]
    assert final.clips[0].beats[2]==plan.clips[0].beats[2]
    assert [b.text for b in final.clips[0].beats]==[b.text for b in plan.clips[0].beats]
    assert (tmp_path/'automatic-timing-0.json').exists()


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
