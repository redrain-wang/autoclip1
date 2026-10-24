from pathlib import Path

import pytest

from autoclip.config import Settings
from autoclip.models import Line
from autoclip.research import ground_window, prepare_research, profile, research_context


def fixture_data():
    lines = [Line(id=4, start=10, end=13, text="儿臣举荐四阿哥"),
             Line(id=5, start=14, end=18, text="太子，朕想问问你的意思")]
    raw = {"events": [{"event": "一位皇子举荐四阿哥，随后皇帝询问太子。",
                       "participants": ["身份待核的皇子", "太子"], "evidence_ids": [5, 4],
                       "identity_basis": "儿臣不是唯一身份", "identity_status": "verified",
                       "interpretation": "可能在征求其他人的意见", "alternatives": [],
                       "uncertainties": ["前一发言者未仅凭字幕定名"]}], "omitted_reason": ""}
    return lines, raw


def test_named_background_does_not_conflate_princes_or_leak_to_other_shows():
    p = profile("雍正王朝")
    aliases = {alias: c["name"] for c in p["characters"] for alias in c["aliases"]}
    assert aliases["太子"] == "胤礽" and aliases["八爷"] == "胤禩"
    assert "儿臣" not in aliases and "吴先生" not in aliases
    assert profile("其他电视剧")["characters"] == []


def test_notes_keep_raw_evidence_and_cannot_self_certify_identity():
    lines, raw = fixture_data()
    event = ground_window(raw, lines)[0]
    assert event["identity_status"] == "unverified"
    assert event["status"] == "model_notes"
    assert (event["start"], event["end"]) == (10, 18)
    assert event["evidence"] == [s.model_dump() for s in lines]


def test_unknown_evidence_is_rejected_instead_of_silently_discarded():
    lines, raw = fixture_data()
    raw["events"][0]["evidence_ids"].append(99)
    with pytest.raises(ValueError, match=r"不存在的字幕\[99\].*可用编号为\[4, 5\]"):
        ground_window(raw, lines)
    with pytest.raises(ValueError, match="说明原因"):
        ground_window({"events": [], "omitted_reason": ""}, lines)


def test_cache_tracks_source_transcript_and_profile_and_filters_story_context(tmp_path, monkeypatch):
    import autoclip.research as research
    video = tmp_path / "source.mp4"
    video.write_bytes(b"first-source")
    lines, raw = fixture_data()

    class FakeLLM:
        calls = 0

        def json(self, system, prompt):
            self.calls += 1
            return raw

    llm = FakeLLM()
    cfg = Settings(series="雍正王朝")
    root = tmp_path / "notes"
    first = prepare_research(video, root, lines, 20, cfg, llm)
    assert prepare_research(video, root, lines, 20, cfg, llm) == first
    assert llm.calls == 1
    video.write_bytes(b"different-source")
    second = prepare_research(video, root, lines, 20, cfg, llm)
    assert second["signature"] != first["signature"] and llm.calls == 2
    lines[0].text = "儿臣愿举荐四阿哥"
    third = prepare_research(video, root, lines, 20, cfg, llm)
    assert third["signature"] != second["signature"] and llm.calls == 3
    monkeypatch.setattr(research, "profile", lambda series: {"series": series, "rules": ["修订人物背景"]})
    fourth = prepare_research(video, root, lines, 20, cfg, llm)
    assert fourth["signature"] != third["signature"] and llm.calls == 4
    assert research_context(fourth, 19, 20)["events"] == []
    assert len(research_context(fourth, 9, 20)["events"]) == 1
    assert "未通过人物画面核对" in (root / "剧情资料.md").read_text()


def test_production_research_and_yunyang_are_enabled():
    cfg = Settings.load(Path(__file__).parents[1] / "configs.production.toml")
    assert cfg.research_before_produce and cfg.voice == "zh-CN-YunyangNeural"


def test_concurrent_json_writers_never_share_a_temporary_file(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from autoclip.util import read_json, write_json
    path = tmp_path / "batch.json"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: write_json(path, {"worker": i, "payload": "字" * 5000}), range(32)))
    data = read_json(path)
    assert data["worker"] in range(32) and data["payload"] == "字" * 5000
    assert not list(tmp_path.glob("*.tmp"))


def test_research_cache_seed_requires_matching_source(tmp_path):
    from autoclip.research import seed_research
    from autoclip.util import write_json
    history = tmp_path / "history"
    identity = {"sample_sha256": "source-a"}
    for name, source in [("same", identity), ("other", {"sample_sha256": "source-b"})]:
        write_json(history / name / "source.json", source)
        write_json(history / name / "research" / f"{name}.json", {"notes": name})
    root = tmp_path / "new"
    seed_research(history, identity, root)
    assert (root / "research" / "same.json").exists()
    assert not (root / "research" / "other.json").exists()


@pytest.mark.parametrize('repair_succeeds',[True,False])
def test_oversized_event_research_repairs_within_budget_without_dropping_evidence(tmp_path,repair_succeeds):
    import copy
    from autoclip.util import read_json
    video=tmp_path/'source.mp4';video.write_bytes(b'source')
    lines=[Line(id=i,start=i*2,end=i*2+1,text='本区间实际对白') for i in range(30)]
    _,valid=fixture_data()
    invalid=copy.deepcopy(valid);invalid['events'][0]['evidence_ids']=list(range(26))
    class Model:
        calls=0
        cache={}
        def json(self,system,prompt):
            if prompt in self.cache:return self.cache[prompt]
            self.calls+=1
            if self.calls>1:
                assert '1–24条' in prompt and '上次未通过的数据' in prompt
            self.cache[prompt]=valid if repair_succeeds and self.calls==3 else invalid
            return self.cache[prompt]
    model=Model();root=tmp_path/'research-run';cfg=Settings(repair_attempts=2)
    if repair_succeeds:
        result=prepare_research(video,root,lines,60,cfg,model)
        assert result['windows'][0]['events'][0]['evidence_ids']==[5,4]
        assert prepare_research(video,root,lines,60,cfg,model)==result
    else:
        with pytest.raises(ValueError,match='at most 24'):prepare_research(video,root,lines,60,cfg,model)
        assert not (root/'research.json').exists()
    assert model.calls==3
    rejected=list((root/'research-attempts').rglob('*.json'))
    assert len(rejected)==(2 if repair_succeeds else 3)
    assert all(len(read_json(p)['response']['events'][0]['evidence_ids'])==26 for p in rejected)


@pytest.mark.parametrize("verdict", [{"passed": True, "issues": ["主语归属错误"]}, {"passed": True}])
def test_contradictory_or_incomplete_review_cannot_pass(tmp_path, verdict):
    from autoclip.repair import review_voice_changes, RepairNeeded
    from autoclip.director import create_plan
    from autoclip.models import Beat, Clip, Plan
    lines = [Line(id=i, start=i*10, end=i*10+9.8, text="场景对白") for i in range(4)]
    clip = Clip(title="错误归属待核", angle="处境分析", hook="谁来处理", beats=[
        Beat(mode="narration" if i%2 == 0 else "original", start=i*10, end=i*10+10,
             text="处理这件事需要承担责任，在场的人会如何选择，仍要听清他们的回应。" if i%2 == 0 else "",
             evidence_ids=[i], reason="当前场景") for i in range(4)])

    class FakeLLM:
        calls = 0

        def json(self, *args):
            self.calls += 1
            return verdict

    cfg = Settings(series="雍正王朝", clips=1, target_seconds=45, min_seconds=30, max_seconds=60, vision=False)
    with pytest.raises(RepairNeeded):
        review_voice_changes(clip, [], tmp_path, cfg, FakeLLM(), lines, 0)

    class FakeWriter:
        calls = 0

        def json(self, *args):
            self.calls += 1
            if self.calls == 1:
                return Plan(episode_summary="", genre="历史", style="", clips=[clip]).model_dump()
            return verdict

    with pytest.raises(ValueError, match="语义审片失败"):
        create_plan(tmp_path / "video.mp4", tmp_path, lines, 41, [], cfg, FakeWriter(), [])
