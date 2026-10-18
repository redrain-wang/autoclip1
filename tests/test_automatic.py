from pathlib import Path
import pytest

from autoclip.automatic import link_voice_evidence, assess_batch, repair_plan, assert_approval
from autoclip.models import Beat, Clip, Plan, Line
from autoclip.config import Settings
from autoclip.util import source_identity, digest


def voice_fixture(tmp_path):
    video = tmp_path/'01.mp4'; video.write_bytes(b'video')
    source = source_identity(video)
    sample = {'id':'a','source_id':'source','source':source,'start':0.,'end':3.,
              'embedding':[1.,0.],'status':'human_confirmed'}
    bank = {'series':'测试剧','model':{'id':'fixture'},'dataset_id':'bank','revision':1,
            'characters':[{'name':'甲','samples':[sample]}]}
    manifest = {'series':'测试剧','model':bank['model'],'dataset_id':'batch',
                'sources':[{'id':'source','source':source}],
                'samples':[{'id':'new','source_id':'source','start':0.,'end':3.,'embedding':[1.,0.],
                            'status':'suggested','name':'旧错名'}]}
    return video,bank,manifest


def test_voice_link_rematches_current_library_and_invalidates_changed_reference(tmp_path):
    video,bank,manifest = voice_fixture(tmp_path)
    lines = [Line(id=0,start=0,end=3,text='我来办')]
    first = link_voice_evidence(video,lines,bank,manifest)
    assert first['lines'][0]['samples'][0]['name'] == '甲'
    bank['characters'][0]['name'] = '乙'
    second = link_voice_evidence(video,lines,bank,manifest)
    assert second['library_digest'] != first['library_digest']
    assert second['lines'][0]['samples'][0]['name'] == '乙'
    assert manifest['samples'][0]['name'] == '旧错名'
    video.write_bytes(b'changed')
    with pytest.raises(ValueError):link_voice_evidence(video,lines,bank,manifest)


def test_conflicting_speakers_are_not_promoted_to_named_identity(tmp_path):
    video,bank,manifest = voice_fixture(tmp_path)
    bank['characters'].append({'name':'乙','samples':[{**bank['characters'][0]['samples'][0],
        'id':'b','embedding':[0.,1.]}]})
    manifest['samples'].append({**manifest['samples'][0],'id':'other','embedding':[0.,1.]})
    result = link_voice_evidence(video,[Line(id=0,start=0,end=3,text='多人问答')],bank,manifest)
    assert result['lines'][0]['status'] == 'conflict'


def review_fixture():
    units = [{'id':'beat-1','text':'他接下任务','end':10}]
    lines = [Line(id=1,start=1,end=3,text='我去办'),Line(id=2,start=4,end=5,text='好')]
    raw = {'passed':True,'issues':[],'units':[{'id':'beat-1','verdict':'supported','reason':'回应明确',
        'evidence_ids':[1,2],'frame_ids':[0],'identity_basis':'explicit_address_response'}]}
    return raw,units,lines,1,[]


@pytest.mark.parametrize('bad',['missing','duplicate','unknown_id','unknown_frame','string_passed'])
def test_malformed_automatic_review_never_passes(bad):
    args = review_fixture(); raw = args[0]
    if bad=='missing':raw['units']=[]
    if bad=='duplicate':raw['units']*=2
    if bad=='unknown_id':raw['units'][0]['evidence_ids']=[99]
    if bad=='unknown_frame':raw['units'][0]['frame_ids']=[3]
    if bad=='string_passed':raw['passed']='true'
    with pytest.raises(ValueError):assess_batch(*args)


@pytest.mark.parametrize('bad',['unknown','contradiction','future','voice_conflict','no_voice'])
def test_semantic_or_identity_uncertainty_blocks_approval(bad):
    raw,units,lines,count,voices = review_fixture()
    if bad=='unknown':raw['units'][0]['verdict']='unknown'
    if bad=='contradiction':raw['issues']=[{'id':'beat-1','message':'说法有误'}]
    if bad=='future':
        units[0]['end']=4
        raw['units'][0]['verdict']='unknown'
    if bad in {'voice_conflict','no_voice'}:
        raw['units'][0]['identity_basis']='voice_and_context'
        if bad=='voice_conflict':voices=[{'line_id':1,'status':'conflict'}]
    assert assess_batch(raw,units,lines,count,voices)


def test_valid_review_is_accepted():
    assert assess_batch(*review_fixture()) == []


def test_supported_verdict_with_future_citation_requires_recheck_instead_of_script_rewrite():
    args=review_fixture();args[1][0]['end']=4
    with pytest.raises(ValueError,match='声称有据但引用未来对白'):
        assess_batch(*args)


def test_review_issues_only_affect_the_named_unit():
    raw,units,lines,count,voices=review_fixture()
    units.append({'id':'beat-2','text':'另一段合理口播','end':10})
    raw['units'].append({**raw['units'][0],'id':'beat-2'})
    raw['passed']=False
    raw['issues']=[{'id':'beat-1','message':'只修这一段的主语'}]
    assert assess_batch(raw,units,lines,count,voices)==raw['issues']
    raw['issues'][0]['id']='title'
    with pytest.raises(ValueError,match='本批次以外'):assess_batch(raw,units,lines,count,voices)


def test_contradictory_response_without_specific_issue_is_not_a_content_verdict():
    args=review_fixture();args[0]['passed']=False
    with pytest.raises(ValueError,match='未指出'):assess_batch(*args)


def test_named_subject_cannot_claim_there_is_no_identity_assertion():
    args=review_fixture();unit=args[0]['units'][0]
    unit['identity_basis']='no_identity_claim';unit['subjects']=['甲']
    assert assess_batch(*args)


def sample_plan():
    return Plan(episode_summary='',genre='',style='',clips=[Clip(title='测试标题',angle='原因',hook='为何',
        beats=[Beat(mode='narration',start=i*10,end=(i+1)*10,text='说明当前事件的原因',evidence_ids=[i],reason='衔接') for i in range(4)])])


def test_repair_only_touches_failed_units(tmp_path):
    plan = sample_plan()
    class Model:
        def json(self,*args):return {'changes':[{'id':'beat-1','text':'说明这个人已经说出的理由'}]}
    revised = repair_plan(plan,[{'id':'beat-1','message':'纠错'}],[],{},tmp_path,Settings(),Model(),0)
    assert revised.clips[0].beats[1].text != plan.clips[0].beats[1].text
    for i in (0,2,3):assert revised.clips[0].beats[i] == plan.clips[0].beats[i]
    class Invalid:
        def json(self,*args):return {'changes':[{'id':'beat-2','text':'不该修改这里'}]}
    with pytest.raises(ValueError):repair_plan(plan,[{'id':'beat-1','message':'纠错'}],[],{},tmp_path,Settings(),Invalid(),0)


def test_invalid_empty_repair_is_retried_transactionally(tmp_path):
    plan=sample_plan();before=plan.model_dump();prompts=[]
    class Model:
        def json(self,system,prompt):
            prompts.append(prompt)
            return {'changes':[{'id':'beat-1','text':'' if len(prompts)==1 else '这个人说明了办理事情的理由'}]}
    revised=repair_plan(plan,[{'id':'beat-1','message':'主语应准确'}],[],{},tmp_path,Settings(),Model(),0)
    assert len(prompts)==2 and 'narration text required' in prompts[1]
    assert plan.model_dump()==before and revised.clips[0].beats[1].text
    for i in (0,2,3):assert revised.clips[0].beats[i]==plan.clips[0].beats[i]


def test_single_unit_repairs_preserve_successful_edits_and_exclude_future_evidence(tmp_path):
    import json
    plan=sample_plan();before=plan.model_dump();targets=[];prompts=[]
    lines=[Line(id=i,start=i*10+.5,end=(i+1)*10-.5,text='当前对白') for i in range(4)]
    class Model:
        def json(self,system,prompt):
            target=json.JSONDecoder().raw_decode(prompt.split('\ntarget=',1)[1])[0]
            targets.append(target['id']);prompts.append(prompt)
            if target['id']=='beat-1':return {'changes':[{'id':'beat-1','text':'先交代这次事情的缘由'}]}
            return {'changes':[{'id':'beat-2','text':'再交代对方随后作出的回应',
                                'evidence_ids':[3] if len(targets)==2 else [2]}]}
    final=repair_plan(plan,[{'id':'beat-1','message':'明确起因'},{'id':'beat-2','message':'说明回应'}],
                      lines,{},tmp_path,Settings(),Model(),0)
    assert targets==['beat-1','beat-2','beat-2']
    assert '先交代这次事情的缘由' in prompts[-1] and '未允许证据' in prompts[-1]
    assert final.clips[0].beats[2].evidence_ids==[2] and plan.model_dump()==before
    assert final.clips[0].beats[0]==plan.clips[0].beats[0]
    assert final.clips[0].beats[3]==plan.clips[0].beats[3]


@pytest.mark.parametrize('cited',[None,[{'id':1}]])
def test_malformed_repair_evidence_has_bounded_retries(tmp_path,cited):
    calls=[]
    class Model:
        def json(self,*args):
            calls.append(args);return {'changes':[{'id':'beat-1','text':'说明原因','evidence_ids':cited}]}
    with pytest.raises(ValueError,match='整数编号列表'):
        repair_plan(sample_plan(),[{'id':'beat-1','message':'修订'}],[],{},tmp_path,
                    Settings(repair_attempts=1),Model(),0)
    assert len(calls)==2


def test_original_note_can_change_without_rewriting_original_audio(tmp_path):
    plan=sample_plan();plan.clips[0].beats[0].mode='original';plan.clips[0].beats[0].text=''
    class Model:
        def json(self,*args):return {'changes':[{'id':'original-0','reason':'保留完整问答，不附加人物判断'}]}
    revised=repair_plan(plan,[{'id':'original-0','message':'内部说明不准确'}],[],{},tmp_path,Settings(),Model(),0)
    before=plan.clips[0].beats[0].model_dump();after=revised.clips[0].beats[0].model_dump()
    before.pop('reason');after.pop('reason')
    assert before==after


@pytest.mark.parametrize('problem',['overlap','future','cut'])
def test_repaired_timeline_is_validated_before_accepting_patch(tmp_path,problem):
    plan=sample_plan();plan.clips[0].beats[0].mode='original';plan.clips[0].beats[0].text=''
    lines=[Line(id=i,start=i*10+.5,end=(i+1)*10-.5,text='当前对白') for i in range(4)]
    cfg=Settings(opening='original',target_seconds=100,min_seconds=30,max_seconds=180,repair_attempts=0)
    row={'id':'beat-1','text':'说明事情发生的原因'}
    if problem=='overlap':row['start']=0
    elif problem=='future':row['evidence_ids']=[1,3]
    else:
        lines[0].end=11
        row={'id':'original-0','end':10}
    class Model:
        def json(self,*args):return {'changes':[row]}
    with pytest.raises(ValueError):
        repair_plan(plan,[{'id':row['id'],'message':'修复'}],lines,{},tmp_path,cfg,Model(),0,source_seconds=40)


def test_review_protocol_retry_keeps_script_and_supplies_previous_shown_context(tmp_path,monkeypatch):
    import json
    import autoclip.automatic as module
    from autoclip.util import write_json
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    frame=tmp_path/'frame.jpg';frame.write_bytes(b'frame')
    monkeypatch.setattr(module,'contact_sheet',lambda *a:frame)
    plan=sample_plan()
    plan.clips[0].beats[0].mode='original';plan.clips[0].beats[0].text=''
    plan.clips[0].beats[0].reason='内部猜测辛某起身，不属于观众听到的内容'
    before=plan.model_dump()
    lines=[Line(id=i,start=i*10+.5,end=(i+1)*10-.5,text='当前对白') for i in range(4)]
    voices={'source':source_identity(video),'transcript_digest':digest([s.model_dump() for s in lines]),'lines':[]}
    groups=[];prompts=[]
    class Model:
        def json(self,system,prompt,images):
            prompts.append(prompt)
            group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0];groups.append(group)
            if len(prompts)==1:return {'passed':False,'issues':[],'units':[]}
            return {'passed':True,'issues':[],'units':[{'id':u['id'],'verdict':'supported',
                'reason':'给定字幕与画面相符','evidence_ids':[0],'frame_ids':[0],
                'identity_basis':'no_identity_claim','subjects':[]} for u in group]}
    report=module.audit_plan(video,plan,lines,voices,tmp_path,Settings(),Model())
    assert report['passed'] and len(prompts)==3
    assert '上次审核响应' in prompts[1] and plan.model_dump()==before
    assert all('内部猜测辛某' not in prompt for prompt in prompts)
    last=next(u for group in groups for u in group if u['id']=='beat-3')
    assert last['shown_evidence']==[0,1,2]
    assert list(tmp_path.glob('automatic-review/*/batch-0-protocol-0.json'))


def test_automatic_approval_binds_exact_plan_voice_and_frames(tmp_path):
    from autoclip.identity import file_hash
    video = tmp_path/'a.mp4';video.write_bytes(b'video')
    frame = tmp_path/'a.jpg';frame.write_bytes(b'frame')
    plan=sample_plan();lines=[];voices={'library_digest':'first'}
    from autoclip.automatic import VERSION
    report={'version':VERSION,'mode':'automatic_source_review','passed':True,'issues':[],'source':source_identity(video),
        'plan_digest':digest(plan.model_dump()),'transcript_digest':digest([]),'voice_digest':digest(voices),
        'frames':[{'path':str(frame),'sha256':file_hash(frame)}]}
    assert_approval(report,video,plan,lines,voices)
    with pytest.raises(ValueError):assert_approval({**report,'version':'old-review'},video,plan,lines,voices)
    with pytest.raises(ValueError):assert_approval(report,video,plan,lines,{'library_digest':'new'},)
    frame.write_bytes(b'edited')
    with pytest.raises(ValueError):assert_approval(report,video,plan,lines,voices)
