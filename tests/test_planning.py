from pathlib import Path
import pytest
from autoclip.config import Settings
from autoclip.models import Line
from autoclip.planning import Structure, compile_outline, validate_structure


def fixture(tmp_path):
    video=tmp_path/'source.mp4';video.write_bytes(b'fixture')
    cfg=Settings(duration_profile='long',opening='original_teaser',target_seconds=300,min_seconds=30,max_seconds=600)
    structure=Structure(title='一件案子的责任',angle='选择',hook='为何如此',opening_start=100,opening_end=120,
        opening_reason='连续高光',chapters=[{'title':'第一章','start':0,'end':80,'purpose':'完整冲突'}])
    lines=[Line(id=i,start=i*20+1,end=i*20+15,text='当前对白') for i in range(4)]
    lines.append(Line(id=10,start=100,end=119,text='已前置高光'))
    slots=[{'mode':'narration' if i%2==0 else 'original','start':i*20,'end':i*20+20,
            'instruction':'解释选择' if i%2==0 else '完整对白','evidence_ids':[i],
            'target_chars':90 if i%2==0 else 0} for i in range(4)]
    return video,structure,[{'beats':slots}],lines,140,cfg


def test_automatic_outline_uses_existing_timeline_rules_and_only_first_bridge_can_borrow_teaser(tmp_path):
    args=fixture(tmp_path);validate_structure(args[1],0,140,args[-1])
    result=compile_outline(*args)
    beats=result['chapters'][0]['beats']
    assert [b['id'] for b in beats]==list(range(5))
    assert 10 in beats[1]['allowed_evidence_ids']
    assert 10 not in beats[3]['allowed_evidence_ids']
    assert result['origin']=='automatic_outline_not_reviewed'


@pytest.mark.parametrize('problem',['future','repeat_teaser','out_of_chapter'])
def test_bad_automatic_shots_fail_before_writing_narration(tmp_path,problem):
    args=fixture(tmp_path);slot=args[2][0]['beats'][2]
    if problem=='future':slot['evidence_ids']=[10]
    elif problem=='repeat_teaser':
        args[1].chapters[0].end=140;slot.update(start=100,end=120,evidence_ids=[10])
    else:slot['end']=90
    with pytest.raises(ValueError):compile_outline(*args)


def test_structure_cannot_overlap_chapters_or_leave_source(tmp_path):
    args=fixture(tmp_path);structure=args[1]
    structure.chapters.append(structure.chapters[0].model_copy())
    with pytest.raises(ValueError,match='章节'):validate_structure(structure,0,140,args[-1])


def test_reports_all_slot_errors_for_one_bounded_repair(tmp_path):
    args=fixture(tmp_path)
    args[2][0]['beats'][0]['end']=45
    args[2][0]['beats'][2]['evidence_ids']=[10]
    with pytest.raises(ValueError) as caught:
        compile_outline(*args)
    assert '须1–40秒' in str(caught.value)
    assert '却引用其后台词' in str(caught.value)
    assert '重叠' in str(caught.value)


@pytest.mark.parametrize('problem, message', [
    ('overlap', '不能重复或倒置'),
    ('repeat_teaser', '正文不能重复播放'),
    ('missing_direct_evidence', '不存在的证据'),
    ('unavailable_direct_evidence', '直接证据未包含'),
    ('empty', '非空章节'),
    ('unrelated_evidence', '画面区间内'),
    ('low_confidence', '低置信度'),
])
def test_supplied_outline_fails_before_continuity_or_model(tmp_path, monkeypatch, problem, message):
    from autoclip import outline as module, continuity
    from autoclip.util import write_json
    args = fixture(tmp_path)
    video, _, _, lines, seconds, cfg = args
    outline = compile_outline(*args)
    slots = outline['chapters'][0]['beats']
    if problem == 'overlap':
        slots[3]['start'] = 39
    elif problem == 'repeat_teaser':
        slots[-1].update(start=100, end=120)
    elif problem == 'missing_direct_evidence':
        slots[2]['evidence_ids'] = [999]
    elif problem == 'unavailable_direct_evidence':
        slots[2]['allowed_evidence_ids'] = []
    elif problem == 'empty':
        outline['chapters'] = []
    elif problem == 'unrelated_evidence':
        slots[-1]['evidence_ids'] = [0]
    elif problem == 'low_confidence':
        lines[1].confidence = .1
    write_json(tmp_path/'source.json', outline['source'])
    write_json(tmp_path/'transcript.json', [s.model_dump() for s in lines])
    write_json(tmp_path/'outline.json', outline)
    monkeypatch.setattr(module, 'duration', lambda _: seconds)
    monkeypatch.setattr(continuity, 'load_continuity', lambda *a: pytest.fail('检查失败前不能准备前情'))
    class Model:
        def json(self, *args):
            pytest.fail('无效时间线不能调用模型')
    with pytest.raises(ValueError, match=message):
        module.compose_outline(video, tmp_path/'transcript.json', tmp_path/'outline.json',
                               tmp_path/'run', cfg, Model())
    assert not (tmp_path/'run').exists()


def test_outline_precheck_allows_teaser_bridge_and_defers_unwritten_duration(tmp_path, monkeypatch):
    from autoclip import outline as module
    from autoclip.util import write_json
    args = fixture(tmp_path)
    video, _, _, lines, seconds, cfg = args
    outline = compile_outline(*args)
    outline['chapters'][0]['beats'][1]['evidence_ids'] = [10]
    write_json(tmp_path/'source.json', outline['source'])
    write_json(tmp_path/'transcript.json', [s.model_dump() for s in lines])
    write_json(tmp_path/'outline.json', outline)
    monkeypatch.setattr(module, 'duration', lambda _: seconds)
    cfg.min_seconds = 500  # An unwritten narration placeholder cannot establish duration.
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    checked, _ = module.check_outline(video, tmp_path/'transcript.json', tmp_path/'outline.json', cfg)
    assert checked == outline
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir()}
