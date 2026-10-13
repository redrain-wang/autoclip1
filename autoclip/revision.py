"""Small explicit narration edits produce a new draft, never overwrite a film."""

from pathlib import Path

from .models import Plan
from .util import digest, read_json, write_json


def parse_time(value):
    parts = str(value).split(':')
    if not 1 <= len(parts) <= 3:
        raise ValueError('时间用秒数或分:秒、时:分:秒')
    values = [float(x) for x in parts]
    if any(x < 0 for x in values) or any(x >= 60 for x in values[1:]):
        raise ValueError('时间格式无效')
    result = 0
    for x in values:
        result = result*60+x
    return result


def edit_narration(plan_path, output, text, beat=None, at=None, manifest=None):
    output = Path(output)
    if output.exists():
        raise ValueError('修订输出已存在，请使用新文件名')
    plan = Plan.model_validate(read_json(plan_path))
    if len(plan.clips) != 1:
        raise ValueError('此入口用于单条成片计划')
    if (beat is None) == (at is None):
        raise ValueError('用段号或成片时间定位，二选一')
    before = digest(plan.model_dump())
    if at is not None:
        if manifest is None:
            raise ValueError('按成片时间修改需要该片manifest.json')
        timeline = read_json(manifest)['timeline']
        if len(timeline) != len(plan.clips[0].beats) or any(
                any(t.get(k) != b.model_dump()[k] for k in ('start', 'end', 'mode', 'text'))
                for t, b in zip(timeline, plan.clips[0].beats)):
            raise ValueError('成片时间线与此计划不匹配，请使用实际成片的定稿计划')
        moment = parse_time(at)
        beat = next((i for i, t in enumerate(timeline) if t['output_start'] <= moment < t['output_end']), None)
    if beat is None or not 0 <= beat < len(plan.clips[0].beats):
        raise ValueError('定位超出成片或段号范围')
    target = plan.clips[0].beats[beat]
    if target.mode != 'narration':
        raise ValueError('这个位置是原声，请定位到要修改的解说段')
    old = target.text
    target.text = text.strip()
    plan = Plan.model_validate(plan.model_dump())
    record = {'status': 'edited_draft_not_remeasured', 'base_plan': str(Path(plan_path).resolve()),
              'base_digest': before, 'new_digest': digest(plan.model_dump()),
              'beat': beat, 'source_start': target.start, 'source_end': target.end,
              'before': old, 'after': target.text,
              'next': '使用现有测时和内容核对流程；旧核对记录不自动批准新文字'}
    write_json(output, plan.model_dump())
    write_json(output.with_suffix('.change.json'), record)
    return record
