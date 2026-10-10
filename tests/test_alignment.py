from autoclip.alignment import align_rows
from autoclip.models import Line


def sample(i, start, text, visible, end=None):
    line = Line(id=i, start=start, end=end or start+1, text=text)
    return line, {"line": line.model_dump(), "readings": [{"readable": True, "text": visible}]*2}


def test_split_asr_maps_to_one_caption_without_guessing_word_times():
    a, ra = sample(1, 10, "在下", "在下王小明")
    b, rb = sample(2, 11.5, "王小名", "在下王小明")
    result = align_rows([ra, rb], [a, b])
    assert len(result) == 1
    assert result[0]["line_ids"] == [1, 2]
    assert result[0]["relation"] == "many_to_one"
    assert (result[0]["start"], result[0]["end"]) == (10, 12.5)
    assert result[0]["visible_text"] == "在下王小明"


def test_gap_and_missing_neighbor_evidence_prevent_grouping():
    a, ra = sample(1, 10, "在下", "在下王小明")
    b, rb = sample(2, 20, "王小名", "在下王小明")
    assert all(r["relation"] != "many_to_one" for r in align_rows([ra, rb], [a, b]))
    b = b.model_copy(update={"start": 11.5, "end": 12.5})
    assert all(r["relation"] != "many_to_one" for r in align_rows([ra], [a, b]))


def test_shared_caption_joins_short_name_with_spelling_errors():
    a, ra = sample(1, 10, "在下", "在下邬思道")
    b, rb = sample(2, 11.5, "吴司道", "在下邬思道")
    ra["readings"].insert(0, {"readable": False, "text": ""})
    result = align_rows([ra, rb], [a, b])
    assert len(result) == 1 and result[0]["line_ids"] == [1, 2]
    assert result[0]["relation"] == "many_to_one"


def test_caption_change_inside_asr_is_held_without_invented_alignment():
    a, row = sample(1, 10, "大人请坐在下王小明", "大人请坐")
    row["readings"][1] = {"readable": True, "text": "在下王小明"}
    assert align_rows([row], [a])[0]["status"] == "unresolved"


def test_two_captions_need_two_observations_each_and_keep_uncertain_transition():
    a, row = sample(1, 10, "大人请坐在下王小名", "大人请坐", end=14)
    row["frames"] = [{"time": at} for at in [10.5, 11.2, 12.8, 13.5]]
    row["readings"] = [{"readable": True, "text": text} for text in
                       ["大人请坐", "大人请坐", "在下王小明", "在下王小明"]]
    result = align_rows([row], [a])[0]
    assert result["relation"] == "one_to_many"
    assert result["transition_window"] == [11.2, 12.8]
    assert (result["start"], result["end"]) == (10, 14)
    row["readings"][1] = {"readable": False, "text": ""}
    assert align_rows([row], [a])[0]["status"] == "unresolved"


def test_repeated_caption_does_not_merge_complete_utterances():
    a, ra = sample(1, 10, "大人请坐", "大人请坐")
    b, rb = sample(2, 11.2, "大人请坐", "大人请坐")
    result = align_rows([ra, rb], [a, b])
    assert len(result) == 2
    assert all(r["relation"] == "one_to_one" for r in result)


def test_unreadable_and_multi_line_readings():
    a, ra = sample(1, 10, "今日天气很好我们出门", "今日天气很好\n我们出门")
    assert align_rows([ra], [a])[0]["status"] == "confirmed"
    ra["readings"] = [{"readable": False, "text": ""}]*2
    assert align_rows([ra], [a])[0]["status"] == "unresolved"


def test_ambiguous_matching_groups_are_not_selected():
    # Either adjacent pair could explain the visible subtitle equally well.
    samples = [sample(i, 10+i, "甲乙", "甲乙甲乙") for i in range(3)]
    result = align_rows([r for _, r in samples], [s for s, _ in samples])
    assert all(r["status"] == "unresolved" for r in result)
