import pytest

from autoclip.branding import cover_times


def timeline():
    return [{'mode':'original','start':10,'end':26},
            {'mode':'narration','start':30,'end':50},
            {'mode':'original','start':70,'end':90}]


def test_reviewed_cover_uses_retained_original_and_ordinary_choice_is_unchanged():
    assert cover_times(timeline(),82)==[82]
    assert cover_times(timeline())==[18,80,18]


def test_title_evidence_prefers_its_scene_and_never_selects_narration_or_unused_footage():
    shots = timeline()
    shots[0]['evidence_ids'] = [1]
    shots[1]['evidence_ids'] = [2]
    shots[2]['evidence_ids'] = [3]
    assert cover_times(shots,title_evidence_ids=[3]) == [80]
    assert cover_times(shots,title_evidence_ids=[2,99]) == cover_times(shots)
    assert cover_times(shots,18,title_evidence_ids=[3]) == [18]


@pytest.mark.parametrize('stamp',[40,26,90,999,True,float('nan'),float('inf'),'82'])
def test_cover_cannot_use_unselected_footage_or_invalid_time(stamp):
    with pytest.raises(ValueError,match='实际保留'):
        cover_times(timeline(),stamp)
