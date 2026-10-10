import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoclip.config import Settings
from autoclip.evidence import assess, inspect_subtitles


def readings(a, b=None):
    return [{"readable": True, "text": t} for t in (a, b if b is not None else a)]


def test_agreement_proposes_but_does_not_apply():
    result = assess("王小名", readings("王小明"))
    assert result["status"] == "proposed_correction"
    assert result["proposed_text"] == "王小明"


def test_punctuation_does_not_create_correction():
    assert assess("大人，请坐。", readings("大人 请坐", "大人，请坐！"))["status"] == "confirmed"


@pytest.mark.parametrize("values", [readings("王小明", "王小民"), readings("人"),
    [{"readable": False, "text": "王小明"}]*2,
    [{"readable": "true", "text": "王小明"}]*2, [None, None], readings("完全无关的下一句对白")])
def test_unresolved_evidence_is_not_a_correction(values):
    result = assess("王小名", values)
    assert result["status"] == "unresolved"
    assert result["proposed_text"] is None


def test_report_preserves_input_and_does_not_prime_ocr(tmp_path, monkeypatch):
    import autoclip.evidence as module
    source, transcript = tmp_path/"source.mp4", tmp_path/"transcript.json"
    source.write_bytes(b"fake-video")
    transcript.write_text(json.dumps([dict(id=3, start=10, end=12, text="王小名")]))
    before = transcript.read_bytes()
    calls = []

    class FakeLLM:
        def __init__(self, *args):
            self.client = SimpleNamespace(close=lambda: None)
        def json(self, system, prompt, images):
            calls.append((system, prompt, images))
            return {"readable": True, "text": "王小明"}
        def usage(self):
            return {"calls_this_run": len(calls)}

    monkeypatch.setattr(module, "LLM", FakeLLM)
    monkeypatch.setattr(module, "run", lambda args: Path(args[-1]).write_bytes(b"jpg"))
    report = inspect_subtitles(source, transcript, [3], tmp_path/"report", Settings())
    data = json.loads(report.read_text())
    assert transcript.read_bytes() == before
    assert data["automatic_application"] is False
    assert data["status"] == "completed"
    assert data["rows"][0]["status"] == "proposed_correction"
    assert [r["time"] for r in data["rows"][0]["frames"]] == [10.7, 11.3]
    assert len(calls) == 2
    assert all("王小名" not in system+prompt for system, prompt, _ in calls)


def test_unknown_id_fails_before_paid_call(tmp_path, monkeypatch):
    import autoclip.evidence as module
    transcript = tmp_path/"transcript.json"
    transcript.write_text("[]")
    monkeypatch.setattr(module, "LLM", lambda *a: pytest.fail("must not call model"))
    with pytest.raises(ValueError, match="ID"):
        inspect_subtitles(tmp_path/"missing.mp4", transcript, [3], tmp_path/"report", Settings())


def test_service_failure_is_recorded_as_failure(tmp_path, monkeypatch):
    import autoclip.evidence as module
    source, transcript = tmp_path/"source.mp4", tmp_path/"transcript.json"
    source.write_bytes(b"fake-video")
    transcript.write_text(json.dumps([dict(id=3, start=10, end=12, text="王小名")]))
    closed = []

    class FailingLLM:
        def __init__(self, *args):
            self.client = SimpleNamespace(close=lambda: closed.append(True))
        def json(self, *args, **kwargs):
            raise RuntimeError("模型接口 HTTP 503")
        def usage(self):
            return {"calls_this_run": 1}

    monkeypatch.setattr(module, "LLM", FailingLLM)
    monkeypatch.setattr(module, "run", lambda args: Path(args[-1]).write_bytes(b"jpg"))
    with pytest.raises(RuntimeError, match="503"):
        inspect_subtitles(source, transcript, [3], tmp_path/"report", Settings())
    data = json.loads(next((tmp_path/"report").glob("*/evidence.json")).read_text())
    assert data["status"] == "failed"
    assert data["rows"] == []
    assert closed == [True]


def test_late_subtitle_gets_one_bounded_extra_sample(tmp_path, monkeypatch):
    import autoclip.evidence as module
    source, transcript = tmp_path/"source.mp4", tmp_path/"transcript.json"
    source.write_bytes(b"fake-video")
    transcript.write_text(json.dumps([dict(id=3, start=10, end=12, text="大人请坐")]))
    calls = []

    class LateCaptionLLM:
        def __init__(self, *args):
            self.client = SimpleNamespace(close=lambda: None)
        def json(self, *args, **kwargs):
            calls.append(1)
            return {"readable": False, "text": ""} if len(calls) == 1 else {"readable": True, "text": "大人请坐"}
        def usage(self):
            return {"calls_this_run": len(calls)}

    monkeypatch.setattr(module, "LLM", LateCaptionLLM)
    monkeypatch.setattr(module, "run", lambda args: Path(args[-1]).write_bytes(b"jpg"))
    data = json.loads(inspect_subtitles(source, transcript, [3], tmp_path/"report", Settings()).read_text())
    assert len(calls) == 3
    assert data["rows"][0]["status"] == "confirmed"
    assert [r["time"] for r in data["rows"][0]["frames"]] == [10.7, 11.3, 11.7]


def test_extra_agreement_cannot_outvote_conflicting_visible_text():
    assert assess("大人请坐", readings("大人请坐") + [{"readable": True, "text": "大人快走"}])["status"] == "unresolved"
