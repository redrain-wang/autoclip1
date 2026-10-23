"""Long-film identity follow-up pauses before more paid writing."""
from pathlib import Path

import pytest

from autoclip import series, short_voices
from autoclip.branding import BrandSettings
from autoclip.util import digest, read_json, source_identity, write_json
from test_short_voices import followup_fixture


def failed_run(tmp_path, monkeypatch):
    video,library,bank,cfg,base,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    cfg=cfg.model_copy(update={'duration_profile':'long'})
    root=base/'runs'/'failed'
    for name,value in [('source.json',source_identity(video)),('settings.json',cfg.model_dump(mode='json')),
                       ('voice-evidence.json',voices),('plan-timed.json',read_json(story/'plan-timed.json')),
                       ('automatic-review.json',report)]:
        write_json(root/name,value)
    write_json(base/'status.json',{'status':'failed','run':str(root)})
    return video,library,bank,cfg,base,lines,voices,root,report,calls


def test_protocol_failure_retires_legacy_stale_voice_task(tmp_path,monkeypatch):
    video,library,bank,cfg,base,lines,voices,root,report,calls=failed_run(tmp_path,monkeypatch)
    task=series.failed_episode_voice_followup(video,base,lines,voices,cfg,library)
    assert task
    write_json(base/'status.json',{'status':'waiting_voice_confirmation','run':str(root),
        'error':'自动审核批次84格式或引用校验失败，未改写稿件：画面编号错误'})
    assert series.failed_episode_voice_followup(video,base,lines,voices,cfg,library) is None
    assert read_json(base/'voice-followup.json')['status']=='superseded'
    assert len(calls)==1 and read_json(library)==bank


@pytest.mark.parametrize('error,checkpoint_only',[
    ('已达到本次 API 调用次数上限；可续跑',False),
    ('已达到本次 API 调用次数上限；可续跑',True),
    ('模型调用失败，已重试：Connection refused',False)])
def test_service_interruption_resumes_review_instead_of_requesting_human_voice_confirmation(tmp_path,monkeypatch,error,checkpoint_only):
    video,library,bank,cfg,base,lines,voices,root,report,calls=failed_run(tmp_path,monkeypatch)
    assert series.failed_episode_voice_followup(video,base,lines,voices,cfg,library)
    status={'status':'waiting_voice_confirmation','run':str(root)}
    if checkpoint_only:write_json(root/'stages/approval.json',{'status':'failed','error':error})
    else:status['error']=error
    write_json(base/'status.json',status)
    assert series.failed_episode_voice_followup(video,base,lines,voices,cfg,library) is None
    assert read_json(base/'voice-followup.json')['status']=='superseded'
    assert len(calls)==1 and read_json(library)==bank
    assert not (root/'plan-approved.json').exists()


@pytest.mark.parametrize('state,matching',[('running',True),('completed',False),('completed',True)])
def test_followup_requires_latest_review_to_finish(tmp_path,monkeypatch,state,matching):
    video,library,bank,cfg,base,lines,voices,root,report,calls=failed_run(tmp_path,monkeypatch)
    write_json(root/'automatic-review-state.json',{'status':state,
        'plan_digest':report['plan_digest'] if matching else 'new-plan'})
    task=series.failed_episode_voice_followup(video,base,lines,voices,cfg,library)
    assert bool(task)==(state=='completed' and matching)
    assert read_json(library)==bank


@pytest.mark.parametrize('change',[None,'source','settings','voices','outside','facts_only'])
def test_failed_long_review_only_requests_current_identity_evidence(tmp_path,monkeypatch,change):
    video,library,bank,cfg,base,lines,voices,root,report,calls=failed_run(tmp_path,monkeypatch)
    if change=='source':write_json(root/'source.json',{'path':'different'})
    elif change=='settings':write_json(root/'settings.json',{})
    elif change=='voices':write_json(root/'voice-evidence.json',{})
    elif change=='outside':write_json(base/'status.json',{'status':'failed','run':str(root.parent.parent)})
    elif change=='facts_only':
        report['issues']=[{'id':'beat-1','message':'事实不符'}]
        write_json(root/'automatic-review.json',report)
    task=series.failed_episode_voice_followup(video,base,lines,voices,cfg,library)
    assert bool(task)==(change is None)
    assert read_json(library)==bank
    if task:
        assert task['sample_lines']==[1]
        write_json(base/'status.json',{'status':'waiting_voice_confirmation','run':str(root)})
        assert series.failed_episode_voice_followup(video,base,lines,voices,cfg,library)==task
        assert len(calls)==1
        assert not read_json(Path(task['review_root'])/'registry.json')['characters']


@pytest.mark.parametrize('snapshot',['valid','changed','outside'])
def test_followup_uses_exact_review_snapshot_after_next_timing_overwrites_plan(tmp_path,monkeypatch,snapshot):
    video,library,bank,cfg,base,lines,voices,root,report,calls=failed_run(tmp_path,monkeypatch)
    original=read_json(root/'plan-timed.json')
    newer=read_json(root/'plan-timed.json');newer['clips'][0]['beats'][1]['text']='另一次修订'
    write_json(root/'plan-timed.json',newer)
    audit=root/'automatic-review'/'batch' if snapshot!='outside' else tmp_path/'unrelated'
    write_json(audit/'plan.json',newer if snapshot=='changed' else original)
    report['batches']=[str(audit/'batch-0.json')];write_json(root/'automatic-review.json',report)
    task=series.failed_episode_voice_followup(video,base,lines,voices,cfg,library)
    assert bool(task)==(snapshot=='valid')
    assert read_json(root/'plan-timed.json')==newer


def test_public_long_command_pauses_merges_finished_human_reference_and_resumes(tmp_path,monkeypatch):
    from autoclip.voice_review import ReviewStore
    video,library,bank,fixture_cfg,unused,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    cfg=fixture_cfg.model_copy(update={'duration_profile':'long','opening':'narration'})
    output=tmp_path/'long-project'
    monkeypatch.setattr(series.Settings,'load',lambda _:cfg.model_copy(deep=True))
    monkeypatch.setattr(series.BrandSettings,'load',lambda _:BrandSettings(enabled=False,watermark=tmp_path/'none'))
    monkeypatch.setattr(series,'ingest_episode',lambda v,b,c:(b,lines,100))
    monkeypatch.setattr(series,'link_voice_evidence',lambda *a:voices)
    monkeypatch.setattr(series,'scan_production_audio',lambda *a:{'model':bank['model'],
        'sources':[{'source':source_identity(video)}]})
    monkeypatch.setattr(series,'build_report',lambda *a:None)
    monkeypatch.setattr(series,'export_finished',lambda *a:None)
    invocations=[]
    def episode(v,base,input_root,rows,seconds,effective,reference,*args):
        invocations.append(reference)
        root=base/'runs'/'failed'
        if len(invocations)>1:
            assert {p['name'] for p in reference['characters']}=={'甲','乙'}
            return {'source':str(v),'status':'completed','calls_this_run':0}
        for name,value in [('source.json',source_identity(v)),('settings.json',effective.model_dump(mode='json')),
                           ('voice-evidence.json',voices),('plan-timed.json',read_json(story/'plan-timed.json')),
                           ('automatic-review.json',report)]:write_json(root/name,value)
        write_json(base/'status.json',{'run':str(root)})
        raise ValueError('自动内容审核未通过')
    monkeypatch.setattr(series,'run_episode',episode)
    first=series.run_series(video,output,series=cfg.series,library=library,test_sample=True)
    assert first['status']=='waiting_voice_confirmation',first
    task=first['episodes'][0]['voice_confirmation']
    again=series.run_series(video,output,test_sample=True)
    assert again['status']=='waiting_voice_confirmation' and len(invocations)==1
    assert again['episodes'][0]['calls_this_run']==0
    assert read_json(output/'production-metrics.json')['waiting_voice_confirmation']==1
    store=ReviewStore(Path(task['review_root']))
    saved=store.save({'dataset_id':store.manifest['dataset_id'],'revision':0,
        'changes':{store.manifest['samples'][0]['id']:{'status':'named','name':'乙'}}})
    assert series.run_series(video,output,test_sample=True)['status']=='waiting_voice_confirmation'
    assert len(invocations)==1 and read_json(library)==bank
    store.finish({'dataset_id':store.manifest['dataset_id'],'revision':saved['revision']})
    assert series.run_series(video,output,test_sample=True)['status']=='completed'
    assert len(invocations)==2
    assert read_json(library.parent/'history'/f'{library.stem}-{digest(bank)}.json')==bank


@pytest.mark.parametrize('change',[None,'source','transcript','settings','history','brand','analysis','future','same_bank'])
def test_long_reference_resume_preserves_draft_but_never_approval(tmp_path,monkeypatch,change):
    from autoclip.models import Plan
    video,library,bank,cfg,base,lines,voices,old,report,calls=failed_run(tmp_path,monkeypatch)
    cfg=cfg.model_copy(update={'opening':'narration','min_seconds':1,'max_seconds':90,'target_seconds':45})
    inputs={'v':series.VERSION,'source':source_identity(video),'lines':[s.model_dump() for s in lines],
        'cfg':cfg.model_dump(mode='json'),'bank':'new-bank','manifest':'new-manifest','prior':'same-prior','brand':{'name':'brand'}}
    old_voices={**voices,'library_digest':'old-bank','manifest_digest':'old-manifest'}
    old_inputs={**inputs,'bank':'old-bank','manifest':'old-manifest'}
    previous=base/'runs'/digest(old_inputs);root=base/'runs'/digest(inputs);root.mkdir(parents=True)
    plan=read_json(old/'plan-timed.json')
    for b in plan['clips'][0]['beats'][2:]:b.update(mode='original',text='')
    for name,value in [('source.json',inputs['source']),('transcript.json',inputs['lines']),
        ('settings.json',inputs['cfg']),('voice-evidence.json',old_voices),('plan-timed.json',plan),
        ('plan-approved.json',plan),('automatic-review.json',{'passed':True})]:write_json(previous/name,value)
    def analysis():
        write_json(previous/'analysis.json',[]);write_json(previous/'research.json',{'notes':'model draft'})
        return [],[previous/'analysis.json',previous/'research.json']
    series.checkpoint(previous,'analysis',old_inputs,analysis)
    if change=='source':write_json(previous/'source.json',{})
    elif change=='transcript':write_json(previous/'transcript.json',[])
    elif change=='settings':write_json(previous/'settings.json',{})
    elif change=='history':inputs['prior']='different-prior'
    elif change=='brand':inputs['brand']={'name':'different-brand'}
    elif change=='analysis':write_json(previous/'research.json',{'notes':'changed'})
    elif change=='future':
        plan['clips'][0]['beats'][0]['evidence_ids']=[0,3];write_json(previous/'plan-timed.json',plan)
    elif change=='same_bank':inputs['bank']='old-bank'
    resumed=series.resume_long_reference(video,root,inputs,lines,voices,cfg,100)
    assert bool(resumed)==(change is None)
    if resumed:
        assert resumed==Plan.model_validate(plan)
        assert read_json(root/'reference-resume.json')['status']=='requires_full_measurement_and_review'
        assert read_json(root/'research.json')=={'notes':'model draft'}
    assert not (root/'automatic-review.json').exists() and not (root/'plan-approved.json').exists()


@pytest.mark.parametrize('change',[None,'source','transcript','settings','voices','root','future'])
def test_current_long_draft_resume_requires_same_inputs_and_reaudits(tmp_path,monkeypatch,change):
    from autoclip.models import Plan
    video,library,bank,cfg,base,lines,voices,old,report,calls=failed_run(tmp_path,monkeypatch)
    cfg=cfg.model_copy(update={'opening':'narration','min_seconds':1,'max_seconds':90,'target_seconds':45})
    inputs={'source':source_identity(video),'lines':[s.model_dump() for s in lines],
        'cfg':cfg.model_dump(mode='json'),'bank':'bank','prior':'prior'}
    root=base/'runs'/digest(inputs)
    saved=read_json(old/'plan-timed.json')
    for beat in saved['clips'][0]['beats'][2:]:beat.update(mode='original',text='')
    initial=Plan.model_validate(saved).model_copy(deep=True)
    saved['clips'][0]['beats'][1]['text']='已经自动修好的旁白。'
    for name,value in [('source.json',inputs['source']),('transcript.json',inputs['lines']),
        ('settings.json',inputs['cfg']),('voice-evidence.json',voices),('plan-timed.json',saved)]:
        write_json(root/name,value)
    if change in {'source','transcript','settings'}:write_json(root/(change+'.json'),{})
    elif change=='voices':write_json(root/'voice-evidence.json',{})
    elif change=='root':inputs['prior']='different'
    elif change=='future':
        saved['clips'][0]['beats'][0]['evidence_ids']=[0,3];write_json(root/'plan-timed.json',saved)
    resumed=series.resume_current_long_draft(root,inputs,lines,voices,cfg,100,initial)
    assert resumed.model_dump()==(saved if change is None else initial.model_dump())
    assert not (root/'plan-approved.json').exists()
    if change is None:assert read_json(root/'current-draft-resume.json')['status']=='requires_full_measurement_and_review'


@pytest.mark.parametrize('mismatch',[None,'base_plan_digest','voice_digest'])
def test_pending_repair_survives_service_stop_but_never_bypasses_review(tmp_path,monkeypatch,mismatch):
    from autoclip.models import Plan
    video,library,bank,cfg,base,lines,voices,old,report,calls=failed_run(tmp_path,monkeypatch)
    cfg=cfg.model_copy(update={'opening':'narration','min_seconds':1,'max_seconds':90,'target_seconds':45})
    inputs={'source':source_identity(video),'lines':[s.model_dump() for s in lines],'cfg':cfg.model_dump(mode='json')}
    root=base/'runs'/digest(inputs)
    timed=Plan.model_validate(read_json(old/'plan-timed.json'))
    for b in timed.clips[0].beats[2:]:b.mode='original';b.text=''
    patched=timed.model_copy(deep=True);patched.clips[0].beats[1].text='服务中断前已修好的当前段落。'
    pending={'base_plan_digest':digest(timed.model_dump()),'voice_digest':digest(voices),
        'plan':patched.model_dump(),'status':'requires_full_measurement_and_review'}
    if mismatch:pending[mismatch]='stale'
    for name,value in [('source.json',inputs['source']),('transcript.json',inputs['lines']),
        ('settings.json',inputs['cfg']),('voice-evidence.json',voices),('plan-timed.json',timed.model_dump()),
        ('pending-repair.json',pending)]:write_json(root/name,value)
    resumed=series.resume_current_long_draft(root,inputs,lines,voices,cfg,100,timed)
    assert resumed==(patched if mismatch is None else timed)
    assert not (root/'plan-approved.json').exists() and not (root/'automatic-review.json').exists()
