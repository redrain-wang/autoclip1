import pytest

from autoclip.config import Settings
from autoclip.models import Beat, Clip, Line
from autoclip.recut import cut_candidates, original_remainder, safe_cut, try_recut
from autoclip.repair import RepairNeeded, timing_report


def fixture():
    lines = [Line(id=i, start=a, end=b, text='完整对白') for i,(a,b) in enumerate(
        [(0,7.8),(8.1,12),(13,17.9),(18,26.9),(27.1,32),(33,37.9)])]
    text = '这段解说依据眼前的完整对白，说明人物为什么处在两难之间，这也是这场冲突的关键。'
    clip = Clip(title='结尾切点测试', angle='解释冲突', hook='两难', beats=[
        Beat(mode='narration',start=0,end=8,text=text,evidence_ids=[0],reason='依据'),
        Beat(mode='original',start=8,end=18,evidence_ids=[1,2],reason='原声'),
        Beat(mode='narration',start=18,end=27,text=text,evidence_ids=[3],reason='依据'),
        Beat(mode='original',start=27,end=38,evidence_ids=[4,5],reason='原声'),
        Beat(mode='narration',start=38,end=40,text=text,evidence_ids=[5],reason='总结')])
    cfg = Settings(target_seconds=45,min_seconds=30,max_seconds=60,vision=False)
    report = timing_report(clip,{0:6,2:8.5,4:5},cfg)
    return clip, lines, cfg, report


def test_outro_borrows_only_at_recognized_boundaries_and_keeps_text_and_outer_bounds():
    clip,lines,cfg,report = fixture()
    before = clip.model_dump()
    candidates = cut_candidates(clip,report,lines,cfg)
    assert candidates and not candidates[0]['timing']['issues']
    revised = Clip.model_validate(candidates[0]['clip'])
    assert revised.beats[-1].start < 38 and revised.beats[-1].end == 40
    assert revised.beats[0].start == 0
    assert revised.beats[-2].end == revised.beats[-1].start
    assert safe_cut(revised.beats[-1].start,lines)
    assert revised.beats[-2].evidence_ids == [4]
    assert [b.text for b in revised.beats] == [b.text for b in clip.beats]
    assert [b.evidence_ids for b in revised.beats if b.mode=='narration'] == [[0],[3],[5]]
    assert clip.model_dump() == before
    removed = candidates[0]['changes'][0]['removed_original_lines']
    assert [line['id'] for line in removed] == [5]


def test_low_confidence_speech_still_blocks_cut_and_cannot_leave_empty_original():
    clip,lines,cfg,report = fixture()
    lines.append(Line(id=99,start=30,end=39,text='不确定但不能当作静音',confidence=.1))
    assert not safe_cut(32,lines)
    assert original_remainder(clip.beats[3],27,32,lines) is None
    assert original_remainder(clip.beats[3],32.5,32.7,lines) is None
    assert cut_candidates(clip,report,lines,cfg) == []


def test_no_stealing_from_neighboring_narration():
    clip,lines,cfg,report = fixture()
    clip.beats[3] = Beat(mode='narration',start=27,end=38,text='连续口播不能擅自缩短',evidence_ids=[4,5],reason='依据')
    report = timing_report(clip,{0:6,2:8.5,3:10,4:5},cfg)
    assert cut_candidates(clip,report,lines,cfg) == []


def test_uses_unused_gap_before_removing_any_dialogue():
    clip,lines,cfg,_ = fixture()
    clip.beats[3].end = 32.1
    clip.beats[3].evidence_ids = [4]
    report = timing_report(clip,{0:6,2:8.5,4:5},cfg)
    candidates = cut_candidates(clip,report,lines,cfg)
    assert candidates
    assert candidates[0]['removed_original_seconds'] == 0
    assert candidates[0]['clip']['beats'][3] == clip.beats[3].model_dump()


@pytest.mark.parametrize('verdict',[{'passed':True,'issues':[]},{'passed':False,'issues':['删掉必要对白']},
                                  {'passed':True,'issues':['结论矛盾']},[],{'passed':True}])
def test_recut_requires_unambiguous_editorial_approval(tmp_path,monkeypatch,verdict):
    from autoclip import repair
    clip,lines,cfg,report = fixture()
    calls=[]
    monkeypatch.setattr(repair,'preflight_voice',lambda *a:calls.append('measured'))
    class Reviewer:
        def json(self,system,prompt,images):
            assert 'removed_original_lines' in prompt
            return verdict
    if verdict == {'passed':True,'issues':[]}:
        assert try_recut(clip,report,tmp_path,cfg,Reviewer(),lines,0,None) is not None
        assert calls == ['measured']
    else:
        with pytest.raises(RepairNeeded) as error:
            try_recut(clip,report,tmp_path,cfg,Reviewer(),lines,0,None)
        assert error.value.report['stage']=='timeline_semantic'
        assert error.value.report['beats']
        assert not calls


def test_too_short_after_recut_keeps_candidate_and_measurements_without_review(tmp_path):
    clip,lines,cfg,report = fixture()
    cfg.min_seconds = 40
    report = timing_report(clip,{0:6,2:8.5,4:5},cfg)
    with pytest.raises(RepairNeeded) as error:
        try_recut(clip,report,tmp_path,cfg,None,lines,0,None)
    assert error.value.report['stage'] == 'timeline_repair'
    assert error.value.report['timeline_repair']['editorial_review'] == 'pending'
    assert all(i['code']=='duration_short' for i in error.value.report['issues'])
    assert error.value.report['clip']['beats'][-1]['start'] < 38


def test_recut_media_or_service_error_is_not_editorial_rejection(tmp_path):
    clip,lines,cfg,report = fixture()
    class Unavailable:
        def json(self,*args):
            raise RuntimeError('模型调用失败，已重试：HTTP 503')
    with pytest.raises(RuntimeError):
        try_recut(clip,report,tmp_path,cfg,Unavailable(),lines,0,None)


def test_duplicate_and_bounds_are_rechecked_on_adjusted_footage():
    from types import SimpleNamespace
    from autoclip.production import check_footage
    clip,lines,cfg,report=fixture()
    previous=[{'timeline':[{'start':36,'end':37}]}]
    story=SimpleNamespace(start=0,end=40)
    # The initial clip already uses that original audio, so check a gap instead.
    clip.beats[3].end=32.1;clip.beats[3].evidence_ids=[4]
    report=timing_report(clip,{0:6,2:8.5,4:5},cfg)
    check_footage(clip,story,previous)
    revised=Clip.model_validate(cut_candidates(clip,report,lines,cfg)[0]['clip'])
    with pytest.raises(ValueError,match='重复'):
        check_footage(revised,story,previous)
    with pytest.raises(ValueError,match='超出'):
        check_footage(revised,SimpleNamespace(start=0,end=39),[])


def test_joint_search_reserves_outro_before_competing_neighbor():
    clip,lines,cfg,_=fixture()
    report=timing_report(clip,{0:6,2:12,4:5},cfg)
    candidates=cut_candidates(clip,report,lines,cfg)
    assert candidates and not candidates[0]['timing']['issues']
    revised=Clip.model_validate(candidates[0]['clip'])
    assert revised.beats[2].start < clip.beats[2].start
    assert revised.beats[4].start < clip.beats[4].start
    assert all(a.end <= b.start for a,b in zip(revised.beats,revised.beats[1:]))
    assert all(b.end-b.start >= 1 for b in revised.beats)


def test_production_preflight_accepts_only_reviewed_and_remeasured_recut(tmp_path,monkeypatch):
    from autoclip import render,util
    from autoclip.repair import prepare_voice
    clip,lines,cfg,_=fixture()
    clip.beats[3].end=32.1
    clip.beats[3].evidence_ids=[4]
    # Distinct texts allow the fake TTS to supply per-shot measurements.
    clip.beats[2].text += '中段'
    clip.beats[4].text += '结尾'
    durations={clip.beats[i].text:v for i,v in [(0,6),(2,8.5),(4,5)]}
    monkeypatch.setattr(render,'synthesize',lambda text,*a:text)
    monkeypatch.setattr(util,'duration',lambda text:durations[text])
    def forbidden(*args):
        pytest.fail('No full text rewrite is needed when safe cuts are enough')
    monkeypatch.setattr(render,'fit_narration',forbidden)
    class Reviewer:
        calls=0
        def json(self,*args):
            self.calls+=1
            return {'passed':True,'issues':[]}
    reviewer=Reviewer()
    revised=prepare_voice(clip,tmp_path,cfg,reviewer,lines,0)
    assert revised.beats[-1].start < 38
    assert reviewer.calls == 1
    assert (tmp_path/'voice-preflight-0-recut.json').exists()
    assert (tmp_path/'timeline-review-0.json').exists()
