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


@pytest.mark.parametrize('passed',[False,True])
def test_new_unit_response_never_accepts_a_model_aggregation_switch(passed):
    from autoclip.automatic import UnitReviews
    raw=review_fixture()[0];raw['passed']=passed
    with pytest.raises(ValueError,match='Extra inputs'):
        UnitReviews.model_validate(raw)
    raw.pop('passed')
    assert len(UnitReviews.model_validate(raw).units)==1


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
    sheet_roots=[]
    def sheet(video,root,*args):
        sheet_roots.append(root)
        return frame
    monkeypatch.setattr(module,'contact_sheet',sheet)
    monkeypatch.setattr(module,'subtitle_evidence_sheet',sheet)
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
            return {'issues':[],'units':[{'id':u['id'],'verdict':'supported',
                'reason':'给定字幕与画面相符','evidence_ids':[0],'frame_ids':[0],
                'identity_basis':'no_identity_claim','subjects':[]} for u in group]}
    report=module.audit_plan(video,plan,lines,voices,tmp_path,Settings(),Model())
    assert report['passed'] and len(prompts)==4
    assert len(sheet_roots)==len(set(sheet_roots))==5
    assert all(len(group)<=3 for group in groups)
    assert [Path(p).name for p in report['batches']]==['batch-0.json','batch-3.json','batch-6.json']
    assert any('上次审核响应' in p for p in prompts[1:]) and plan.model_dump()==before
    assert any('逐项引用边界=' in p for p in prompts[1:])
    assert all('内部猜测辛某' not in prompt for prompt in prompts)
    last=next(u for group in groups for u in group if u['id']=='beat-3')
    assert last['shown_evidence']==[0,1,2]
    assert list(tmp_path.glob('automatic-review/*/batch-0-protocol-0.json'))


def test_automatic_approval_binds_exact_plan_voice_and_frames(tmp_path):
    from autoclip.identity import file_hash
    video = tmp_path/'a.mp4';video.write_bytes(b'video')
    frame = tmp_path/'a.jpg';frame.write_bytes(b'frame')
    plan=sample_plan();lines=[];voices={'library_digest':'first'}
    from autoclip.automatic import VERSION,METADATA_CAPTION_POLICY
    report={'version':VERSION,'mode':'automatic_source_review','passed':True,'issues':[],'source':source_identity(video),
        'plan_digest':digest(plan.model_dump()),'transcript_digest':digest([]),'voice_digest':digest(voices),
        'metadata_caption_policy':METADATA_CAPTION_POLICY,
        'frames':[{'path':str(frame),'sha256':file_hash(frame),'purpose':'metadata_captions'}]}
    assert_approval(report,video,plan,lines,voices)
    with pytest.raises(ValueError):assert_approval({**report,'version':'old-review'},video,plan,lines,voices)
    with pytest.raises(ValueError):assert_approval(report,video,plan,lines,{'library_digest':'new'},)
    frame.write_bytes(b'edited')
    with pytest.raises(ValueError):assert_approval(report,video,plan,lines,voices)


@pytest.mark.parametrize('broken_cut',[False,True])
def test_originals_have_separate_review_scope_but_real_cut_failures_still_block(tmp_path,monkeypatch,broken_cut):
    import json
    import autoclip.automatic as module
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    frame=tmp_path/'frame.jpg';frame.write_bytes(b'frame')
    monkeypatch.setattr(module,'contact_sheet',lambda *a:frame)
    monkeypatch.setattr(module,'subtitle_evidence_sheet',lambda *a:frame)
    plan=sample_plan()
    for i in (0,2):plan.clips[0].beats[i].mode='original';plan.clips[0].beats[i].text=''
    before=plan.model_dump()
    lines=[Line(id=i,start=i*10+.5,end=(i+1)*10-.5,text='当前对白') for i in range(4)]
    rows=[{'line_id':i,'status':'unknown','samples':[{'name':None,'status':'unknown',
        'candidates':[{'name':'RANK_ONLY_NOT_AN_IDENTITY','score':.2}]}]} for i in range(4)]
    voices={'source':source_identity(video),'transcript_digest':digest([s.model_dump() for s in lines]),'lines':rows}
    original_attempts=[]
    class Model:
        def json(self,system,prompt,images):
            assert 'RANK_ONLY_NOT_AN_IDENTITY' not in prompt
            timeline=json.JSONDecoder().raw_decode(prompt.split('\nviewer_timeline=',1)[1])[0]
            assert len(timeline)==len(plan.clips[0].beats)
            assert timeline[1]['text']==plan.clips[0].beats[1].text
            assert all('reason' not in row for row in timeline)
            group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
            original=any(u.get('mode')=='original' for u in group)
            if original:
                assert all(u.get('mode')=='original' and u['source_continuity']=='single_uncut_interval' for u in group)
                assert '\n声音候选=' not in prompt
                original_attempts.append(1)
            invalid_scope=original and len(original_attempts)==1
            return {'issues':[],'notes':[],
                'units':[{'id':u['id'],'verdict':'contradicted' if original and broken_cut else 'supported',
                    'reason':'切点截断对白' if original and broken_cut else '对白切点和承接完整',
                    'evidence_ids':u.get('evidence_ids',[0]),'frame_ids':[0],
                    'identity_basis':'insufficient' if invalid_scope else 'no_identity_claim',
                    'subjects':[]} for u in group]}
    report=module.audit_plan(video,plan,lines,voices,tmp_path,Settings(),Model())
    assert report['passed'] is (not broken_cut)
    assert len(original_attempts)==2 and plan.model_dump()==before
    assert list(tmp_path.glob('automatic-review/*/batch-*-protocol-0.json'))
    assert all(i['id'].startswith('original-') for i in report['issues'])


@pytest.mark.parametrize('case',['complete_audio','cut_dialogue','service_error'])
def test_failed_short_bridge_can_only_retain_complete_source_audio_and_needs_review(tmp_path,case):
    import json
    plan=Plan(episode_summary='',genre='',style='',clips=[Clip(title='短承接保留原声',angle='',hook='',beats=[
        Beat(mode=mode,start=a,end=b,text='前段交代事情的来由' if mode=='narration' else '',evidence_ids=[i],reason='承接')
        for i,(mode,a,b) in enumerate([('original',0,12),('narration',12,20),('original',20,32),
                                       ('narration',32,36),('original',36,48)])])])
    before=plan.model_dump()
    lines=[Line(id=i,start=a,end=b,text='完整对白') for i,(a,b) in enumerate([(1,11),(13,19),(21,31),(32.4,35.6),(37,47)])]
    if case=='cut_dialogue':lines[3].end=36.7
    cfg=Settings(duration_profile='long',target_seconds=300,min_seconds=30,max_seconds=600,opening='original',repair_attempts=1)
    class Model:
        def json(self,system,prompt):
            target=json.JSONDecoder().raw_decode(prompt.split('\ntarget=',1)[1])[0]
            if target['id']=='beat-1':return {'changes':[{'id':'beat-1','text':'前段先交代这件事的来由'}]}
            if case=='service_error':raise RuntimeError('service unavailable')
            return {'changes':[{'id':'beat-3','start':33,'end':33.7,'text':'这里接话了'}]}
    issues=[{'id':'beat-1','message':'自然承接'},{'id':'beat-3','message':'修复短承接'}]
    if case!='complete_audio':
        with pytest.raises(RuntimeError if case=='service_error' else ValueError):
            repair_plan(plan,issues,lines,{},tmp_path,cfg,Model(),0,source_seconds=48)
        assert not list(tmp_path.rglob('source-audio-fallback.json'))
    else:
        revised=repair_plan(plan,issues,lines,{},tmp_path,cfg,Model(),0,source_seconds=48)
        b=revised.clips[0].beats[3]
        assert (b.mode,b.start,b.end,b.text,b.evidence_ids)==('original',32,36,'',[3])
        assert revised.clips[0].beats[1].text=='前段先交代这件事的来由'
        for i in (0,2,4):assert revised.clips[0].beats[i]==plan.clips[0].beats[i]
        from autoclip.util import read_json
        assert read_json(next(tmp_path.rglob('source-audio-fallback.json')))['status']=='requires_full_review'
        assert not (tmp_path/'plan-approved.json').exists()
    assert plan.model_dump()==before


@pytest.mark.parametrize('adjacent',[False,True])
def test_original_prefix_restoration_requires_exact_adjacent_complete_source(adjacent):
    from autoclip.automatic import restore_original_prefix
    plan=Plan(episode_summary='',genre='',style='',clips=[Clip(title='还原完整句子',angle='',hook='',beats=[
        Beat(mode=mode,start=a,end=b,text='交代这场问答的经过' if mode=='narration' else '',evidence_ids=[i],reason='承接')
        for i,(mode,a,b) in enumerate([('original',0,12),('narration',12,20),('original',20,32),
                                       ('narration',32,36),('original',36,48)])])])
    plan.clips[0].beats[4].evidence_ids=[3,4]
    if not adjacent:plan.clips[0].beats[3].end=35
    before=plan.model_dump()
    lines=[Line(id=i,start=a,end=b,text='完整对白') for i,(a,b) in enumerate([(1,11),(13,19),(21,31),(32.4,35.6),(37,47)])]
    cfg=Settings(duration_profile='long',target_seconds=300,min_seconds=30,max_seconds=600,opening='original')
    revised=restore_original_prefix(plan,'original-4',lines,cfg,48)
    if adjacent:
        assert revised.clips[0].beats[3].mode=='original'
        assert revised.clips[0].beats[3].evidence_ids==[3]
        for i in (0,1,2,4):assert revised.clips[0].beats[i]==plan.clips[0].beats[i]
    else:assert revised is None
    assert plan.model_dump()==before


@pytest.mark.parametrize('recovers',[True,False])
def test_inconsistent_voice_support_rechecks_review_before_rewriting_script(tmp_path,monkeypatch,recovers):
    import json
    import autoclip.automatic as module
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    frame=tmp_path/'frame.jpg';frame.write_bytes(b'frame')
    monkeypatch.setattr(module,'contact_sheet',lambda *a:frame)
    monkeypatch.setattr(module,'subtitle_evidence_sheet',lambda *a:frame)
    plan=sample_plan();before=plan.model_dump()
    lines=[Line(id=i,start=i*10+.5,end=(i+1)*10-.5,text='甲先生，请坐。' if i==0 else '谢谢您。') for i in range(4)]
    voices={'source':source_identity(video),'transcript_digest':digest([s.model_dump() for s in lines]),'lines':[]}
    class Model:
        calls=0
        def json(self,system,prompt,images):
            group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
            if any(u['id']=='beat-1' for u in group):self.calls+=1
            rows=[]
            for u in group:
                row={'id':u['id'],'verdict':'supported','reason':'源对白与画面支持',
                     'evidence_ids':[0],'frame_ids':[0],'identity_basis':'no_identity_claim','subjects':[]}
                if u['id']=='beat-1':
                    row.update(identity_basis='voice_and_context',subjects=['甲'])
                    if recovers and self.calls>1:
                        assert '字幕放大图' in prompt or '审核自身的身份依据' in prompt
                        row.update(identity_basis='explicit_address_response',evidence_ids=[0,1])
                rows.append(row)
            return {'units':rows,'issues':[]}
    model=Model();report=module.audit_plan(video,plan,lines,voices,tmp_path,Settings(repair_attempts=1),model)
    assert model.calls==2
    assert report['passed'] is recovers
    assert plan.model_dump()==before
    if not recovers:assert any('声音' in i['message'] for i in report['issues'])


def test_caption_dispute_samples_named_middle_ids_before_generic_endpoints():
    from autoclip.automatic import caption_lines_for_issues
    lines=[Line(id=i,start=i,end=i+1,text='原字幕') for i in range(590,615)]
    selected=caption_lines_for_issues([{'message':'字幕600-602中的称呼有误，核查字幕612。'}],lines,[590,614])
    assert [s.id for s in selected]==[600,601,602,612,590,614]


def test_local_writer_receives_actual_caption_evidence_not_only_reviewer_claim(tmp_path,monkeypatch):
    import autoclip.automatic as module
    from test_pipeline import fixture_plan
    lines,plan=fixture_plan()
    video=tmp_path/'source.mp4';video.write_bytes(b'video')
    frame=tmp_path/'caption.jpg';frame.write_bytes(b'caption')
    selected_frame=tmp_path/'selected.jpg';selected_frame.write_bytes(b'selected')
    captured=[]
    def sheet(video,root,selected,name):captured.extend(s.id for s in selected);return frame
    monkeypatch.setattr(module,'subtitle_evidence_sheet',sheet)
    def selected_sheet(video,root,times,name):
        target=plan.clips[0].beats[0]
        assert times==[round(t,3) for t in (target.start+.1,(target.start+target.end)/2,target.end-.1)]
        return selected_frame
    monkeypatch.setattr(module,'contact_sheet',selected_sheet)
    class Model:
        def json(self,system,prompt,images):
            if '局部修订核验员' in system:
                assert images==[selected_frame,frame]
                return {'issues':[],'units':[{'id':'beat-0','verdict':'supported','reason':'原片支持修订',
                    'evidence_ids':[0],'frame_ids':[0],'identity_basis':'no_identity_claim','subjects':[]}]}
            assert images==[frame,selected_frame] and '实际原片字幕放大图' in prompt
            assert '当前实际剪入区间' in prompt and '不能据此前字幕图虚构当前画面' in prompt
            return {'changes':[{'id':'beat-0','text':'按原片可见文字作必要承接','evidence_ids':[0]}]}
    revised=module.repair_plan(plan,[{'id':'beat-0','message':'字幕0的转写字形有误'}],lines,
        {'source':source_identity(video),'lines':[]},tmp_path,Settings(target_seconds=45),Model(),0,source_seconds=40)
    assert captured==[0,0]
    assert revised.clips[0].beats[0].text=='按原片可见文字作必要承接'
    assert revised.clips[0].beats[1:]==plan.clips[0].beats[1:]
    assert not (tmp_path/'plan-approved.json').exists()


@pytest.mark.parametrize('failure',['fixed','unresolved','bad_frame','malformed'])
def test_local_patch_requires_source_verification_before_acceptance(tmp_path,monkeypatch,failure):
    import json
    import autoclip.automatic as module
    from autoclip.repair import RepairNeeded
    from autoclip.util import read_json
    from test_pipeline import fixture_plan
    lines,plan=fixture_plan();original=plan.model_dump()
    video=tmp_path/'source.mp4';video.write_bytes(b'video')
    frame=tmp_path/'scene.jpg';frame.write_bytes(b'scene')
    monkeypatch.setattr(module,'contact_sheet',lambda *a:frame)
    monkeypatch.setattr(module,'subtitle_evidence_sheet',lambda *a:frame)
    class Model:
        writes=0
        checks=0
        def json(self,system,prompt,images):
            if '局部修订核验员' in system:
                self.checks+=1
                group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
                assert len(group)==1 and group[0]['id']=='beat-0'
                if failure=='malformed':return {'units':['beat-0']}
                supported=failure in {'bad_frame','fixed'} and self.writes>1
                return {'issues':[],'units':[{'id':'beat-0','verdict':'supported' if supported else 'contradicted',
                    'reason':'当前是室内陈设，没有人提灯' if not supported else '原片支持',
                    'evidence_ids':[0],'frame_ids':[9 if failure=='bad_frame' else 0],
                    'identity_basis':'no_identity_claim','subjects':[]}]}
            self.writes+=1
            if self.writes>1:assert '上轮局部修订未通过程序检查' in prompt
            return {'changes':[{'id':'beat-0','text':'有人提着灯往里走。' if self.writes==1 else '有人请客人进屋。','evidence_ids':[0]}]}
    model=Model()
    args=(plan,[{'id':'beat-0','message':'当前画面中没有提灯者，旁白动作无据'}],lines,
        {'source':source_identity(video),'lines':[]},tmp_path,Settings(target_seconds=45,repair_attempts=1),model,0)
    if failure=='fixed':
        revised=module.repair_plan(*args,source_seconds=40)
        assert revised.clips[0].beats[0].text=='有人请客人进屋。'
        assert read_json(tmp_path/'automatic-repairs/0/beat-0/verify-1/verification.json')['passed']
    else:
        with pytest.raises(RepairNeeded) as exc:module.repair_plan(*args,source_seconds=40)
        assert exc.value.report['clip']==plan.clips[0].model_dump()
    assert model.writes==model.checks==2
    assert plan.model_dump()==original and not (tmp_path/'plan-approved.json').exists()


def test_metadata_crops_prioritize_plot_changing_words_from_asr():
    from autoclip.automatic import metadata_caption_lines
    lines=[Line(id=i,start=i,end=i+1,text='八爷继续说话') for i in range(20)]
    lines.append(Line(id=638,start=30,end=34,text='八爷的乳名暗含着八爷的绝路'))
    chosen=metadata_caption_lines([{'text':'乳名暗含绝路，从相面到下令拿人'}],lines)
    assert 638 in [s.id for s in chosen] and len(chosen)<=6


def test_metadata_gets_source_caption_images_before_any_asr_dispute(tmp_path,monkeypatch):
    import json
    import autoclip.automatic as module
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    main=tmp_path/'frame.jpg';main.write_bytes(b'full source picture')
    captions=tmp_path/'captions.jpg';captions.write_bytes(b'actual source captions')
    monkeypatch.setattr(module,'contact_sheet',lambda *a:main)
    monkeypatch.setattr(module,'subtitle_evidence_sheet',lambda *a:captions)
    plan=sample_plan();plan.clips[0].angle='乳名暗含绝路'
    lines=[Line(id=i,start=i*10+.1,end=i*10+9,text='乳名暗含绝路' if i==0 else '当前对白') for i in range(4)]
    voices={'source':source_identity(video),'transcript_digest':digest([s.model_dump() for s in lines]),'lines':[]}
    class Model:
        def json(self,system,prompt,images):
            group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
            if any(u['id']=='angle' for u in group):
                assert images==[main,captions] and 'ASR只是识别稿' in prompt
            return {'issues':[],'units':[{'id':u['id'],
                'verdict':'contradicted' if u['id']=='angle' else 'supported',
                'reason':'原片字幕为爵禄，不能写成绝路' if u['id']=='angle' else '依据相符',
                'evidence_ids':u.get('evidence_ids',[0]),'frame_ids':[0],
                'identity_basis':'no_identity_claim','subjects':[]} for u in group]}
    report=module.audit_plan(video,plan,lines,voices,tmp_path,Settings(repair_attempts=0),Model())
    assert not report['passed'] and any(i['id']=='angle' and '爵禄' in i['message'] for i in report['issues'])
    assert report['metadata_caption_policy']==module.METADATA_CAPTION_POLICY
    assert any(f.get('purpose')=='metadata_captions' and f['path']==str(captions) for f in report['frames'])


@pytest.mark.parametrize('correction',[True,False])
def test_required_source_correction_in_notes_blocks_supported_metadata(correction):
    units=[{'id':'title','text':'当众拆字'},{'id':'angle','text':'乳名暗含绝路' if correction else '乳名暗含爵禄'}]
    note=('原片字幕为“乳名暗含爵禄”，角度中“绝路”应据清晰字幕修正为“爵禄”。' if correction else
          'ASR把爵禄写作绝路，当前角度已修正为爵禄，无需修改。')
    raw={'passed':True,'issues':[],'notes':[{'id':'title','message':note}],
        'units':[{'id':u['id'],'verdict':'supported','reason':'测试字幕与画面',
            'evidence_ids':[0],'frame_ids':[0],'identity_basis':'no_identity_claim','subjects':[]} for u in units]}
    issues=assess_batch(raw,units,[Line(id=0,start=0,end=4,text='乳名暗含绝路')],1,[])
    if correction:assert len(issues)==1 and issues[0]['id']=='angle' and '爵禄' in issues[0]['message']
    else:assert issues==[]


def test_local_repair_rejects_process_language_without_blocking_other_pending_units(tmp_path):
    from autoclip.util import read_json
    plan=sample_plan();plan.clips[0].beats[1].text='字幕里说，这事没弄明白。'
    plan.clips[0].beats[2].text='镜头里有人继续说话。'
    before=plan.model_dump()
    class Model:
        calls=0
        def json(self,system,prompt):
            self.calls+=1
            assert '字幕、转写、声纹及审核记录只是内部依据' in prompt
            if self.calls>1:assert '仍混入制作或审查话术' in prompt
            return {'changes':[{'id':'beat-1','text':'画面中有人表示不明白。' if self.calls==1 else '他仍不明白这次来意。'}]}
    model=Model();revised=repair_plan(plan,[{'id':'beat-1','message':'制作话术'}],[],{},tmp_path,
                                    Settings(repair_attempts=1),model,0)
    assert model.calls==2 and revised.clips[0].beats[1].text=='他仍不明白这次来意。'
    assert revised.clips[0].beats[2]==plan.clips[0].beats[2] and plan.model_dump()==before
    assert '制作或审查话术' in read_json(tmp_path/'automatic-repairs/0/beat-1/attempt-0.json')['error']


def test_supported_model_verdict_cannot_approve_production_language(tmp_path,monkeypatch):
    import json
    import autoclip.automatic as module
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    frame=tmp_path/'frame.jpg';frame.write_bytes(b'frame')
    monkeypatch.setattr(module,'contact_sheet',lambda *a:frame)
    monkeypatch.setattr(module,'subtitle_evidence_sheet',lambda *a:frame)
    plan=sample_plan();plan.clips[0].beats[1].text='字幕里说，这事没弄明白。'
    lines=[Line(id=i,start=i*10+.1,end=i*10+9,text='当前对白') for i in range(4)]
    voices={'source':source_identity(video),'transcript_digest':digest([s.model_dump() for s in lines]),'lines':[]}
    class Model:
        def json(self,system,prompt,images):
            group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
            return {'issues':[],'units':[{'id':u['id'],'verdict':'supported','reason':'字面符合',
                'evidence_ids':u.get('evidence_ids',[0]),'frame_ids':[0],
                'identity_basis':'no_identity_claim','subjects':[]} for u in group]}
    report=module.audit_plan(video,plan,lines,voices,tmp_path,Settings(),Model())
    assert not report['passed'] and any(i['id']=='beat-1' and '制作' in i['message'] for i in report['issues'])
    with pytest.raises(ValueError,match='制作或审查措辞'):
        assert_approval({**report,'passed':True,'issues':[]},video,plan,lines,voices)


@pytest.mark.parametrize('problem',[None,'missing','duplicate'])
def test_source_caption_reader_receives_only_pixels_and_rejects_wrong_ids(tmp_path,problem):
    from autoclip.automatic import read_caption_observations
    frame=tmp_path/'source.jpg';frame.write_bytes(b'source pixels')
    class Model:
        def json(self,system,prompt,images):
            assert images==[frame] and '字幕辨读员' in system
            assert '李凡院' not in prompt and '手尖' not in prompt and '旁白候选' not in prompt
            rows=[{'line_id':209,'text':'六叔'},{'line_id':210,'text':'到理藩院当个守监的'}]
            if problem=='missing':rows=rows[:1]
            elif problem=='duplicate':rows[1]['line_id']=209
            return {'readings':rows}
    if problem:
        with pytest.raises(ValueError,match='精确覆盖'):read_caption_observations(Model(),frame,[209,210])
    else:assert read_caption_observations(Model(),frame,[209,210])[1]['text']=='到理藩院当个守监的'


def test_asr_dispute_rereads_primary_captions_without_rewriting_correct_narration(tmp_path,monkeypatch):
    import json
    import autoclip.automatic as module
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    frame=tmp_path/'frame.jpg';frame.write_bytes(b'source pixels')
    monkeypatch.setattr(module,'contact_sheet',lambda *a:frame)
    monkeypatch.setattr(module,'subtitle_evidence_sheet',lambda *a:frame)
    plan=sample_plan();plan.clips[0].beats[0].text='他请求到理藩院当个守监的。';before=plan.model_dump()
    lines=[Line(id=i,start=i*10+.1,end=i*10+9,text='李凡院，手尖的' if i==0 else '当前对白') for i in range(4)]
    voices={'source':source_identity(video),'transcript_digest':digest([s.model_dump() for s in lines]),'lines':[]}
    class Model:
        reads=0
        def json(self,system,prompt,images):
            if '字幕辨读员' in system:
                self.reads+=1;assert '李凡院' not in prompt and '守监' not in prompt
                ids=json.loads(prompt.rsplit('：',1)[1])
                return {'readings':[{'line_id':i,'text':'到理藩院当个守监的' if i==0 else '当前对白'} for i in ids]}
            group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
            supplied=json.JSONDecoder().raw_decode(prompt.split('\n字幕=',1)[1])[0]
            assert all('asr_text' in s and 'text' not in s for s in supplied)
            disputing=any(u['id']=='beat-0' for u in group) and '只看原片图' not in prompt
            return {'issues':[],'units':[{'id':u['id'],
                'verdict':'contradicted' if disputing and u['id']=='beat-0' else 'supported',
                'reason':'转写为李凡院手尖，需核对同音字' if disputing and u['id']=='beat-0' else '原片与旁白相符',
                'evidence_ids':u.get('evidence_ids',[0]),'frame_ids':[0],
                'identity_basis':'no_identity_claim','subjects':[]} for u in group]}
    model=Model();report=module.audit_plan(video,plan,lines,voices,tmp_path,Settings(repair_attempts=1),model)
    assert report['passed'] and model.reads==1 and plan.model_dump()==before
    assert lines[0].text=='李凡院，手尖的'
    assert list(tmp_path.glob('automatic-review/*/batch-*-caption-readings.json'))


@pytest.mark.parametrize('phrase',['无须修改','无需再修正','不需要修改','不必修正','不应修改','不须修改'])
def test_harmless_asr_notes_do_not_trigger_content_repair(phrase):
    from autoclip.automatic import required_note_repairs, UnitIssue
    units=[{'id':'beat-3','text':'他说起先前辅政的经历。'}]
    assert required_note_repairs([UnitIssue(id='beat-3',message='ASR同音疑点，旁白按原意概括，'+phrase+'。')],units)==[]


def test_harmless_note_sentence_does_not_hide_another_required_correction():
    from autoclip.automatic import required_note_repairs, UnitIssue
    units=[{'id':'angle','text':'乳名暗含绝路'}]
    notes=[UnitIssue(id='angle',message='标题无需修改。角度中“绝路”必须更正为“爵禄”。')]
    assert required_note_repairs(notes,units)==[{'id':'angle','message':notes[0].message}]


@pytest.mark.parametrize('note',[
    '字幕209中“李凡院”应为“理藩院”的机器转写同音错字，原片字幕放大图已显示“理藩院”，旁白使用“理藩院”正确，不应按asr_text修改。',
    'ASR中“李凡院”需要改为“理藩院”，旁白使用“理藩院”正确，无须修改。',
    '旁白无须对照错误ASR修改。',
])
def test_correct_narration_is_not_rewritten_for_asr_correction_notes(note):
    units=[{'id':'beat-17','text':'年轻人道谢，请六叔让自己到理藩院当个守监的。'}]
    raw={'passed':True,'issues':[],'notes':[{'id':'beat-17','message':note}],
        'units':[{'id':'beat-17','verdict':'supported','reason':'与原片相符',
            'evidence_ids':[0],'frame_ids':[0],'identity_basis':'no_identity_claim','subjects':[]}]}
    assert assess_batch(raw,units,[Line(id=0,start=0,end=8,text='李凡院，我手尖的')],1,[])==[]


def test_review_retry_limits_bind_each_unit_time_and_actual_voice_candidates():
    from autoclip.automatic import review_citation_limits
    lines=[Line(id=i,start=i*10,end=i*10+5,text='原片对白') for i in range(4)]
    rows=[{'line_id':0,'status':'candidate','samples':[{'name':'甲'}]},
          {'line_id':1,'status':'unknown','samples':[{'name':None,'candidates':[{'name':'猜测人物'}]}]},
          {'line_id':2,'status':'candidate','samples':[{'name':'乙'}]},
          {'line_id':3,'status':'conflict','samples':[{'name':'甲'},{'name':'乙'}]}]
    units=[{'id':'beat-1','end':20}, {'id':'beat-2','end':30,'shown_evidence':[3]},
           {'id':'original-0','mode':'original','end':40},{'id':'title'}]
    limits=review_citation_limits(units,lines,rows)
    assert limits[0]['allowed_evidence_ids']==[0,1]
    assert limits[0]['future_evidence_ids']==[2,3]
    assert limits[0]['voice_candidates']==[{'line_id':0,'names':['甲']}]
    assert limits[1]['allowed_evidence_ids']==[0,1,2,3]
    assert limits[1]['voice_candidates']==[{'line_id':0,'names':['甲']},{'line_id':2,'names':['乙']}]
    assert limits[2]['voice_candidates']==[]
    assert limits[3]['allowed_evidence_ids']==[0,1,2,3]


@pytest.mark.parametrize('message,expected',[
    ('请的是“二爷”而非“二爷”，应改写。',True),
    ('画面字幕放大图标注的说话者为甲，人物归属不符。',True),
    ('字幕之间有无字幕间隔，无法确认原声完整。',True),
    ('中间是否有对白被ASR漏记无法从字幕确认。',True),
    ('切点切断了当前对白，回应丢失。',False),
    ('说的是“二爷”而非“三爷”。',False)])
def test_review_self_recheck_does_not_silently_approve_source_problems(message,expected):
    from autoclip.automatic import review_recheck_reason
    assert bool(review_recheck_reason([{'id':'original-1','message':message}],
        [{'id':'original-1','mode':'original'}]))==expected


@pytest.mark.parametrize('still_broken',[False,True])
def test_continuous_original_gap_recheck_is_bounded_and_keeps_real_failures(tmp_path,monkeypatch,still_broken):
    import json
    import autoclip.automatic as module
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    frame=tmp_path/'frame.jpg';frame.write_bytes(b'frame')
    monkeypatch.setattr(module,'contact_sheet',lambda *a:frame)
    monkeypatch.setattr(module,'subtitle_evidence_sheet',lambda *a:frame)
    plan=sample_plan()
    plan.clips[0].beats[0].mode='original';plan.clips[0].beats[0].text=''
    lines=[Line(id=i,start=i*10+.5,end=i*10+5,text='对白') for i in range(4)]
    voices={'source':source_identity(video),'transcript_digest':digest([s.model_dump() for s in lines]),'lines':[]}
    attempts=[]
    class Model:
        def json(self,system,prompt,images):
            group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
            original=group[0].get('mode')=='original'
            if original:attempts.append(prompt)
            rejected=original and (len(attempts)==1 or still_broken)
            reason=('字幕之间有无字幕间隔，不能确认原声完整。' if len(attempts)==1 else '实际结束切点丢失回应。') if rejected else '原片支持'
            return {'issues':[],'units':[{'id':u['id'],'verdict':'unknown' if rejected else 'supported',
                'reason':reason,'evidence_ids':u.get('evidence_ids',[0]),'frame_ids':[0],
                'identity_basis':'no_identity_claim','subjects':[]} for u in group]}
    report=module.audit_plan(video,plan,lines,voices,tmp_path,Settings(repair_attempts=1),Model())
    assert len(attempts)==2 and '内部没有删剪' in attempts[1]
    assert report['passed'] is (not still_broken)
    if still_broken:assert any('实际结束切点' in i['message'] for i in report['issues'])


def test_invalid_frame_feedback_gives_exact_main_sheet_bounds():
    raw={'passed':True,'issues':[],'units':[{'id':'beat-0','verdict':'supported','reason':'原片支持',
        'evidence_ids':[0],'frame_ids':[9],'identity_basis':'no_identity_claim','subjects':[]}]}
    with pytest.raises(ValueError,match=r'frame_ids只能为\[0, 1, 2\]'):
        assess_batch(raw,[{'id':'beat-0'}],[Line(id=0,start=0,end=3,text='对白')],3,[])


@pytest.mark.parametrize('actual,expected',[
    ('随后有人引路，称一声“二爷，您请”。',True),
    ('随后有人引路，称一声“阿爷”。',False),
    ('随后有人引路，称一声“阿 爷”。',False)])
def test_review_must_quote_actual_target_before_requesting_word_correction(actual,expected):
    from autoclip.automatic import review_recheck_reason
    reasons=review_recheck_reason([{'id':'beat-23','message':'引路称呼应为“二爷，您请”，旁白写成“阿爷”，与画面字幕不符。'}],
        [{'id':'beat-23','text':actual}])
    assert bool(reasons)==expected
    if expected:assert actual in reasons[0]


@pytest.mark.parametrize('label,subject,accepted',[
    ('八爷（八阿哥）','八爷',True),
    ('八爷（八阿哥）','八爷（八阿哥）',True),
    ('八爷（八阿哥）','八阿哥',False),
    ('十阿哥','十爷',False),
    ('道士','张道长',False),
    ('四爷(雍正)','四爷',True)])
def test_human_reference_label_prefix_is_not_a_different_person(label,subject,accepted):
    raw,units,lines,count,voices=review_fixture()
    raw['units'][0].update(identity_basis='voice_and_context',subjects=[subject])
    voices=[{'line_id':1,'status':'candidate','samples':[{'name':label}]}]
    assert (not assess_batch(raw,units,lines,count,voices))==accepted


@pytest.mark.parametrize('valid_rejection',[True,False])
def test_malformed_recheck_only_preserves_valid_rejection_never_creates_approval(tmp_path,monkeypatch,valid_rejection):
    import json
    import autoclip.automatic as module
    from autoclip.util import read_json
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    frame=tmp_path/'frame.jpg';frame.write_bytes(b'frame')
    monkeypatch.setattr(module,'contact_sheet',lambda *a:frame)
    monkeypatch.setattr(module,'subtitle_evidence_sheet',lambda *a:frame)
    plan=sample_plan()
    plan.clips[0].beats[0].mode='original';plan.clips[0].beats[0].text=''
    lines=[Line(id=i,start=i*10+.5,end=i*10+5,text='对白') for i in range(4)]
    voices={'source':source_identity(video),'transcript_digest':digest([s.model_dump() for s in lines]),'lines':[]}
    class Model:
        attempts=0
        def json(self,system,prompt,images):
            group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
            original=group[0].get('mode')=='original'
            if original:
                self.attempts+=1
                if not valid_rejection or self.attempts>1:return {'units':[group[0]['id'],'verdict']}
            return {'issues':[],'units':[{'id':u['id'],'verdict':'unknown' if original else 'supported',
                'reason':'ASR漏记，无法确认原声完整' if original else '原片支持',
                'evidence_ids':u.get('evidence_ids',[0]),'frame_ids':[0],
                'identity_basis':'no_identity_claim','subjects':[]} for u in group]}
    model=Model()
    if valid_rejection:
        report=module.audit_plan(video,plan,lines,voices,tmp_path,Settings(repair_attempts=1),model)
        assert not report['passed'] and any('ASR漏记' in i['message'] for i in report['issues'])
        assert list(tmp_path.glob('automatic-review/*/batch-*-recheck-failed.json'))
        assert read_json(tmp_path/'automatic-review-state.json')['status']=='completed'
    else:
        with pytest.raises(ValueError,match='格式或引用校验失败'):
            module.audit_plan(video,plan,lines,voices,tmp_path,Settings(repair_attempts=1),model)
        assert not (tmp_path/'automatic-review.json').exists()
        assert read_json(tmp_path/'automatic-review-state.json')['status']=='running'
    assert model.attempts==2 and not (tmp_path/'plan-approved.json').exists()
