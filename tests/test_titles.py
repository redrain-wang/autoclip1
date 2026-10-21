import copy

import pytest

from autoclip.titles import title_changes, title_issues


def selection():
    return {'candidates': [
        {'title': '道士称八爷有帝王命，为何反被抓？', 'evidence_ids': [1, 2], 'rationale': '预测与拿人的反差'},
        {'title': '八爷为何要抓给自己算命的道士？', 'evidence_ids': [1, 2], 'rationale': '拿人的原因'},
        {'title': '道士当众谈帝王命，八爷下令拿人', 'evidence_ids': [1, 2], 'rationale': '具体行动'}],
        'selected': 0, 'selection_reason': '保留预测归属，突出成片展开的具体冲突'}


@pytest.mark.parametrize('title', ['长'*25, ' 标题', '标题\n换行', '标题\r换行'])
def test_title_format_cannot_pass_on_model_opinion(title):
    assert title_issues(title)[0]['id'] == 'title'


def test_cover_reads_only_approved_title_citations(tmp_path):
    from autoclip.titles import reviewed_title_evidence, TITLE_POLICY
    from autoclip.util import write_json
    path=tmp_path/'batch.json'
    write_json(path,{'raw':{'units':[
        {'id':'title','verdict':'supported','evidence_ids':[3,1,3]},
        {'id':'hook','verdict':'supported','evidence_ids':[99]}]}})
    report={'passed':True,'title_policy':TITLE_POLICY,'batches':[str(path)]}
    assert reviewed_title_evidence(report)==[1,3]
    with pytest.raises(ValueError):reviewed_title_evidence({**report,'passed':False})


@pytest.mark.parametrize('change', ['unselected_evidence', 'out_of_range', 'bool_index', 'duplicate', 'long', 'extra_patch'])
def test_invalid_title_choices_cannot_modify_the_plan(change):
    raw = selection()
    if change == 'unselected_evidence': raw['candidates'][1]['evidence_ids'] = [99]
    if change == 'out_of_range': raw['selected'] = 3
    if change == 'bool_index': raw['selected'] = True
    if change == 'duplicate': raw['candidates'][1]['title'] = raw['candidates'][0]['title']
    if change == 'long': raw['candidates'][0]['title'] = '长' * 25
    if change == 'extra_patch': raw['changes'] = [{'id': 'beat-0', 'text': '不允许改稿'}]
    with pytest.raises(ValueError): title_changes(raw, {1, 2})


def test_title_repair_is_recorded_but_never_approves_or_rewrites_footage(tmp_path):
    from autoclip.automatic import repair_plan
    from autoclip.config import Settings
    from autoclip.models import Beat, Clip, Line, Plan
    from autoclip.util import read_json
    beats = [Beat(mode='original', start=i*10, end=(i+1)*10,
                  evidence_ids=[i], reason='完整原声') for i in range(4)]
    plan = Plan(episode_summary='概括', genre='剧情', style='剧情优先', clips=[
        Clip(title='废储之后：观望与妖道', angle='争储', hook='八爷拿人', beats=beats)])
    before = copy.deepcopy(plan.model_dump())
    lines = [Line(id=i, start=i*10, end=(i+1)*10, text='已选用对白') for i in range(4)]
    class Model:
        def json(self, system, prompt):
            assert '提出3个实质不同的候选' in prompt
            assert '未剪入情节或后集结果' in prompt
            return selection()
    revised = repair_plan(plan, [{'id': 'title', 'message': '目录式标签，没有具体看点'}],
                          lines, {}, tmp_path, Settings(vision=False), Model(), 0)
    assert plan.model_dump() == before
    assert revised.clips[0].title == selection()['candidates'][0]['title']
    expected = copy.deepcopy(before)
    expected['clips'][0]['title'] = revised.clips[0].title
    assert revised.model_dump() == expected
    saved = read_json(tmp_path/'automatic-repairs/0/title/candidates-0.json')
    assert saved['raw'] == selection()
    assert saved['status'] == 'draft_requires_source_review'
    assert not (tmp_path/'plan-approved.json').exists()
