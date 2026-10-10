import hashlib

import pytest

from autoclip.corrections import apply_layer, bounded_replacement, build_layer
from autoclip.models import Line
from autoclip.util import digest, read_json, source_identity, write_json


def fixture(tmp_path):
    video = tmp_path/"video.mp4"
    video.write_bytes(b"source")
    line = Line(id=7, start=10, end=13, text="又是坐过牢的行鱼之人", confidence=.7)
    row = {"line": line.model_dump(), "readings": [{"readable": True, "text": "又是坐过牢的刑余之人"}]*2,
           "status": "proposed_correction", "frames": []}
    for n, at in enumerate([11, 12]):
        frame = tmp_path/f"{n}.jpg"
        frame.write_bytes(f"frame-{n}".encode())
        row["frames"].append({"time": at, "path": str(frame), "sha256": hashlib.sha256(frame.read_bytes()).hexdigest()})
    report = tmp_path/"report.json"
    write_json(report, {"status": "completed", "source": source_identity(video),
                        "raw_lines_digest": digest([line.model_dump()]), "rows": [row]})
    layer = tmp_path/"layer.json"
    write_json(layer, build_layer(report))
    return video, [line], report, layer


def test_layer_preserves_ids_timing_confidence_and_original(tmp_path):
    video, lines, report, layer = fixture(tmp_path)
    original = lines[0].model_dump()
    result, provenance = apply_layer(video, lines, layer)
    assert result[0].text == "又是坐过牢的刑余之人"
    assert lines[0].model_dump() == original
    assert result[0].model_dump(exclude={"text"}) == lines[0].model_dump(exclude={"text"})
    assert provenance["applied_ids"] == [7]


@pytest.mark.parametrize("change", ["source", "transcript", "layer", "frame", "report"])
def test_changed_inputs_or_evidence_fail_closed(tmp_path, change):
    video, lines, report, layer = fixture(tmp_path)
    if change == "source":
        video.write_bytes(b"changed-source")
    elif change == "transcript":
        lines[0].text = "另外一句话"
    elif change == "layer":
        data = read_json(layer); data["entries"][0]["corrected"] = "没有证据的内容"
        write_json(layer, data)
    elif change == "frame":
        (tmp_path/"0.jpg").write_bytes(b"changed-image")
    else:
        data = read_json(report); data["status"] = "failed"; write_json(report, data)
    with pytest.raises(ValueError):
        apply_layer(video, lines, layer)


@pytest.mark.parametrize("before,after", [("吴司道", "邬思道"), ("在下吴司道", "在下邬思道"),
    ("请你在这里等待大人回来", "请在这里等待大人回来"),
    ("需归还三百两银子", "需归还五百两银子"), ("请先归还300两银子", "请先归还500两银子")])
def test_short_names_insertions_and_amount_edits_stay_deferred(before, after):
    row = {"line": {"text": before}, "status": "proposed_correction",
           "readings": [{"readable": True, "text": after}]*2}
    assert bounded_replacement(row)[0] is None


def test_layer_changes_downstream_content_fingerprint(tmp_path):
    video, lines, _, layer = fixture(tmp_path)
    effective, _ = apply_layer(video, lines, layer)
    assert digest([s.model_dump() for s in lines]) != digest([s.model_dump() for s in effective])


def test_production_uses_layer_only_when_requested_and_preserves_raw_transcript(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import autoclip.production as module
    from autoclip.branding import BrandSettings
    from autoclip.config import Settings

    video, lines, _, layer = fixture(tmp_path)
    cfg = Settings(output=tmp_path/"output")
    inputs = []

    class FakeLLM:
        def __init__(self, *args):
            self.client = SimpleNamespace(close=lambda: None)
        def usage(self):
            return {}

    monkeypatch.setattr(module, "duration", lambda _: 200)
    monkeypatch.setattr(module, "transcribe", lambda *args: lines)
    monkeypatch.setattr(module, "LLM", FakeLLM)
    monkeypatch.setattr(module, "analyze", lambda video, root, effective, *args: inputs.append(effective[0].text) or [])
    monkeypatch.setattr(module, "select_stories", lambda *args: module.Selection(episode_summary="", stories=[]))
    brand = BrandSettings(watermark=tmp_path/"unused.png", enabled=False)
    module.produce_episode(video, cfg, brand, tmp_path/"history", stage="scout", subtitle_layer=layer)
    root = cfg.output/(video.stem+"-"+digest(source_identity(video))[:8])
    raw = (root/"transcript.json").read_bytes()
    assert inputs[-1] == "又是坐过牢的刑余之人"
    assert read_json(root/"transcript.json")[0]["text"] == lines[0].text
    assert read_json(root/"transcript-input.json")["applied_ids"] == [7]
    module.produce_episode(video, cfg, brand, tmp_path/"history", stage="scout")
    assert inputs[-1] == lines[0].text
    assert read_json(root/"transcript.effective.json")[0]["text"] == lines[0].text
    assert read_json(root/"transcript-input.json")["applied_ids"] == []
    assert (root/"transcript.json").read_bytes() == raw


def test_production_rejects_stale_layer_before_model_calls(tmp_path, monkeypatch):
    import autoclip.production as module
    from autoclip.branding import BrandSettings
    from autoclip.config import Settings
    video, lines, _, layer = fixture(tmp_path)
    lines[0].text = "不同的转写版本"
    monkeypatch.setattr(module, "duration", lambda _: 200)
    monkeypatch.setattr(module, "transcribe", lambda *args: lines)
    monkeypatch.setattr(module, "LLM", lambda *args: pytest.fail("must validate before paid calls"))
    with pytest.raises(ValueError, match="原始字幕不匹配"):
        module.produce_episode(video, Settings(output=tmp_path/"output"), BrandSettings(watermark=tmp_path/"unused.png", enabled=False),
                               tmp_path/"history", stage="scout", subtitle_layer=layer)
