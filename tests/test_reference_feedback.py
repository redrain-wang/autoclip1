"""Reference feedback locates literal source text without deciding an identity."""
from copy import deepcopy
import json

import pytest

from autoclip.automatic import assess_batch
from autoclip.models import Beat, Clip, Line, Plan
from autoclip.util import read_json


CITED = '\n该proof实际引用='
CANDIDATES = '\n同名字面候选（供对图复核，非身份认定，不自动补引用或放行）='


def rows_after(message, label):
    return json.JSONDecoder().raw_decode(message.split(label, 1)[1])[0]


def reference_response(uid='hook', *, verdict='supported'):
    return {'passed': verdict == 'supported', 'issues': [], 'units': [{
        'id': uid, 'verdict': verdict, 'reason': '人物的行动归属仍无依据' if verdict != 'supported' else '对白提及了九爷',
        'evidence_ids': [10], 'frame_ids': [0], 'identity_basis': 'explicit_reference', 'subjects': ['九爷']}]}


@pytest.mark.parametrize('verdict', ['supported', 'contradicted'])
def test_literal_candidate_is_feedback_and_does_not_supply_the_missing_reference(verdict):
    units = [{'id': 'hook', 'text': '九爷托人捎来了一封信。'}]
    lines = [Line(id=10, start=1, end=3, text='九哥最近没有来信'),
             Line(id=11, start=4, end=6, text='九爷托我捎来这封信'),
             Line(id=12, start=7, end=9, text='九阿哥另有安排')]
    raw = reference_response(verdict=verdict)
    before = deepcopy((raw, units, [s.model_dump() for s in lines]))
    issues = assess_batch(raw, units, lines, 1, [])
    feedback = next(i['message'] for i in issues if i['message'].startswith('被提及人物须在'))
    assert rows_after(feedback, CITED) == [{'id': 10, 'text': '九哥最近没有来信'}]
    assert rows_after(feedback, CANDIDATES) == [{'id': 11, 'text': '九爷托我捎来这封信'}]
    assert issues  # A nearby matching name never repairs the model's actual proof.
    if verdict == 'contradicted':
        assert {'id': 'hook', 'message': '人物的行动归属仍无依据'} in issues
    assert (raw, units, [s.model_dump() for s in lines]) == before


def test_caption_readings_override_asr_for_actual_proof_and_candidates_and_future_is_excluded():
    units = [{'id': 'beat-1', 'text': '九爷托人捎来了信。', 'start': 100, 'end': 120}]
    lines = [Line(id=10, start=101, end=103, text='九爷最近没有来信'),
             Line(id=11, start=105, end=107, text='九爷让我过来'),
             Line(id=12, start=110, end=112, text='九哥托我捎信'),
             Line(id=13, start=120, end=122, text='九爷这就到了'),
             Line(id=14, start=130, end=132, text='九爷已经答应'),
             Line(id=15, start=115, end=117, text='九阿哥托我捎信')]
    captions = [{'line_id': 10, 'text': '九哥最近没有来信'},
                {'line_id': 11, 'text': '八爷让我过来'},
                {'line_id': 12, 'text': '九爷托我捎信'}]
    raw = reference_response('beat-1')
    before = deepcopy((raw, units, captions, [s.model_dump() for s in lines]))
    issues = assess_batch(raw, units, lines, 1, [], captions)
    feedback = next(i['message'] for i in issues if i['message'].startswith('被提及人物须在'))
    assert rows_after(feedback, CITED) == [{'id': 10, 'text': '九哥最近没有来信'}]
    assert rows_after(feedback, CANDIDATES) == [{'id': 12, 'text': '九爷托我捎信'}]
    assert (raw, units, captions, [s.model_dump() for s in lines]) == before


def test_candidate_search_requires_the_literal_name_in_the_actual_target_and_is_bounded():
    lines = [Line(id=10, start=1, end=2, text='九哥没有来信')]
    lines += [Line(id=i, start=i, end=i + .5, text=f'九爷的第{i}封信') for i in range(20, 29)]
    raw = reference_response()
    issues = assess_batch(raw, [{'id': 'hook', 'text': '九哥托人捎来了一封信。'}], lines, 1, [])
    assert CANDIDATES not in issues[0]['message']
    issues = assess_batch(raw, [{'id': 'hook', 'text': '九爷托人捎来了一封信。'}], lines, 1, [])
    assert [r['id'] for r in rows_after(issues[0]['message'], CANDIDATES)] == list(range(20, 26))


def test_mixed_identity_feedback_shows_the_failing_proofs_own_citations():
    units = [{'id': 'hook', 'text': '九爷送来书信，八爷另有安排。'}]
    lines = [Line(id=10, start=1, end=2, text='九哥没有来信'),
             Line(id=11, start=3, end=4, text='九爷托我捎信'),
             Line(id=20, start=5, end=6, text='八爷另有安排')]
    raw = reference_response()
    raw['units'][0].update(identity_basis='mixed', subjects=['九爷', '八爷'], evidence_ids=[10, 20],
        subject_evidence=[{'subject': name, 'basis': 'explicit_reference', 'evidence_ids': [line_id],
                           'reason': '对白提及该人'} for name, line_id in [('九爷', 10), ('八爷', 20)]])
    before = deepcopy(raw)
    issues = assess_batch(raw, units, lines, 1, [])
    assert len(issues) == 1
    assert rows_after(issues[0]['message'], CITED) == [{'id': 10, 'text': '九哥没有来信'}]
    assert rows_after(issues[0]['message'], CANDIDATES) == [{'id': 11, 'text': '九爷托我捎信'}]
    assert raw == before


def test_precise_reference_feedback_reaches_independent_recheck_and_a_real_rejection_still_blocks(tmp_path, monkeypatch):
    import autoclip.automatic as automatic

    video = tmp_path / 'source.mp4'
    video.write_bytes(b'source')
    frame = tmp_path / 'frame.jpg'
    frame.write_bytes(b'pixels')
    monkeypatch.setattr(automatic, 'contact_sheet', lambda *a: frame)
    monkeypatch.setattr(automatic, 'subtitle_evidence_sheet', lambda *a: frame)
    monkeypatch.setattr(automatic, 'read_disputed_reference_captions', lambda *a, **k: None)
    plan = Plan(episode_summary='', genre='', style='', clips=[Clip(title='来信要求捐款', angle='', hook='', beats=[
        Beat(mode='narration', start=i * 10, end=(i + 1) * 10, text='九爷托人捎来一封信。', evidence_ids=[10], reason='来信')
        for i in range(4)])])
    lines = [Line(id=10, start=11, end=12, text='九哥最近没有来信'),
             Line(id=11, start=13, end=14, text='九爷托我捎来这封信')]
    before = plan.model_dump()
    prompts = []

    class Reviewer:
        def json(self, system, prompt, images):
            assert '局部修订核验员' in system
            prompts.append(prompt)
            raw = reference_response('beat-1', verdict='supported' if len(prompts) == 1 else 'contradicted')
            raw.pop('passed')
            if len(prompts) > 1:
                raw['units'][0]['evidence_ids'] = [11]
            return raw

    with pytest.raises(ValueError, match='局部修订仍未解决原片问题'):
        automatic.verify_narration_patch(video, plan, 'beat-1', [], lines, {'lines': []}, tmp_path,
                                         Reviewer(), retries=1)
    assert len(prompts) == 2
    error = read_json(tmp_path / 'protocol-0.json')['error']
    assert error in prompts[1]
    assert '该proof实际引用' in error and '同名字面候选' in error and '非身份认定' in error
    verification = read_json(tmp_path / 'verification.json')
    assert verification['passed'] is False
    assert verification['issues'] == [{'id': 'beat-1', 'message': '人物的行动归属仍无依据'}]
    assert plan.model_dump() == before
    assert not (tmp_path / 'plan-approved.json').exists()
