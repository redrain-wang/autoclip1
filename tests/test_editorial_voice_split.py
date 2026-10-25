from pathlib import Path

import pytest

from autoclip import short_voices
from autoclip.util import digest, read_json, write_json
from test_short_voices import followup_fixture


@pytest.mark.parametrize('stage',['narrative_review','commentary_balance','selection_review','editorial_review','editorial'])
def test_editorial_stages_never_create_voice_enrollment_even_with_identity_words(tmp_path,monkeypatch,stage):
    video,library,bank,cfg,root,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    report['issues']=[{'id':'beat-1','stage':stage,
                      'message':'人物身份转场没有交代清楚，应补一句承接，不是声音缺口'}]
    write_json(story/'automatic-review.json',report)
    assert short_voices.prepare_voice_followup(video,lines,voices,root,cfg,library,[story]) is None
    assert calls==[] and not (root/'voice-followup.json').exists()
    assert read_json(library)==bank


def test_real_source_identity_gap_keeps_human_step_and_records_review_provenance(tmp_path,monkeypatch):
    video,library,bank,cfg,root,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    report['issues'][0]['stage']='source_review'
    report['issues'].append({'id':'beat-2','stage':'narrative_review','message':'身份切换的衔接不自然'})
    write_json(story/'automatic-review.json',report)
    task=short_voices.prepare_voice_followup(video,lines,voices,root,cfg,library,[story])
    assert task['sample_lines']==[1] and len(calls)==1
    manifest=read_json(Path(task['review_root'])/'manifest.json')
    requests=manifest['automatic_gap_requests']
    assert len(requests)==1 and requests[0]['stage']=='source_review'
    assert requests[0]['review_run']==str(story.resolve())
    assert requests[0]['plan_digest']==report['plan_digest']
    assert requests[0]['issue_digest']==digest(report['issues'][0])
    assert short_voices.pending_voice_followup(video,lines,voices,root,cfg,library)==task
    assert read_json(library)==bank


@pytest.mark.parametrize('stage',['narrative_review','commentary_balance','selection_review'])
def test_explicit_editorial_wait_is_retired_without_changing_human_library(tmp_path,monkeypatch,stage):
    video,library,bank,cfg,root,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    task=short_voices.prepare_voice_followup(video,lines,voices,root,cfg,library,[story])
    path=Path(task['review_root'])/'manifest.json';manifest=read_json(path)
    manifest['automatic_gap_requests'][0]['stage']=stage
    write_json(path,manifest)
    assert short_voices.pending_voice_followup(video,lines,voices,root,cfg,library) is None
    assert read_json(root/'voice-followup.json')['status']=='superseded'
    assert len(calls)==1 and read_json(library)==bank


def legacy_task(tmp_path,monkeypatch):
    data=followup_fixture(tmp_path,monkeypatch)
    video,library,bank,cfg,root,lines,voices,story,report,calls=data
    task=short_voices.prepare_voice_followup(video,lines,voices,root,cfg,library,[story])
    path=Path(task['review_root'])/'manifest.json';manifest=read_json(path)
    for request in manifest['automatic_gap_requests']:
        for key in ('stage','review_run','plan_digest','issue_digest'):request.pop(key,None)
    write_json(path,manifest)
    return (*data,task)


def test_legacy_untyped_task_is_retired_only_when_current_report_proves_editorial_origin(tmp_path,monkeypatch):
    video,library,bank,cfg,root,lines,voices,story,report,calls,task=legacy_task(tmp_path,monkeypatch)
    report['issues'][0]['stage']='narrative_review'
    write_json(story/'automatic-review.json',report)
    assert short_voices.pending_voice_followup(video,lines,voices,root,cfg,library) is None
    assert read_json(root/'voice-followup.json')['status']=='superseded'
    assert read_json(library)==bank


@pytest.mark.parametrize('case',['source_gap','stale_plan','wrong_voice','incomplete','different_reason','ambiguous_reports'])
def test_legacy_untyped_real_or_unbound_gap_is_preserved(tmp_path,monkeypatch,case):
    video,library,bank,cfg,root,lines,voices,story,report,calls,task=legacy_task(tmp_path,monkeypatch)
    if case!='source_gap':report['issues'][0]['stage']='narrative_review'
    if case=='stale_plan':
        plan=read_json(story/'plan-timed.json');plan['clips'][0]['beats'][1]['text']='新的实际工作稿'
        write_json(story/'plan-timed.json',plan)
    elif case=='wrong_voice':report['voice_digest']='different'
    elif case=='incomplete':write_json(story/'automatic-review-state.json',{'status':'running','plan_digest':report['plan_digest']})
    elif case=='different_reason':report['issues'][0]['message']='另一个身份衔接问题'
    write_json(story/'automatic-review.json',report)
    if case=='ambiguous_reports':
        another=root/'stories'/'another'
        write_json(another/'automatic-review.json',report)
        write_json(another/'plan-timed.json',read_json(story/'plan-timed.json'))
    assert short_voices.pending_voice_followup(video,lines,voices,root,cfg,library)==task
    assert read_json(root/'voice-followup.json')['status']=='waiting_voice_confirmation'
    assert read_json(library)==bank and len(calls)==1


def test_mixed_editorial_and_genuine_requests_do_not_drop_the_real_gap(tmp_path,monkeypatch):
    video,library,bank,cfg,root,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    task=short_voices.prepare_voice_followup(video,lines,voices,root,cfg,library,[story])
    path=Path(task['review_root'])/'manifest.json';manifest=read_json(path)
    manifest['automatic_gap_requests'].append({'unit':'beat-2','reason':'人物身份的转场措辞不自然',
                                             'stage':'narrative_review','line_ids':[2]})
    write_json(path,manifest)
    assert short_voices.pending_voice_followup(video,lines,voices,root,cfg,library)==task
    assert read_json(library)==bank


def test_reused_audio_dataset_refreshes_review_provenance_without_reenrollment(tmp_path,monkeypatch):
    video,library,bank,cfg,root,lines,voices,story,report,calls=followup_fixture(tmp_path,monkeypatch)
    task=short_voices.prepare_voice_followup(video,lines,voices,root,cfg,library,[story])
    folder=Path(task['review_root']);old_manifest=read_json(folder/'manifest.json')
    report['issues'][0]['message']='声音候选冲突，需核实该说话人'
    report['issues'][0]['stage']='source_review'
    write_json(story/'automatic-review.json',report)
    again=short_voices.prepare_voice_followup(video,lines,voices,root,cfg,library,[story])
    updated=read_json(folder/'manifest.json')
    assert again==task and len(calls)==1
    assert updated['dataset_id']==old_manifest['dataset_id'] and updated['samples']==old_manifest['samples']
    assert updated['automatic_gap_requests'][0]['reason']==report['issues'][0]['message']
    assert read_json(library)==bank
