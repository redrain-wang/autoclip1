import pytest

from autoclip.config import Settings
from autoclip.models import Beat, Clip
from autoclip.production import content_rejection
from autoclip.repair import RepairNeeded, preflight_voice, prepare_voice, repair_context, timing_report, voice_fits


def sample_clip():
    return Clip(title="配音预检", angle="检查真实时间", hook="发生了什么", beats=[
        Beat(mode="narration", start=0, end=3.6, text="需要更长画面的口播", evidence_ids=[0], reason="依据"),
        Beat(mode="original", start=4, end=14, evidence_ids=[1], reason="原声"),
        Beat(mode="narration", start=14, end=24, text="另一段解说", evidence_ids=[2], reason="依据"),
        Beat(mode="original", start=25, end=35, evidence_ids=[3], reason="原声"),
    ])


def test_real_user_failure_returns_specific_beat_and_total_without_mutation():
    clip=sample_clip()
    before=clip.model_dump()
    cfg=Settings(target_seconds=190,min_seconds=150,max_seconds=240)
    report=timing_report(clip,{0:6.1,2:9.65},cfg)
    assert report["issues"][0]["code"]=="voice_alignment"
    assert report["issues"][0]["beat"]==0
    assert report["beats"][0]["allowed_source_seconds"][0] > 3.6
    assert report["measured_total_seconds"]==36.45
    assert report["issues"][-1]["code"]=="duration_short"
    assert clip.model_dump()==before


def test_valid_measured_timing_passes_without_rewriting():
    clip=sample_clip()
    cfg=Settings(target_seconds=45,min_seconds=30,max_seconds=60)
    report=timing_report(clip,{0:3.0,2:9.65},cfg)
    assert report["issues"]==[]
    assert report["beats"][1]["output_seconds"]==10


def test_preflight_persists_diagnostics_before_any_encoding(tmp_path,monkeypatch):
    from autoclip import render, util
    monkeypatch.setattr(render,"synthesize",lambda *args:tmp_path/"voice.mp3")
    monkeypatch.setattr(util,"duration",lambda _:6.1)
    with pytest.raises(RepairNeeded) as result:
        preflight_voice(sample_clip(),tmp_path,Settings(target_seconds=190,min_seconds=150,max_seconds=240),0)
    assert (tmp_path/"voice-preflight-0.json").exists()
    assert not list(tmp_path.rglob("*.mp4"))
    assert content_rejection(result.value)
    assert repair_context(result.value,None,Settings())==result.value.report


def test_unknown_media_and_service_errors_are_not_editorial_retries():
    assert not content_rejection(RuntimeError("已达到本次 API 调用次数上限"))
    assert not content_rejection(RuntimeError("配音服务失败"))
    assert not content_rejection(RuntimeError("ffmpeg failed: disk full"))


def test_retry_budget_is_finite():
    with pytest.raises(ValueError):
        Settings(repair_attempts=99)


def test_one_frame_tolerance_does_not_hide_real_voice_overruns():
    cfg=Settings(target_seconds=190)
    assert voice_fits(7.35,5.24,cfg)
    assert not voice_fits(7.5,5.24,cfg)
    assert not voice_fits(6.45,3.6,cfg)


@pytest.mark.parametrize("passed", [True,False])
def test_local_voice_repair_requires_factual_approval_and_keeps_original_shots(tmp_path,monkeypatch,passed):
    from autoclip import render, util
    clip=sample_clip()
    before=clip.model_dump()
    cfg=Settings(target_seconds=45,min_seconds=30,max_seconds=60)
    monkeypatch.setattr(render,"synthesize",lambda text,*args:text)
    monkeypatch.setattr(util,"duration",lambda text:3.0 if text=="短口播" else 6.1 if text==clip.beats[0].text else 9.65)
    monkeypatch.setattr(render,"fit_narration",lambda *args:("短口播",tmp_path/"voice.mp3",3.0))
    class Reviewer:
        def json(self,system,prompt,images=None):
            assert '"before"' in prompt and '"evidence"' in prompt
            return {"passed":passed,"issues":[] if passed else ["事实不符"]}
    if passed:
        revised=prepare_voice(clip,tmp_path,cfg,Reviewer(),[],0)
        assert revised.beats[0].text=="短口播"
        assert revised.beats[1:]==clip.beats[1:]
        assert (tmp_path/"voice-preflight-0-repaired.json").exists()
    else:
        with pytest.raises(RepairNeeded) as error:
            prepare_voice(clip,tmp_path,cfg,Reviewer(),[],0)
        assert error.value.report["stage"]=="voice_semantic"
    assert clip.model_dump()==before


def test_failed_plan_is_retained_for_targeted_repair(tmp_path):
    from autoclip.director import create_plan
    from autoclip.models import Line
    raw={"episode_summary":"摘要","genre":"历史","style":"分析","clips":[sample_clip().model_dump()]}
    class Writer:
        def json(self,*args):
            return raw
    lines=[Line(id=i,start=i*10,end=i*10+9,text="字幕") for i in range(4)]
    with pytest.raises(RepairNeeded) as error:
        create_plan(tmp_path/"source.mp4",tmp_path,lines,100,[],
                    Settings(clips=1,target_seconds=190,min_seconds=150,max_seconds=240,vision=False),Writer(),[])
    assert error.value.report["stage"]=="plan_validation"
    assert error.value.report["proposed_clip"]==raw["clips"][0]
    assert error.value.report["clip"]["title"]==raw["clips"][0]["title"]
    assert any(i["code"]=="duration_short" for i in error.value.report["issues"])


def test_compiler_padding_cannot_duplicate_adjacent_original_dialogue():
    from autoclip.director import compile_plan
    from autoclip.models import Line
    raw=sample_clip().model_dump()
    raw["beats"][1]["end"]=9
    raw["beats"].insert(2,{"mode":"original","start":9,"end":14,"text":"","evidence_ids":[2],"reason":"下一句"})
    lines=[Line(id=i,start=a,end=b,text="对白") for i,(a,b) in enumerate([(0,3),(4,9),(9,14),(14,24),(25,35)])]
    result=compile_plan({"clips":[raw],"episode_summary":"摘要","genre":"历史","style":"分析"},lines).clips[0]
    original=result.beats[1]
    assert original.mode=="original"
    assert original.start==pytest.approx(3.92)
    assert original.end==pytest.approx(14.12)
    assert set(original.evidence_ids)=={1,2}
    assert len(result.beats)==4


def test_story_boundary_trims_padding_but_never_cuts_an_utterance():
    from autoclip.director import compile_plan
    from autoclip.models import Line
    raw={"clips":[sample_clip().model_dump()],"episode_summary":"摘要","genre":"历史","style":"分析"}
    lines=[Line(id=i,start=a,end=b,text="对白") for i,(a,b) in enumerate([(0,3),(4,14),(14,24),(25,35)])]
    exact=compile_plan(raw,lines,bounds=(0,35))
    assert exact.clips[0].beats[-1].end==35
    crossing=compile_plan(raw,lines,bounds=(0,34.9))
    assert crossing.clips[0].beats[-1].end>35  # validation must reject; no truncated speech


def test_repair_evidence_candidates_exclude_future_speech_and_do_not_change_ids():
    from autoclip.models import Line
    from autoclip.repair import enrich_constraints
    raw=sample_clip().model_dump()
    raw["beats"][2]["evidence_ids"]=[99]
    lines=[Line(id=1,start=4,end=14,text="刚说完的事实"),
           Line(id=2,start=14,end=24,text="当前事实"),Line(id=99,start=24,end=29,text="未来事实")]
    report=enrich_constraints({"clip":raw,"issues":[{"code":"evidence","message":"clips[0].beats[2] 没有落在画面区间内的字幕证据"}]},lines,(0,35))
    detail=report["affected_shots"][0]
    assert {s['id'] for s in detail['eligible_evidence']}=={1,2}
    assert detail['cited_lines'][0]['id']==99
    assert raw['beats'][2]['evidence_ids']==[99]
    assert report['outro_budget']['end']==35
