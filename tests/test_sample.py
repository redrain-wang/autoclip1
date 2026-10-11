from pathlib import Path

import pytest

from autoclip.identity import units_for_clip, file_hash
from autoclip.models import Line, Plan, Clip, Beat
from autoclip.sample import check_review, FrozenScript
from autoclip.util import digest, source_identity


def fixture(tmp_path):
    video = tmp_path / 'source.mp4'
    video.write_bytes(b'source')
    frame = tmp_path / 'frame.jpg'
    frame.write_bytes(b'frame')
    lines = [Line(id=1, start=10, end=12, text='借银问话')]
    plan = Plan(episode_summary='例子', genre='历史', style='试片', clips=[Clip(
        title='一两银子的问话', angle='已还清为何还问', hook='为何还问', beats=[
            Beat(mode='narration', start=10+i*10, end=20+i*10, text='解释与事实必须分开', evidence_ids=[1], reason='试片')
            for i in range(4)])])
    review = {'mode': 'development_reviewed_sample', 'reviewer': 'fixture',
              'source': source_identity(video), 'plan_digest': digest(plan.model_dump()),
              'transcript_digest': digest([s.model_dump() for s in lines]),
              'units': [{'id': u['id'], 'text': u['text'], 'decision': 'accepted', 'basis': '已核对', 'evidence_ids': [1]}
                        for u in units_for_clip(plan.clips[0])],
              'visual_evidence': [{'path': str(frame), 'sha256': file_hash(frame)}],
              'limitations': ['开发参与，不是自动审片验收']}
    return review, video, plan, lines


def test_exact_development_sample_can_be_reproduced(tmp_path):
    check_review(*fixture(tmp_path))


@pytest.mark.parametrize('change', ['source', 'plan', 'transcript', 'missing_unit', 'unaccepted', 'invalid_evidence', 'frame', 'mode'])
def test_stale_or_incomplete_review_cannot_render(tmp_path, change):
    review, video, plan, lines = fixture(tmp_path)
    if change == 'source': video.write_bytes(b'changed source')
    if change == 'plan': plan.clips[0].beats[0].text = '新的说法'
    if change == 'transcript': lines[0].text = '新的字幕'
    if change == 'missing_unit': review['units'].pop()
    if change == 'unaccepted': review['units'][0]['decision'] = 'pending'
    if change == 'invalid_evidence': review['units'][0]['evidence_ids'] = [999]
    if change == 'frame': Path(review['visual_evidence'][0]['path']).write_bytes(b'changed frame')
    if change == 'mode': review['mode'] = 'automatic'
    with pytest.raises(ValueError):
        check_review(review, video, plan, lines)


def test_tts_may_not_silently_rewrite_a_reviewed_sample():
    with pytest.raises(ValueError, match='禁止自动改写'):
        FrozenScript().json('rewrite after synthesis')
