import pytest

from autoclip.budget import story_budget, uncovered_intervals
from autoclip.config import Settings
from autoclip.models import Line


def test_gaps_union_clips_overlaps_and_out_of_bounds_without_negative_time():
    assert uncovered_intervals((10, 50), [{"start": 0, "end": 12}, {"start": 11, "end": 20},
                                          {"start": 30, "end": 35}, {"start": 34, "end": 80}]) == [(20, 30)]


def test_budget_quantifies_deficit_and_only_lists_complete_unused_utterances():
    cfg = Settings(target_seconds=190, min_seconds=150, max_seconds=240)
    beats = [{"mode": "narration", "start": 10, "end": 14}, {"mode": "original", "start": 14, "end": 30},
             {"mode": "narration", "start": 40, "end": 50}]
    lines = [Line(id=1, start=29, end=33, text="横跨已使用与空余区间"),
             Line(id=2, start=33, end=36, text="完整未用对白"),
             Line(id=3, start=36, end=38, text="不可靠", confidence=.2)]
    report = {"clip": {"beats": beats}, "measured_total_seconds": 144.9,
              "beats": [{"mode": "narration", "beat": 0, "text": "十个字的测试解说词啊。",
                         "voice_seconds": 2, "output_seconds": 2.35},
                        {"mode": "narration", "beat": 2, "text": "结尾口播", "voice_seconds": 18, "output_seconds": 18.35}]}
    before = repr(report)
    budget = story_budget(lines, (10, 55), cfg, report)
    assert budget["missing_to_minimum_seconds"] == 5.1
    assert budget["unused_source_seconds"] == 15
    assert [s["id"] for g in budget["unused_intervals"] for s in g["complete_lines"]] == [2]
    assert budget["speech_rate"]["basis"] == "same_script_measured_tts"
    assert repr(report) == before


def test_no_extra_footage_past_story_end_or_through_neighbor_original():
    cfg = Settings(target_seconds=190)
    beats = [{"mode": "narration", "start": 0, "end": 4}, {"mode": "original", "start": 4, "end": 20},
             {"mode": "narration", "start": 20, "end": 24}]
    report = {"clip": {"beats": beats}, "beats": [
        {"mode": "narration", "beat": i, "text": "需要时间的口播", "voice_seconds": 10, "output_seconds": 10.35}
        for i in (0, 2)]}
    budget = story_budget([], (0, 24), cfg, report)
    assert all(r["must_rewrite_or_reallocate_neighbors"] for r in budget["narration_slots"])
    assert [r["free_window"] for r in budget["narration_slots"]] == [[0, 4], [20, 24]]


def test_initial_estimate_is_labelled_and_ceiling_is_not_a_quality_pass():
    cfg = Settings(target_seconds=190, min_seconds=150, max_seconds=240)
    budget = story_budget([], (100, 220), cfg)
    assert budget["absolute_output_ceiling_seconds"] == pytest.approx(168)
    assert budget["speech_rate"]["basis"] == "initial_estimate"
    assert "passed" not in budget


@pytest.mark.parametrize("beat", [{"mode": "original"}, {"mode": "narration", "start": "10", "end": 20},
                                  {"mode": "original", "start": 10, "end": float("nan")}])
def test_malformed_model_timeline_does_not_break_repair_diagnostics(beat):
    budget = story_budget([], (0, 150), Settings(target_seconds=190), {"clip": {"beats": [beat]}})
    assert "timeline_budget_unavailable" in budget
    assert "unused_intervals" not in budget
