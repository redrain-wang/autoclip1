"""Protocol repair must correct finding IDs without erasing a real rejection."""
from copy import deepcopy
import json

import pytest

from autoclip.config import Settings
from autoclip.models import Beat, Clip, Line, Plan
from autoclip.util import digest, read_json, source_identity


def array_item(schema, name):
    item = schema['properties'][name]['items']
    if '$ref' in item:
        return schema['$defs'][item['$ref'].rsplit('/', 1)[1]]
    return item


@pytest.mark.parametrize('original', [False, True])
def test_retry_schema_binds_all_response_ids_and_main_frame_numbers(original):
    import autoclip.automatic as automatic

    units = [{'id': uid} for uid in (['original-40', 'original-41'] if original else ['beat-7', 'beat-8'])]
    model = automatic.OriginalReviewBatch if original else automatic.UnitReviews
    static = deepcopy(model.model_json_schema())
    schema = automatic.review_response_schema(units, 6, original=original)
    expected = {u['id'] for u in units}
    for name in ('units', 'issues', 'notes'):
        assert set(array_item(schema, name)['properties']['id']['enum']) == expected
    decision = array_item(schema, 'units')
    assert decision['properties']['frame_ids']['items']['enum'] == list(range(6))
    assert decision['properties']['frame_ids']['minItems'] == 1
    assert {'id', 'verdict', 'reason', 'evidence_ids', 'frame_ids', 'identity_basis'} <= set(decision['required'])
    if original:
        assert decision['properties']['identity_basis']['const'] == 'no_identity_claim'
        assert decision['properties']['subjects']['maxItems'] == 0
    assert model.model_json_schema() == static  # Existing first-request prompts keep their schema.
    constraints = automatic.review_response_constraints(units, 6, original=original)
    assert json.dumps(schema, ensure_ascii=False) in constraints


def gap_fixture(tmp_path, monkeypatch):
    import autoclip.automatic as automatic

    video = tmp_path / 'source.mp4'
    video.write_bytes(b'source')
    frame = tmp_path / 'frame.jpg'
    frame.write_bytes(b'actual frame')
    monkeypatch.setattr(automatic, 'contact_sheet', lambda *a: frame)
    monkeypatch.setattr(automatic, 'subtitle_evidence_sheet', lambda *a: frame)
    monkeypatch.setattr(automatic, 'read_scene_observations', lambda llm, sheet, times, retries: [
        {'frame_id': i, 'visible': '主客仍在交谈'} for i in range(len(times))])
    plan = Plan(episode_summary='', genre='', style='', clips=[Clip(
        title='问话被剪断，回应为何变了', angle='来客说明来意', hook='这段回应接上了谁的问话', beats=[
            Beat(mode='original', start=730, end=746, text='', evidence_ids=[0, 1], reason='主客见面'),
            Beat(mode='original', start=752, end=765, text='', evidence_ids=[3, 4], reason='继续问答'),
            Beat(mode='narration', start=765, end=775, text='来人说明自己是来送信的。', evidence_ids=[5], reason='交代请求'),
            Beat(mode='narration', start=775, end=785, text='主人随后请客人入内。', evidence_ids=[6], reason='交代回应')])])
    lines = [Line(id=i, start=a, end=b, text=text) for i, (a, b, text) in enumerate([
        (731, 735, '客人来了'), (744, 746, '来者何人'), (746, 752, '请先说明你的来意'),
        (752, 755, '回老爷，我是送信来的'), (760, 764, '请进'),
        (766, 770, '我是来送信的'), (776, 780, '请到里面说话')])]
    voices = {'source': source_identity(video), 'transcript_digest': digest([s.model_dump() for s in lines]), 'lines': []}
    return automatic, video, plan, lines, voices


GAP = '原声746–752秒的实际时间缺口删掉必要问话，后段回应接到了不对应的上句。'


def parse_group(prompt):
    return json.JSONDecoder().raw_decode(prompt.split('\nunits=', 1)[1])[0]


def review_response(group, *, invalid=False):
    original = group[0]['id'].startswith('original-')
    return {'units': [{
        'id': unit['id'], 'verdict': 'contradicted' if original else 'supported',
        'reason': GAP if original else '原片支持此概括',
        'evidence_ids': unit.get('evidence_ids', [0]), 'frame_ids': [0],
        'identity_basis': 'no_identity_claim', 'subjects': []} for unit in group],
        'issues': [{'id': 'missing-746-752' if invalid else group[0]['id'], 'message': GAP}] if original else []}


def assert_constraints(automatic, prompt):
    group = parse_group(prompt)
    times = json.JSONDecoder().raw_decode(prompt.split('\ntimes=', 1)[1])[0]
    assert automatic.review_response_constraints(group, len(times), original=True) in prompt
    assert 'missing-746-752' in prompt
    assert all(u['id'] in prompt for u in group)


def assert_gap_rejected(report):
    assert report['passed'] is False
    assert {'original-0', 'original-1'} <= {i['id'] for i in report['issues']}
    assert all(i['id'] in {'original-0', 'original-1'} for i in report['issues'])
    assert all(GAP == i['message'] for i in report['issues'])


def test_out_of_batch_gap_id_is_reasked_and_corrected_rejection_survives(tmp_path, monkeypatch):
    automatic, video, plan, lines, voices = gap_fixture(tmp_path, monkeypatch)
    before = plan.model_dump()
    prompts = []

    class Reviewer:
        def json(self, system, prompt, images):
            group = parse_group(prompt)
            if not group[0]['id'].startswith('original-'):
                return review_response(group)
            prompts.append(prompt)
            return review_response(group, invalid=len(prompts) == 1)

    report = automatic.audit_plan(video, plan, lines, voices, tmp_path, Settings(repair_attempts=1), Reviewer())
    assert len(prompts) == 2
    group = parse_group(prompts[0])
    times = json.JSONDecoder().raw_decode(prompts[0].split('\ntimes=', 1)[1])[0]
    assert automatic.review_response_constraints(group, len(times), original=True) not in prompts[0]
    assert_constraints(automatic, prompts[1])
    failure = read_json(next(tmp_path.glob('automatic-review/*/batch-5-protocol-0.json')))
    assert 'missing-746-752' in failure['error']
    assert all(u['id'] in failure['error'] for u in group)
    assert failure['raw']['units'][0]['verdict'] == 'contradicted'
    assert_gap_rejected(report)
    assert plan.model_dump() == before
    assert not (tmp_path / 'plan-approved.json').exists()


def test_failed_cached_batch_resumes_with_specific_contract_and_preserves_real_gap(tmp_path, monkeypatch):
    automatic, video, plan, lines, voices = gap_fixture(tmp_path, monkeypatch)
    before = plan.model_dump()

    class CachedReviewer:
        def __init__(self):
            self.cache, self.original_requests = {}, []
            self.recover = False

        def json(self, system, prompt, images):
            if prompt not in self.cache:
                group = parse_group(prompt)
                original = group[0]['id'].startswith('original-')
                if original:
                    self.original_requests.append(prompt)
                self.cache[prompt] = review_response(group, invalid=original and not self.recover)
            return deepcopy(self.cache[prompt])

    reviewer = CachedReviewer()
    cfg = Settings(repair_attempts=1)
    with pytest.raises(ValueError, match='格式或引用校验失败'):
        automatic.audit_plan(video, plan, lines, voices, tmp_path, cfg, reviewer)
    assert len(reviewer.original_requests) == 2
    resume = read_json(next(tmp_path.glob('automatic-review/*/batch-5-protocol-resume.json')))
    assert resume['round'] == 1 and 'missing-746-752' in resume['error']
    assert all(uid in resume['error'] for uid in ('original-0', 'original-1'))
    assert not (tmp_path / 'automatic-review.json').exists()
    assert plan.model_dump() == before

    reviewer.recover = True
    report = automatic.audit_plan(video, plan, lines, voices, tmp_path, cfg, reviewer)
    assert 3 <= len(reviewer.original_requests) <= 4  # A real cut rejection may also get one boundary recheck.
    restored_prompt = reviewer.original_requests[2]
    assert '恢复轮次1' in restored_prompt
    assert_constraints(automatic, restored_prompt)
    assert json.dumps(resume['raw'], ensure_ascii=False) in restored_prompt
    assert_gap_rejected(report)
    assert plan.model_dump() == before

    requests = len(reviewer.cache)
    assert_gap_rejected(automatic.audit_plan(video, plan, lines, voices, tmp_path, cfg, reviewer))
    assert len(reviewer.cache) == requests  # Resume seed is stable enough to reuse its valid rejection.
    assert plan.model_dump() == before
    assert not (tmp_path / 'plan-approved.json').exists()
