from copy import deepcopy

import pytest

from autoclip.automatic import UnitReviews, ReviewBatch, normalize_review_response, assess_batch
from autoclip.models import Line


def response():
    return {'units':[{'id':'beat-1','verdict':'supported','reason':'原片支持',
        'evidence_ids':[1],'frame_ids':[0],'identity_basis':'no_identity_claim','subjects':[]}],
        'issues':[],'notes':[]}


@pytest.mark.parametrize('value',[None,'',[],{}])
def test_empty_extension_fields_are_recovered_without_mutating_raw_or_verdict(value):
    raw=response()
    raw['units'][0].update(notes=value,subject_evidence_note=value,reason_detail_note=value)
    raw['subject_evidence']=value
    before=deepcopy(raw)
    parsed=UnitReviews.model_validate(raw)
    assert raw==before and parsed.units[0].verdict=='supported'
    normalized,operations=normalize_review_response(raw)
    assert len(operations)==4 and normalized==response()


@pytest.mark.parametrize('extra',[False,0,'未核实的人物',{'verdict':'unknown'},['仍有错误']])
def test_substantive_unknown_fields_are_not_silently_discarded(extra):
    raw=response();raw['units'][0]['invented_review_field']=extra
    with pytest.raises(ValueError,match='Extra inputs'):UnitReviews.model_validate(raw)


@pytest.mark.parametrize('field',['evidence_ids','frame_ids','reason','verdict','identity_basis'])
def test_required_fields_do_not_become_optional_during_format_recovery(field):
    raw=response();raw['units'][0][field]=[] if field.endswith('_ids') else ''
    raw['units'][0]['notes']=[]
    with pytest.raises(ValueError):UnitReviews.model_validate(raw)


@pytest.mark.parametrize('notes',[
    '旁白中“二十万”必须更正为“三十万”。',
    ['旁白中“二十万”必须更正为“三十万”。'],
    [{'message':'旁白中“二十万”必须更正为“三十万”。'}],
    [{'id':'beat-1','message':'旁白中“二十万”必须更正为“三十万”。'}]])
def test_misplaced_notes_are_preserved_and_required_corrections_still_block_approval(notes):
    raw=response();raw['units'][0]['notes']=notes
    parsed=UnitReviews.model_validate(raw)
    assert len(parsed.notes)==1 and parsed.notes[0].id=='beat-1'
    checked=parsed.model_dump();checked['passed']=True
    issues=assess_batch(checked,[{'id':'beat-1','text':'他说捐款二十万','start':0,'end':4}],
        [Line(id=1,start=1,end=3,text='三十万')],1,[])
    assert issues and issues[0]['id']=='beat-1'


@pytest.mark.parametrize('notes',[
    [{'id':'beat-9','message':'不得把错误移到其他段'}],
    [{'message':'有内容','unknown_field':'不能丢弃'}],
    ['正常备注',False]])
def test_ambiguous_misplaced_notes_remain_rejected(notes):
    raw=response();raw['units'][0]['notes']=notes
    with pytest.raises(ValueError):UnitReviews.model_validate(raw)


def test_empty_proof_extensions_do_not_bypass_identity_evidence_validation():
    raw=response()
    raw['units'][0].update(identity_basis='mixed',subjects=['甲先生'],subject_evidence=[{
        'subject':'甲先生','basis':'explicit_reference','evidence_ids':[999],'reason':'被提及','notes':[]}])
    parsed=UnitReviews.model_validate(raw)
    checked=parsed.model_dump();checked['passed']=True
    with pytest.raises(ValueError,match='未提供的字幕'):
        assess_batch(checked,[{'id':'beat-1','text':'有人提及甲先生','start':0,'end':4}],
            [Line(id=1,start=1,end=3,text='甲先生')],1,[])


@pytest.mark.parametrize('verdict',['unknown','contradicted'])
def test_format_recovery_never_turns_a_rejection_into_support(verdict):
    raw=response();raw['units'][0].update(verdict=verdict,notes=[])
    parsed=UnitReviews.model_validate(raw)
    checked=parsed.model_dump();checked['passed']=False
    assert assess_batch(checked,[{'id':'beat-1','text':'他说事情已经办完','start':0,'end':4}],
        [Line(id=1,start=1,end=3,text='还没办完')],1,[])
    assert parsed.units[0].verdict==verdict
