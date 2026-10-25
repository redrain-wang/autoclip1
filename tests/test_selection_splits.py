from copy import deepcopy

import pytest

from autoclip.models import Line
from autoclip.selection import repair_original_selection
from autoclip.util import digest, read_json
from test_selection import fixture, finding


def split_fixture():
    plan,lines,cfg=fixture()
    for index in (2,4):
        start=plan.clips[0].beats[index].start
        rows=[Line(id=index*100+j,start=start+a,end=start+b,text=f'完整原声{index}-{j}')
              for j,(a,b) in enumerate(((1,6),(9,14),(18,23),(28,34)))]
        lines=[line for line in lines if line.id!=index]+rows
        plan.clips[0].beats[index].evidence_ids=[line.id for line in rows]
    return plan,sorted(lines,key=lambda line:line.start),cfg


def row(index,start,end,ids):
    return {'beat':index,'action':'trim','start':start,'end':end,'text':'','evidence_ids':ids}


def test_multiple_trim_segments_remove_middle_and_preserve_only_bound_targets(tmp_path):
    plan,lines,cfg=split_fixture();before=deepcopy(plan.model_dump())
    issues=[finding(plan,2,'trim'),finding(plan,4,'trim'),finding(plan,6,'drop')]
    raw={'changes':[row(4,161,166,[400]),row(4,188,194,[403]),
                    row(2,81,86,[200]),row(2,108,114,[203])]}
    class Model:
        def json(self,system,prompt):
            assert '重复beat' in prompt and '1–3项changes' in prompt
            return raw
    revised=repair_original_selection(plan,issues,lines,tmp_path,cfg,Model(),0,1300)
    assert [(b.start,b.end) for b in revised.clips[0].beats[2:4]]==[(81,86),(108,114)]
    assert [(b.start,b.end) for b in revised.clips[0].beats[5:7]]==[(161,166),(188,194)]
    assert revised.clips[0].beats[:2]==plan.clips[0].beats[:2]
    assert revised.clips[0].beats[4]==plan.clips[0].beats[3]
    assert revised.clips[0].beats[7]==plan.clips[0].beats[5]
    assert revised.clips[0].beats[8:]==plan.clips[0].beats[7:]
    assert len(revised.clips[0].beats)==33 and plan.model_dump()==before
    result=read_json(tmp_path/'selection-repairs/0/result.json')
    assert result['before_digest']==digest(before) and result['issue_digest']==digest(issues)
    assert result['status']=='requires_full_measurement_and_review'
    assert result['raw']==raw and not (tmp_path/'plan-approved.json').exists()


def test_trim_accepts_up_to_three_complete_segments_and_derives_exact_subtitles(tmp_path):
    plan,lines,cfg=split_fixture()
    raw={'changes':[row(2,80,86,[]),row(2,98,103,[202]),row(2,108,115,[203])]}
    class Model:
        def json(self,*args):return raw
    revised=repair_original_selection(plan,[finding(plan,2,'trim')],lines,tmp_path,cfg,Model(),0,1300)
    pieces=revised.clips[0].beats[2:5]
    assert [b.evidence_ids for b in pieces]==[[200],[202],[203]]
    assert all(b.mode=='original' and not b.text for b in pieces)
    assert sum(b.end-b.start for b in pieces)<40


@pytest.mark.parametrize('case',['too_many','overlap','reverse','unchanged_total','cut_sentence',
                                'outside','foreign_id','unknown_target','changed_action','added_text',
                                'no_complete_line','missing_target'])
def test_split_guards_reject_scope_timeline_and_evidence_violations(tmp_path,case):
    plan,lines,cfg=split_fixture();before=plan.model_dump()
    issues=[finding(plan,2,'trim')]
    changes=[row(2,81,86,[200]),row(2,108,114,[203])]
    if case=='too_many':changes=[row(2,81,86,[200]),row(2,89,94,[201]),row(2,98,103,[202]),row(2,108,114,[203])]
    elif case=='overlap':changes=[row(2,80,94,[200,201]),row(2,89,114,[201,202,203])]
    elif case=='reverse':changes.reverse()
    elif case=='unchanged_total':changes=[row(2,80,94,[200,201]),row(2,94,120,[202,203])]
    elif case=='cut_sentence':changes[0]['start']=82
    elif case=='outside':changes[0]['start']=79
    elif case=='foreign_id':changes[0]['evidence_ids']=[400]
    elif case=='unknown_target':changes.append(row(4,161,166,[400]))
    elif case=='changed_action':changes[0]['action']='narrate';changes[0]['text']='改成了旁白'
    elif case=='added_text':changes[0]['text']='不能往原声加字'
    elif case=='no_complete_line':changes[0]=row(2,86,88,[])
    elif case=='missing_target':issues.append(finding(plan,4,'trim'))
    class Model:
        def json(self,*args):return {'changes':changes}
    with pytest.raises(ValueError):repair_original_selection(plan,issues,lines,tmp_path,cfg,Model(),0,1300)
    assert plan.model_dump()==before and not (tmp_path/'selection-repairs/0/result.json').exists()
    rejected=read_json(tmp_path/'selection-repairs/0/rejected-0.json')
    assert rejected['before_digest']==digest(before) and rejected['issue_digest']==digest(issues)


def test_opening_cannot_be_split_even_into_increasing_subsegments(tmp_path):
    plan,lines,cfg=split_fixture()
    class Model:
        def json(self,*args):return {'changes':[row(0,0,6,[]),row(0,6,11,[])]}
    with pytest.raises(ValueError,match='最多允许1段'):
        repair_original_selection(plan,[finding(plan,0,'trim')],lines,tmp_path,cfg,Model(),0,1300)


def test_opening_still_allows_one_complete_legal_trim(tmp_path):
    plan,lines,cfg=split_fixture()
    class Model:
        def json(self,*args):return {'changes':[row(0,1,11,[0])]}
    revised=repair_original_selection(plan,[finding(plan,0,'trim')],lines,tmp_path,cfg,Model(),0,1300)
    assert (revised.clips[0].beats[0].start,revised.clips[0].beats[0].end)==(1,11)


@pytest.mark.parametrize('case',['duplicate','future'])
def test_narration_remains_one_item_and_cannot_quote_later_within_old_original(tmp_path,case):
    plan,lines,cfg=split_fixture()
    change={'beat':2,'action':'narrate','start':80,'end':95,'text':'有人提出要求，对方随后作出回应。',
            'evidence_ids':[200,203] if case=='future' else [200]}
    class Model:
        def json(self,*args):return {'changes':[change,dict(change,start=98,end=114)] if case=='duplicate' else [change]}
    with pytest.raises(ValueError):
        repair_original_selection(plan,[finding(plan,2,'narrate')],lines,tmp_path,cfg,Model(),0,1300)


def test_retry_uses_same_model_review_path_with_distinct_complete_feedback(tmp_path):
    plan,lines,cfg=split_fixture();cfg.repair_attempts=2
    prompts=[];review_prompts=[]
    invalid={'changes':[row(2,82,86,[200]),row(2,108,114,[203])]}
    valid={'changes':[row(2,81,86,[200]),row(2,108,114,[203])]}
    class Model:
        def json(self,system,prompt):prompts.append(prompt);return invalid
        def review_json(self,system,prompt):
            review_prompts.append(prompt)
            assert '当前目标边界及操作' in prompt and '上轮完整响应' in prompt
            return invalid if len(review_prompts)==1 else valid
    revised=repair_original_selection(plan,[finding(plan,2,'trim')],lines,tmp_path,cfg,Model(),0,1300)
    assert len(prompts)==1 and len(review_prompts)==2 and review_prompts[0]!=review_prompts[1]
    assert '第1轮' in review_prompts[0] and '第2轮' in review_prompts[1]
    assert revised.clips[0].beats[2].start==81


def test_drop_remains_bound_and_does_not_request_model_or_allow_returned_changes(tmp_path):
    plan,lines,cfg=split_fixture()
    revised=repair_original_selection(plan,[finding(plan,2,'drop')],lines,tmp_path,cfg,None,0,1300)
    assert revised.clips[0].beats==plan.clips[0].beats[:2]+plan.clips[0].beats[3:]
