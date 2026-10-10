import json
from pathlib import Path

import httpx
import pytest

from autoclip.config import Settings
from autoclip.director import compile_plan, correct_spelling
from autoclip.ingest import load_srt
from autoclip.llm import LLM
from autoclip.models import Beat, Clip, Line, Plan, validate_plan
from autoclip.render import ass_escape, render_clip
from autoclip.util import duration, source_identity, run


def fixture_plan():
    lines = [Line(id=i,start=i*10,end=(i+1)*10,text="有证据的对白") for i in range(4)]
    beats = [Beat(mode="narration" if i%2==0 else "original", start=i*10,end=(i+1)*10,
                  text="这是一个具体的矛盾，需要用画面和对白来说明它的起因和结果。" if i%2==0 else "", evidence_ids=[i],reason="本段对白证实") for i in range(4)]
    return lines, Plan(episode_summary="摘要",genre="历史",style="分析",clips=[Clip(title="谁在承担代价",angle="矛盾",hook="谁承担",beats=beats)])


def test_verified_spelling_changes_text_without_changing_evidence(tmp_path):
    lines, plan = fixture_plan()
    plan.clips[0].title = "吴先生的选择"
    plan.clips[0].beats[0].text = "吴先生分析了这件事。"
    original = plan.model_dump()
    path = tmp_path / "text_corrections.json"
    path.write_text(json.dumps({"origin": "原片字幕核实", "replacements": {"吴先生": "邬先生"}}))
    corrected = correct_spelling(plan, tmp_path)
    assert corrected.clips[0].title == "邬先生的选择"
    assert corrected.clips[0].beats[0].text == "邬先生分析了这件事。"
    assert plan.model_dump() == original
    for before, after in zip(plan.clips[0].beats, corrected.clips[0].beats):
        assert (before.start, before.end, before.evidence_ids, before.mode) == (after.start, after.end, after.evidence_ids, after.mode)
    path.write_text(json.dumps({"replacements": {"吴": "邬"}}))
    with pytest.raises(ValueError, match="依据"):
        correct_spelling(plan, tmp_path)


def test_auto_selection_can_decline_all_stories_and_reuse_decision(tmp_path):
    from autoclip.director import create_plan
    lines, plan = fixture_plan()
    plan.clips = []
    validate_plan(plan, lines, 40, 0, 45)
    with pytest.raises(ValueError, match="需要 2"):
        validate_plan(plan, lines, 40, 2, 45)
    class Reviewer:
        calls = 0
        def json(self, system, prompt):
            self.calls += 1
            return plan.model_dump()
    # Feedback indexed against old fixed-count scripts cannot target a new slate.
    (tmp_path / "editor_notes.json").write_text(json.dumps({"clips": [{"index": 99}]}))
    reviewer = Reviewer()
    cfg = Settings(clips=0, vision=False, target_seconds=45)
    for _ in range(2):
        actual = create_plan(Path("source.mp4"), tmp_path, lines, 40, [], cfg, reviewer, [])
        assert actual.clips == []
    assert reviewer.calls == 1


def test_rejects_out_of_bounds_reversed_and_fabricated_evidence():
    lines, plan = fixture_plan()
    validate_plan(plan,lines,40,1,45)
    plan.clips[0].beats[2].start = 5
    plan.clips[0].beats[3].end = 41
    plan.clips[0].beats[0].evidence_ids = [999]
    with pytest.raises(ValueError) as err:
        validate_plan(plan,lines,40,1,45)
    assert "不存在" in str(err.value)
    assert "递增" in str(err.value)
    assert "结尾" in str(err.value)


def test_rejects_non_overlapping_evidence_and_duplicate_clips():
    lines, plan = fixture_plan()
    plan.clips[0].beats[0].evidence_ids = [3]
    plan.clips.append(plan.clips[0].model_copy(deep=True))
    with pytest.raises(ValueError) as err:
        validate_plan(plan, lines, 40, 2, 45)
    assert "证据" in str(err.value)
    assert "重复" in str(err.value)


def test_rejects_low_confidence_as_only_evidence():
    lines, plan = fixture_plan()
    lines[0].confidence = .02
    with pytest.raises(ValueError, match="低置信度"):
        validate_plan(plan, lines, 40, 1, 45)


def test_narration_can_explain_immediately_preceding_original():
    lines, plan = fixture_plan()
    plan.clips[0].beats[2].evidence_ids = [1]
    validate_plan(plan, lines, 40, 1, 45)
    lines.append(Line(id=4,start=1,end=3,text="从未出现在已剪镜头的内容"))
    plan.clips[0].beats[2].evidence_ids = [4]
    with pytest.raises(ValueError, match="证据"):
        validate_plan(plan, lines, 40, 1, 45)


def test_compiler_extends_subsecond_narration_without_overlapping_next_shot():
    lines, plan = fixture_plan()
    raw=plan.model_dump()
    raw["clips"][0]["beats"][2]["end"] = 20.12
    compiled=compile_plan(raw,lines)
    assert compiled.clips[0].beats[2].end-compiled.clips[0].beats[2].start >= 5
    assert compiled.clips[0].beats[2].end <= compiled.clips[0].beats[3].start


def test_long_original_is_split_without_losing_dialogue():
    lines, plan=fixture_plan()
    lines.extend(Line(id=i,start=i*10,end=(i+1)*10,text="连续原声") for i in range(4,8))
    raw=plan.model_dump()
    raw["clips"][0]["beats"][-1].update(start=30,end=75,evidence_ids=[3,4,5,6,7])
    result=compile_plan(raw,lines)
    a,b=result.clips[0].beats[-2:]
    assert a.mode == b.mode == "original"
    assert a.end == b.start
    assert a.start <= 30 and b.end >= 75
    assert a.end-a.start <= 40 and b.end-b.start <=40


def test_scene_selection_compiles_to_short_evidence_centered_shot():
    lines, plan = fixture_plan()
    raw = plan.model_dump()
    raw["clips"][0]["beats"][0].update(start=0,end=50,evidence_ids=[0])
    compiled = compile_plan(raw, lines)
    first = compiled.clips[0].beats[0]
    assert first.end-first.start <= 22
    assert first.start < lines[0].end and first.end > lines[0].start
    original = compiled.clips[0].beats[1]
    assert original.start <= lines[1].start and original.end >= lines[1].end


def test_sidecar_crlf_multiline_and_bad_blocks(tmp_path):
    path = tmp_path / "a.srt"
    path.write_bytes(b'\xef\xbb\xbf1\r\n00:00:01,250 --> 00:00:03,500\r\n<b>Hello</b>\r\nworld\r\n\r\nbad\r\n')
    lines = load_srt(path)
    assert len(lines) == 1
    assert lines[0].text == "Hello world"
    assert lines[0].start == 1.25


def test_no_automatic_cross_provider_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_USE_DASHSCOPE_ALIAS", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "wrong-provider")
    with pytest.raises(ValueError):
        Settings().api_key()
    monkeypatch.setenv("DEEPSEEK_USE_DASHSCOPE_ALIAS", "true")
    assert Settings().api_key() == "wrong-provider"


def test_llm_cache_reuses_response_and_hides_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    llm = LLM(Settings(), tmp_path)
    llm.client.close()
    requests = []
    def response(request):
        requests.append(request)
        return httpx.Response(200,json={"choices":[{"message":{"content":'{"ok":true}'},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2}})
    llm.client = httpx.Client(transport=httpx.MockTransport(response))
    assert llm.json("system", "prompt") == {"ok":True}
    assert llm.json("system", "prompt") == {"ok":True}
    assert len(requests) == 1
    assert "test-key" not in next((tmp_path / "llm").glob("*.json")).read_text()
    assert llm.usage()["prompt_tokens"] == 5


def test_model_override_has_separate_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    llm=LLM(Settings(),tmp_path)
    llm.client.close()
    seen=[]
    def response(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(200,json={"choices":[{"message":{"content":'{"ok":true}'},"finish_reason":"stop"}]})
    llm.client=httpx.Client(transport=httpx.MockTransport(response))
    llm.json("system","prompt")
    llm.json("system","prompt",model="deepseek-v4-pro")
    llm.json("system","prompt",model="deepseek-v4-pro")
    assert seen == ["deepseek-flash","deepseek-v4-pro"]


def test_source_change_invalidates_cache(tmp_path):
    p = tmp_path / "input.mp4"
    p.write_bytes(b"aaaa")
    a = source_identity(p)
    p.write_bytes(b"bbbb")
    assert source_identity(p) != a


def test_export_archives_renamed_deliverable_but_keeps_user_files(tmp_path):
    from autoclip.deliver import export_finished
    from autoclip.util import write_json, read_json
    episode = tmp_path / "01-example"
    episode.mkdir()
    source = episode / "video.mp4"
    source.write_bytes(b"export fixture")
    write_json(episode / "status.json", {"stage": "已完成"})
    clip = {"title": "旧标题", "video": str(source), "duration": 60, "angle": "观点", "timeline": []}
    write_json(episode / "results.json", [clip])
    target = export_finished(tmp_path)
    old = read_json(target / "清单.json")[0]["file"]
    user_file = target / "用户保存的视频.mp4"
    user_file.write_bytes(b"user content")
    clip["title"] = "新标题"
    write_json(episode / "results.json", [clip])
    export_finished(tmp_path)
    assert not (target / old).exists()
    assert len(list((target / "历史版本").glob(f"*/{old}"))) == 1
    assert user_file.read_bytes() == b"user content"
    assert (target / read_json(target / "清单.json")[0]["file"]).read_bytes() == source.read_bytes()
    # An automatic pass with no selected stories must not leave old exports
    # looking like results from the current pass.
    write_json(episode / "results.json", [])
    export_finished(tmp_path)
    assert read_json(target / "清单.json") == []
    assert list(target.glob("*.mp4")) == [user_file]


def test_subtitle_control_text_is_escaped():
    assert "{" not in ass_escape(r"{\pos(0,0)}测试")
    assert "\\" not in ass_escape(r"{\pos(0,0)}测试")


def test_real_render_has_audio_video_and_valid_timeline(tmp_path, monkeypatch):
    import autoclip.render as renderer
    source, voice = tmp_path / "source.mp4", tmp_path / "voice.mp3"
    run(["ffmpeg","-v","error","-y","-f","lavfi","-i","color=c=0x304060:s=320x180:r=25",
         "-f","lavfi","-i","sine=frequency=220:sample_rate=48000","-t","40","-c:v","libx264","-preset","ultrafast","-c:a","aac",source])
    run(["ffmpeg","-v","error","-y","-f","lavfi","-i","sine=frequency=440:sample_rate=48000","-t","9.5",voice])
    monkeypatch.setattr(renderer,"synthesize",lambda text,root,cfg: voice)
    lines, plan = fixture_plan()
    cfg = Settings(target_seconds=45, output=tmp_path)
    result = render_clip(source, tmp_path, plan.clips[0], 0, cfg, None, lines)
    assert 39 < duration(Path(result["video"])) < 41
    assert result["width"] == 720 and result["height"] == 1280
    assert result["qa"]["full_decode"] == "passed"
    assert all(a["output_end"] == b["output_start"] for a,b in zip(result["timeline"],result["timeline"][1:]))
    from PIL import Image, ImageDraw
    import autoclip.branding as branding
    logo = tmp_path / "logo.png"
    art = Image.new("RGBA", (200, 70))
    ImageDraw.Draw(art).rectangle((10, 10, 190, 60), fill=(240, 210, 100, 255))
    art.save(logo)
    brand = branding.BrandSettings(watermark=logo)
    finished = branding.package_clip(source, tmp_path, result, 0, "测试作品", brand)
    assert Path(finished["video"]) != Path(result["video"])
    assert Image.open(finished["cover"]).size == (1080, 1920)
    assert any(b.start <= finished["cover_details"]["source_time"] <= b.end for b in plan.clips[0].beats)
    def audio_hash(video):
        return run(["ffmpeg", "-v", "error", "-i", video, "-map", "0:a:0", "-c", "copy", "-f", "hash", "-"])
    assert audio_hash(finished["video"]) == audio_hash(result["video"])
    # Rebranding a branded manifest must reuse the clean base and cached package.
    monkeypatch.setattr(branding, "run", lambda *a, **kw: pytest.fail("重跑不应重新合成"))
    assert branding.package_clip(source, tmp_path, finished, 0, "测试作品", brand) == finished
