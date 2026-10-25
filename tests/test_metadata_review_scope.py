"""Metadata may summarize selected later scenes without inventing their facts."""
import json

import pytest

from autoclip.automatic import review_recheck_reason
from autoclip.config import Settings
from autoclip.models import Beat, Clip, Line, Plan
from autoclip.util import digest, source_identity


def test_scope_recheck_only_targets_metadata_without_a_source_interval():
    messages=[
        '当前单位尚未出现后文的通行场景，概括属于预支。',
        '随后并非紧接，中间还有问答，过程压缩过度。']
    for message in messages:
        for uid in ['title','angle','hook']:
            reasons=review_recheck_reason([{'id':uid,'message':message}],[{'id':uid,'text':'先问来意，随后放行'}])
            assert len(reasons)==1 and '按全片实际选用内容' in reasons[0]
            assert '没有单镜头的end截止' in reasons[0]
            assert '仍须明确拒绝' in reasons[0] and '来自别集' in reasons[0]
        assert review_recheck_reason([{'id':'beat-1','message':message}],
            [{'id':'beat-1','text':'当前旁白','start':10,'end':20}])==[]
        assert review_recheck_reason([{'id':'hook','message':message}],
            [{'id':'hook','text':'这里是实际口播','start':10,'end':20}])==[]
        assert review_recheck_reason([{'id':'summary','message':message}],
            [{'id':'summary','text':'其他类型单位'}])==[]
    for message in ['事件来自其他集，选用素材中没有发生。','把请求写成已经放行，改变了必要因果。']:
        assert review_recheck_reason([{'id':'hook','message':message}],[{'id':'hook','text':'已经放行'}])==[]


@pytest.mark.parametrize('resolved',[True,False])
def test_metadata_scope_recheck_does_not_hide_a_real_result_or_causality_error(tmp_path,monkeypatch,resolved):
    import autoclip.automatic as automatic
    video=tmp_path/'video.mp4';video.write_bytes(b'source')
    frame=tmp_path/'frame.jpg';frame.write_bytes(b'actual source frame')
    monkeypatch.setattr(automatic,'contact_sheet',lambda *a:frame)
    monkeypatch.setattr(automatic,'subtitle_evidence_sheet',lambda *a:frame)
    monkeypatch.setattr(automatic,'read_scene_observations',lambda llm,sheet,times,retries:[
        {'frame_id':i,'visible':'原片里的交谈'} for i in range(len(times))])
    plan=Plan(episode_summary='',genre='',style='',clips=[Clip(title='先受阻，随后获准通行',
        angle='问明来意之后处理请求',hook='为何最后准许经过',beats=[
            Beat(mode='narration',start=i*10,end=(i+1)*10,text='来人说明自己的请求，对方作出回应。',
                evidence_ids=[i],reason='讲清经过') for i in range(4)])])
    lines=[Line(id=i,start=i*10+1,end=i*10+5,text=text) for i,text in enumerate([
        '先别过来','请说明来意','我是来送信的','准你过去' if resolved else '仍然不能过去'])]
    voices={'source':source_identity(video),'transcript_digest':digest([s.model_dump() for s in lines]),'lines':[]}
    before=plan.model_dump();metadata_prompts=[]
    class Reviewer:
        def json(self,system,prompt,images):
            group=json.JSONDecoder().raw_decode(prompt.split('\nunits=',1)[1])[0]
            metadata=any('start' not in u for u in group)
            if metadata:
                metadata_prompts.append(prompt)
                assert '按全片实际选用证据核查' in prompt and '“随后”不等于无间隔紧接' in prompt
                if len(metadata_prompts)>1:
                    assert '先核对元信息的范围' in prompt
                    assert '未在选用素材出现、来自别集' in prompt
            first=metadata and len(metadata_prompts)==1
            reason=('当前单位尚未出现后文放行场景，摘要提到后半属于预支。' if first else
                    '选用末段仍然拒绝，摘要把请求写成已经获准，改变了必要因果。')
            rejected=metadata and (first or not resolved)
            return {'issues':[],'units':[{'id':u['id'],
                'verdict':'contradicted' if rejected and u['id']=='hook' else 'supported',
                'reason':reason if rejected and u['id']=='hook' else '原片支持当前概括',
                'evidence_ids':[0,3] if metadata else u['evidence_ids'],'frame_ids':[0],
                'identity_basis':'no_identity_claim','subjects':[]} for u in group]}
    report=automatic.audit_plan(video,plan,lines,voices,tmp_path,Settings(repair_attempts=1),Reviewer())
    assert len(metadata_prompts)==2
    assert report['passed'] is resolved
    if not resolved:
        assert any(i['id']=='hook' and '改变了必要因果' in i['message'] for i in report['issues'])
    else:assert report['issues']==[]
    assert plan.model_dump()==before
    assert list(tmp_path.glob('automatic-review/*/batch-0-source-recheck-0.json'))
    assert not (tmp_path/'plan-approved.json').exists()
