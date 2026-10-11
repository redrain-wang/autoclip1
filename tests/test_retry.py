import pytest

from autoclip.models import Line
from autoclip.retry import retry_selection
from autoclip.util import read_json, write_json


def setup(tmp_path):
    old = tmp_path/"old"
    identity = {"path": "test.mp4", "size": 100}
    stories = [{"title": title, "angle": "观点", "start": i*200, "end": i*200+150,
                "evidence_ids": [i*2, i*2+1], "reason": "完整故事"}
               for i, title in enumerate(["已完成故事", "失败故事甲", "失败故事乙"])]
    lines = [Line(id=i, start=(i//2)*200+10+(i%2)*100, end=(i//2)*200+15+(i%2)*100, text="证据") for i in range(6)]
    write_json(old/"source.json", identity)
    write_json(old/"selection.json", {"episode_summary": "摘要", "stories": stories})
    write_json(old/"rejected.json", [{"title": s["title"], "error": "实测时长不足"} for s in stories[1:]])
    return old, identity, lines


def test_replay_only_failed_candidates_and_preserve_previous_batch(tmp_path):
    old, identity, lines = setup(tmp_path)
    before = {p.name: p.read_bytes() for p in old.iterdir()}
    result = retry_selection(old, tmp_path/"new", identity, lines, 600, [], 3)
    assert [s.title for s in result.stories] == ["失败故事甲", "失败故事乙"]
    assert {p.name: p.read_bytes() for p in old.iterdir()} == before
    assert list((tmp_path/"new/selections").glob("retry-*.json"))


def test_replay_rechecks_current_history_and_missing_evidence(tmp_path):
    old, identity, lines = setup(tmp_path)
    history = [{"timeline": [{"start": 200, "end": 350}]}]
    result = retry_selection(old, tmp_path/"new", identity, lines[:-1], 600, history, 3)
    assert result.stories == []
    skipped = read_json(tmp_path/"new/retry-selection.json")["skipped"]
    assert len(skipped) == 2
    assert "历史成片" in skipped[0]["reason"]
    assert "证据" in skipped[1]["reason"]


def test_replay_rejects_same_output_and_other_source(tmp_path):
    old, identity, lines = setup(tmp_path)
    with pytest.raises(ValueError, match="新批次"):
        retry_selection(old, old, identity, lines, 600, [], 3)
    with pytest.raises(ValueError, match="素材"):
        retry_selection(old, tmp_path/"new", {"path": "other.mp4"}, lines, 600, [], 3)


def test_replay_does_not_silently_drop_unmatched_titles(tmp_path):
    old, identity, lines = setup(tmp_path)
    write_json(old/"rejected.json", [{"title": "较早版本的故事", "error": "实测时长不足"}])
    assert retry_selection(old, tmp_path/"new", identity, lines, 600, [], 3).stories == []
    assert read_json(tmp_path/"new/retry-selection.json")["skipped"][0]["title"] == "较早版本的故事"
