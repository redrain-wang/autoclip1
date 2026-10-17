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
    if bad=='contradiction':raw['issues']=['说法有误']
    if bad=='future':units[0]['end']=4
    if bad in {'voice_conflict','no_voice'}:
        raw['units'][0]['identity_basis']='voice_and_context'
        if bad=='voice_conflict':voices=[{'line_id':1,'status':'conflict'}]
    assert assess_batch(raw,units,lines,count,voices)


def test_valid_review_is_accepted():
    assert assess_batch(*review_fixture()) == []


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


def test_automatic_approval_binds_exact_plan_voice_and_frames(tmp_path):
    from autoclip.identity import file_hash
    video = tmp_path/'a.mp4';video.write_bytes(b'video')
    frame = tmp_path/'a.jpg';frame.write_bytes(b'frame')
    plan=sample_plan();lines=[];voices={'library_digest':'first'}
    report={'mode':'automatic_source_review','passed':True,'issues':[],'source':source_identity(video),
        'plan_digest':digest(plan.model_dump()),'transcript_digest':digest([]),'voice_digest':digest(voices),
        'frames':[{'path':str(frame),'sha256':file_hash(frame)}]}
    assert_approval(report,video,plan,lines,voices)
    with pytest.raises(ValueError):assert_approval(report,video,plan,lines,{'library_digest':'new'},)
    frame.write_bytes(b'edited')
    with pytest.raises(ValueError):assert_approval(report,video,plan,lines,voices)
