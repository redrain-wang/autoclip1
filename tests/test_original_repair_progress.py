"""A source-audio repair must change what viewers actually see and hear."""
from copy import deepcopy
import json

import pytest

from autoclip.models import Plan, validate_plan
from autoclip.narrative import NARRATIVE_CRITERIA, NARRATIVE_POLICY, NARRATIVE_REVIEW_POLICY
from autoclip.repair import RepairNeeded
from autoclip.util import digest, read_json, write_json
from test_commentary_balance import fixture


def production_fixture():
    plan, lines, cfg, _ = fixture()
    plan.clips[0].beats[0].text += '据' * 30  # Enough developed narration for the final gate.
    issue = {'id': 'original-36', 'message': '原声尾部多留了无关过场，应在完整对白结束后收住'}
    return plan, lines, cfg, issue


def ineffective_patch(plan, kind):
    beat = plan.clips[0].beats[36]
    change = {'id': 'original-36'}
    if kind == 'reason':
        change['reason'] = '已按要求在完整对白后收住，保留必要承接'
    elif kind == 'evidence':
        change['evidence_ids'] = beat.evidence_ids[:1]
    else:
        change['start'], change['end'] = beat.start, beat.end
    return {'changes': [change]}


@pytest.mark.parametrize('kind', ['reason', 'evidence'])
def test_original_metadata_only_patch_retries_before_accepting_real_source_cut(tmp_path, kind):
    import autoclip.automatic as automatic

    plan, lines, cfg, issue = production_fixture()
    before = plan.model_dump()
    invalid = ineffective_patch(plan, kind)
    valid = {'changes': [{'id': 'original-36', 'end': 1478}]}
    prompts = []

    class Model:
        def json(self, system, prompt):
            prompts.append(prompt)
            return deepcopy(invalid if len(prompts) == 1 else valid)

    revised = automatic.repair_plan(plan, [issue], lines, {}, tmp_path, cfg, Model(), 0,
                                    source_seconds=1480)
    assert len(prompts) == 2
    rejected = read_json(tmp_path / 'automatic-repairs/0/original-36/attempt-0.json')
    assert 'error' in rejected and rejected.get('status') != 'valid_patch'
    assert '原声' in rejected['error'] and '剪辑区间' in rejected['error']
    assert rejected['error'] in prompts[1]
    assert json.dumps(invalid, ensure_ascii=False) in prompts[1]
    assert prompts[1] != prompts[0]  # A cached ineffective response cannot silently pass.
    accepted = read_json(tmp_path / 'automatic-repairs/0/original-36/attempt-1.json')
    assert accepted['status'] == 'valid_patch' and accepted['raw'] == valid
    assert revised.clips[0].beats[36].end == 1478
    assert revised.clips[0].beats[36].start == plan.clips[0].beats[36].start
    assert revised.clips[0].beats[36].evidence_ids == plan.clips[0].beats[36].evidence_ids
    assert revised.clips[0].beats[36].reason == plan.clips[0].beats[36].reason
    assert revised.clips[0].beats[:36] == plan.clips[0].beats[:36]
    assert plan.model_dump() == before
    assert read_json(tmp_path / 'pending-repair.json')['status'] == 'requires_full_measurement_and_review'
    assert not (tmp_path / 'plan-approved.json').exists()


@pytest.mark.parametrize('kind', ['reason', 'evidence', 'unchanged'])
def test_exhausted_ineffective_original_repairs_do_not_repeat_whole_film_audit(tmp_path, monkeypatch, kind):
    import autoclip.series as series

    plan, lines, cfg, issue = production_fixture()
    before = plan.model_dump()
    events, prompts = [], []
    invalid = ineffective_patch(plan, kind)

    def measure(video, path, transcript, cfg, root):
        events.append('measure')
        current = Plan.model_validate(read_json(path))
        write_json(root / 'plan-timed.json', current.model_dump())
        return {'issues': [], 'beats': [
            {'output_seconds': len(b.text) / 4.5 if b.mode == 'narration' else b.end - b.start}
            for b in current.clips[0].beats]}

    def audit(*args):
        events.append('audit')
        return {'passed': False, 'issues': [issue]}

    def preflight(clip,*args,**kwargs):
        events.append('preflight')
        assert not (tmp_path/'plan-approved.json').exists()
        return {'policy':NARRATIVE_POLICY,'review_policy':NARRATIVE_REVIEW_POLICY,
                'clip_digest':digest(clip.model_dump()),'passed':True,'issues':[],
                'checks':[{'criterion':c,'passed':True,'reason':'编辑预检通过，不代表原片通过'}
                          for c in sorted(NARRATIVE_CRITERIA)]}

    class Model:
        def json(self, system, prompt):
            assert '局部修稿编辑' in system
            events.append('write')
            prompts.append(prompt)
            return deepcopy(invalid)

    monkeypatch.setattr(series, 'measure_sample_plan', measure)
    monkeypatch.setattr(series, 'audit_plan', audit)
    monkeypatch.setattr(series, 'duration', lambda _: 1480)
    monkeypatch.setattr(series, 'review_commentary', preflight)
    with pytest.raises(RepairNeeded) as caught:
        series.complete_long_plan(tmp_path / 'video.mp4', plan, lines, {}, tmp_path, cfg, Model())

    assert caught.value.report['stage'] == 'automatic_partial'
    assert caught.value.report['clip'] == plan.clips[0].model_dump()
    assert events == ['measure', 'preflight', 'audit', 'write', 'write']
    assert len(prompts) == cfg.repair_attempts + 1 == 2
    assert '上轮局部修订未通过程序检查' in prompts[1]
    assert json.dumps(invalid, ensure_ascii=False) in prompts[1]
    for number in range(2):
        record = read_json(tmp_path / f'automatic-repairs/0/original-36/attempt-{number}.json')
        assert 'error' in record and record.get('status') != 'valid_patch'
    assert read_json(tmp_path / 'automatic-repair-0.json')['changes'] == []
    assert read_json(tmp_path / 'repair-budget.json')['used']['content'] == 1
    assert plan.model_dump() == before
    assert not (tmp_path / 'pending-repair.json').exists()
    assert not (tmp_path / 'partial-repair-0.json').exists()
    assert not (tmp_path / 'plan-approved.json').exists()
    assert read_json(tmp_path/'editorial-preflight.json')['status']=='passed'


def test_formal_original_trim_can_await_measurement_below_the_rough_duration_floor(tmp_path, monkeypatch):
    import autoclip.automatic as automatic

    plan, lines, cfg, issue = production_fixture()
    for beat in plan.clips[0].beats[18:36]:
        beat.end = beat.start + 30
    validate_plan(plan, lines, 1480, 1, cfg.target_seconds, cfg.min_seconds, cfg.max_seconds, cfg.opening)
    duration_checks = []

    def checked(*args, **kwargs):
        duration_checks.append(kwargs.get('check_duration', True))
        return validate_plan(*args, **kwargs)

    class Model:
        def json(self, *args):
            return {'changes': [{'id': 'original-36', 'end': 1460}]}

    monkeypatch.setattr(automatic, 'validate_plan', checked)
    revised = automatic.repair_plan(plan, [issue], lines, {}, tmp_path, cfg, Model(), 0,
                                    source_seconds=1480)
    assert duration_checks == [False]
    assert revised.clips[0].beats[36].end == 1460
    # Only the rough estimate is deferred: source bounds, order and evidence were
    # still checked by the real validator, and no final approval was produced.
    with pytest.raises(ValueError, match='预计时长'):
        validate_plan(revised, lines, 1480, 1, cfg.target_seconds, cfg.min_seconds, cfg.max_seconds, cfg.opening)
    pending = read_json(tmp_path / 'pending-repair.json')
    assert pending['plan'] == revised.model_dump()
    assert pending['status'] == 'requires_full_measurement_and_review'
    assert not (tmp_path / 'plan-approved.json').exists()
