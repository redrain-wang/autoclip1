"""Readable subtitles must not turn long narration into flashing single letters."""
import json

import pytest

from autoclip.render import subtitle_chunks, subtitle_display, voice_captions


def test_long_phrases_preserve_text_without_single_character_tails():
    for length in range(15, 100):
        text = '这' * length + '。'
        chunks = subtitle_chunks(text)
        assert ''.join(chunks) == text
        assert all(2 <= len(subtitle_display(chunk)) <= 14 for chunk in chunks)
        assert all(len(chunk) <= 14 for chunk in chunks)


def test_short_enumeration_stays_readable_and_sentence_pauses_survive():
    text = '好。他把这个美字拆成八、大、王，接着往下说。'
    chunks = subtitle_chunks(text)
    assert ''.join(chunks) == text
    assert chunks[0] == '好。'
    assert any('八、大、王' in chunk for chunk in chunks)
    assert all(len(chunk) <= 14 for chunk in chunks)
    assert subtitle_chunks('八爷要拿人，十爷为何护着道士？', 12) == ['八爷要拿人，', '十爷为何护着道士？']


@pytest.mark.parametrize('service_boundaries', [False, True])
def test_balanced_captions_keep_audio_timing_and_spoken_text(tmp_path, service_boundaries):
    voice = tmp_path / 'voice.mp3'
    text = '这' * 15 + '，八、大、王。'
    start, seconds = (1., 8.) if service_boundaries else (0., 9.)
    if service_boundaries:
        voice.with_suffix('.boundaries.jsonl').write_text(json.dumps({
            'text': text, 'offset': start * 1e7, 'duration': seconds * 1e7}))
    rows, _ = voice_captions(text, voice, 9.)
    assert ''.join(row[2] for row in rows) == text
    assert rows[0][0] == pytest.approx(start)
    assert rows[-1][1] == pytest.approx(9.)
    assert all(stop > begin for begin, stop, _ in rows)
    assert all(len(subtitle_display(row[2])) > 1 for row in rows)
    assert all(a[1] == pytest.approx(b[0]) for a, b in zip(rows, rows[1:]))
