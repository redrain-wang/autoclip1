import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from autoclip.branding import BrandSettings
from autoclip.config import Settings
from autoclip.director import compile_plan
from autoclip.models import Line
from autoclip.production import completed_stories, content_rejection, load_history, seed_cache, select_stories
from autoclip.render import subtitle_display
from autoclip.util import digest, write_json


def test_completed_story_survives_changed_selection_and_interrupted_index(tmp_path):
    story={"title":"已完成故事","start":100,"end":300}
    folder=tmp_path/"stories"/digest(story)/"published"/"clip"
    folder.mkdir(parents=True)
    clip={"title":"成片标题","qa":{"branded_full_decode":"passed"}}
    for field in ("video","cover","poster"):
        path=folder/field
        path.write_bytes(b"completed")
        clip[field]=str(path)
    write_json(folder/"manifest.json",clip)
    write_json(tmp_path/"selections"/"old.json",{"stories":[story]})
    write_json(tmp_path/"selection.json",{"stories":[]})
    write_json(tmp_path/"results.json",[])
    recovered=completed_stories(tmp_path)
    assert len(recovered)==1 and recovered[0]["story"]==story
    write_json(tmp_path/"completed.json",recovered)
    write_json(tmp_path/"results.json",recovered)
    assert completed_stories(tmp_path)==recovered
    write_json(tmp_path/"quality-holds.json",{clip["video"]:{"reason":"事实待核验"}})
    assert completed_stories(tmp_path)==[]
    (tmp_path/"quality-holds.json").unlink()
    Path(clip["video"]).unlink()
    assert completed_stories(tmp_path)==[]


def test_visible_caption_omits_terminal_punctuation_but_keeps_numbers_and_quotes():
    assert subtitle_display("一千两，") == "一千两"
    assert subtitle_display("他问：‘谁来还？’") == "他问：‘谁来还’"
    assert subtitle_display("2.5两，共1,200两。") == "2.5两，共1,200两"
    assert subtitle_display("没有标点") == "没有标点"


def test_rotation_is_stable_per_clip():
    brand=BrandSettings(watermark=Path("a.png"),watermarks=[Path("a.png"),Path("b.png")])
    assert [brand.for_clip(i).watermark.name for i in range(4)] == ["a.png","b.png","a.png","b.png"]
    assert brand.watermarks == [Path("a.png"),Path("b.png")]


def test_history_reads_other_batches_and_never_seeds_editor_overrides(tmp_path):
    old=tmp_path/"old"; active=tmp_path/"active"; other=tmp_path/"other"/"episode"
    identity={"path":"test-video","size":100}
    for root,title in ((old,"old"),(active,"active"),(other,"other")):
        write_json(root/"source.json",identity)
        write_json(root/"results.json",[{"title":title}])
    write_json(old/"asr"/"signature"/"transcript.json",[])
    write_json(old/"editor_notes.json",{"private":"manual edit"})
    before=(old/"asr"/"signature"/"transcript.json").read_bytes()
    prior,clips=load_history(tmp_path,identity,active)
    assert {c["title"] for c in clips} == {"old","other"}
    seed_cache(prior,active)
    assert not (active/"editor_notes.json").exists()
    (active/"asr"/"signature"/"transcript.json").write_text("changed")
    assert (old/"asr"/"signature"/"transcript.json").read_bytes()==before


def test_bad_candidate_does_not_discard_a_valid_story(tmp_path):
    class FakeLLM:
        def json(self,*args):
            return {"episode_summary":"摘要","stories":[
                {"title":"过短故事","angle":"短","start":0,"end":30,"evidence_ids":[0,1],"reason":"短"},
                {"title":"完整故事","angle":"完整","start":100,"end":300,"evidence_ids":[2,3],"reason":"完整"}]}
    lines=[Line(id=i,start=i*50,end=i*50+5,text="证据") for i in range(4)]
    result=select_stories(tmp_path,lines,[],400,[],Settings(target_seconds=190,min_seconds=150,max_seconds=240),FakeLLM(),3)
    assert [s.title for s in result.stories]==["完整故事"]
    assert list(tmp_path.glob("selection-rejected-*.json"))


def test_invalid_duration_configuration_fails_before_paid_work():
    with pytest.raises(ValueError):
        Settings(target_seconds=190,min_seconds=200)
    with pytest.raises(ValueError):
        Settings(target_seconds=190,max_seconds=100)


def test_service_failures_cannot_be_hidden_as_editorial_rejections():
    assert content_rejection(ValueError("剪辑计划未通过时间线和证据校验"))
    assert not content_rejection(ValueError("模型接口 HTTP 401"))
    assert not content_rejection(RuntimeError("配音服务失败"))
    assert not content_rejection(RuntimeError("ffmpeg failed: disk full"))


def test_silent_outro_is_not_truncated_to_last_subtitle():
    lines=[Line(id=i,start=i*10,end=i*10+9,text="对白") for i in range(4)]
    raw={"episode_summary":"摘要","genre":"历史","style":"分析","clips":[{
        "title":"保留结尾","angle":"观点","hook":"钩子","beats":[
            {"mode":"narration","start":0,"end":9,"text":"交代人物处境和矛盾。","evidence_ids":[0],"reason":"依据"},
            {"mode":"original","start":10,"end":19,"text":"","evidence_ids":[1],"reason":"原声"},
            {"mode":"original","start":20,"end":29,"text":"","evidence_ids":[2],"reason":"原声"},
            {"mode":"narration","start":40,"end":45,"text":"这是原声结束后的总结，可以使用仍然存在的无对白反应画面来呈现完整观点。","evidence_ids":[3],"reason":"总结"}]}]}
    clip=compile_plan(raw,lines).clips[0]
    assert clip.beats[-1].end==45
    assert clip.beats[-1].end>clip.beats[-1].start


def test_installed_cli_runs_outside_repo_without_codex_environment(tmp_path):
    environment={k:v for k,v in os.environ.items() if not k.startswith("CODEX")}
    result=subprocess.run([sys.executable,"-m","autoclip","produce","--help"],
                          cwd=tmp_path,env=environment,text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    assert "max-new" in result.stdout
