from pathlib import Path

import pytest

from autoclip.config import Settings
from autoclip.identity import assess_review, file_hash, load_anchors
from autoclip.models import Line
from autoclip.util import source_identity, write_json


def evidence():
    lines = [Line(id=1, start=10, end=11, text="儿臣愿交出户部"),
             Line(id=2, start=12, end=13, text="请皇阿玛允准")]
    units = [{"id": "title", "text": "八阿哥交出户部"}]
    anchor = {"id": "eight", "name": "胤禩", "aliases": ["八阿哥", "八爷"]}
    raw = {"units": [{"id": "title", "claims": [{"excerpt": "八阿哥交出户部", "subject": "八阿哥",
        "action": "交出户部", "observed_subject": "胤禩", "verdict": "supported", "basis": "anchor_and_context",
        "anchor_ids": ["eight"], "evidence_ids": [1, 2], "frame_ids": [0], "reason": "参考身份与本场对白对应"}]}]}
    return raw, units, lines, [anchor]


def assess(data):
    raw, units, lines, anchors = data
    return assess_review(raw, units, lines, anchors, [10.15], "雍正王朝")


def test_supported_subject_resolves_canonical_aliases():
    assert assess(evidence())["passed"]


@pytest.mark.parametrize("change", [
    {"subject": "太子"}, {"observed_subject": "太子"}, {"verdict": "unresolved"},
    {"verdict": "contradicted"}, {"anchor_ids": ["missing"]}, {"evidence_ids": [999]},
    {"frame_ids": [999]}, {"frame_ids": []}, {"excerpt": "并未写过的句子"},
    {"basis": "insufficient"}, {"subject": "太子", "observed_subject": "太子"},
])
def test_model_supported_cannot_override_conflicts_or_invalid_evidence(change):
    data = evidence()
    data[0]["units"][0]["claims"][0].update(change)
    assert not assess(data)["passed"]


def test_every_unit_must_be_covered_and_named_units_cannot_claim_no_attribution():
    data = evidence()
    data[1].append({"id": "hook", "text": "八阿哥说了什么"})
    with pytest.raises(ValueError, match="逐项覆盖"):
        assess(data)
    data = evidence()
    data[0]["units"][0].update(claims=[], no_claim_reason="只是一句标题")
    assert not assess(data)["passed"]
    data[1][0]["text"] = "他为什么这样做"
    assert not assess(data)["passed"]


def test_erchen_alone_is_never_a_direct_identity_proof():
    data = evidence()
    data[0]["units"][0]["claims"][0].update(basis="explicit_address_response", anchor_ids=[])
    assert not assess(data)["passed"]
    data[2][0].text = "八阿哥，你的意思呢"
    assert assess(data)["passed"]  # Citation structure only; semantic judgment is still model-based.


def test_scene_turn_belongs_to_observed_person_not_any_extra_anchor():
    raw, units, lines, anchors = evidence()
    anchors.append({"id": "crown", "name": "胤礽", "aliases": ["太子"]})
    claim = raw["units"][0]["claims"][0]
    claim.update(subject="太子", observed_subject="太子", anchor_ids=["eight", "crown"])
    scene = {"turns": [{"anchor_id": "eight", "evidence_ids": [1, 2]}]}
    result = assess_review(raw, units, lines, anchors, [10.15], "雍正王朝", scene)
    assert not result["passed"]
    assert "独立场景" in str(result["issues"])


def test_named_recipient_is_not_required_to_be_the_speaker_but_cannot_claim_active_action():
    raw, units, lines, anchors = evidence()
    claim = raw["units"][0]["claims"][0]
    claim.update(role="addressee", action="被点名")
    lines[0].text = "八阿哥，你来说说"
    scene = {"turns": []}
    assert assess_review(raw, units, lines, anchors, [10.15], "雍正王朝", scene)["passed"]
    claim["action"] = "主动交出户部"
    assert not assess_review(raw, units, lines, anchors, [10.15], "雍正王朝", scene)["passed"]


def test_reference_label_suffix_is_normalized_only_when_identity_matches():
    data = evidence()
    claim = data[0]["units"][0]["claims"][0]
    claim["observed_subject"] = "胤禩（R0）"
    assert assess(data)["passed"]
    claim["observed_subject"] = "胤禩（R9）"
    assert not assess(data)["passed"]


@pytest.mark.parametrize("frame_time,expected", [(10.5, "model_supported"), (99.0, "unresolved")])
def test_blind_scene_cannot_use_faraway_face_as_current_speaker(frame_time, expected):
    from autoclip.speakers import ground_speakers
    lines = [Line(id=1,start=8,end=9,text='八爷，您的意思呢'),Line(id=2,start=10,end=11,text='儿臣愿意')]
    raw = {'lines':[{'evidence_id':2,'speaker':'R0','basis_type':'address_response',
                     'context_ids':[1,2],'frame_ids':[0],'basis':'点名后回应'}]}
    turns = ground_speakers(raw,lines,[2],[{'id':0,'visible':['R0'],'uncertain':False}],
                           [frame_time],[{'id':'eight','name':'胤禩','aliases':['八爷']}])
    assert turns[0]['status']==expected


def test_anchor_images_and_source_version_are_checked(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-a")
    image = tmp_path / "portrait.jpg"
    image.write_bytes(b"frame-a")
    path = tmp_path / "registry.json"
    row = {"id": "eight", "name": "胤禩", "aliases": ["八爷"], "basis": "原片点名与回应",
           "reviewer": "fixture", "status": "reviewed", "source": source_identity(source),
           "image": image.name, "image_sha256": file_hash(image)}
    write_json(path, {"series": "雍正王朝", "anchors": [row]})
    assert len(load_anchors(path, "雍正王朝")[0]) == 1
    with pytest.raises(ValueError, match="作品"):
        load_anchors(path, "别的作品")
    image.write_bytes(b"changed")
    with pytest.raises(ValueError, match="图片已改变"):
        load_anchors(path, "雍正王朝")
    image.write_bytes(b"frame-a")
    source.write_bytes(b"source-b")
    with pytest.raises(ValueError, match="版本已改变"):
        load_anchors(path, "雍正王朝")


def test_render_cannot_reuse_or_write_media_before_identity_gate(tmp_path, monkeypatch):
    import autoclip.identity as identity
    import autoclip.render as render
    from autoclip.repair import RepairNeeded

    def reject(*args):
        raise RepairNeeded({"stage": "identity_review", "issues": [{"code": "semantic_review", "message": "身份不明"}]})

    monkeypatch.setattr(identity, "enforce_identity", reject)
    monkeypatch.setattr(render, "run", lambda *a: pytest.fail("不得先执行渲染"))
    from test_pipeline import fixture_plan
    _,plan=fixture_plan()
    with pytest.raises(RepairNeeded,match='身份不明'):
        render.render_clip(tmp_path / "video.mp4", tmp_path, plan.clips[0], 0, Settings(), None, [])
    assert not list(tmp_path.rglob("*.mp4"))


def test_identity_gate_requires_images_and_resolves_registry_relative_to_config(tmp_path):
    with pytest.raises(ValueError, match="vision"):
        Settings(identity_gate=True, vision=False)
    path = tmp_path / "config.toml"
    path.write_text('identity_gate = true\nidentity_registry = "refs/registry.json"')
    assert Settings.load(path).identity_registry == tmp_path / "refs/registry.json"


def test_identity_cli_and_comparison_cli_are_available_without_codex(tmp_path):
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX")}
    for command in ("identity-audit", "compare-scripts"):
        result = subprocess.run([sys.executable, "-m", "autoclip", command, "--help"],
                                cwd=tmp_path, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("action,expected", [("被征求意见", True), ("被点名询问意思", True),
                                           ("被提出交出户部", False), ("主动交出户部", False)])
def test_passive_address_normalization_does_not_grant_active_actions(action, expected):
    raw, units, lines, anchors = evidence()
    raw["units"][0]["claims"][0].update(role="actor", action=action)
    lines[0].text = "八阿哥，你的意思呢"
    assert assess_review(raw, units, lines, anchors, [10.15], "雍正王朝", {"turns": []})["passed"] is expected


@pytest.mark.parametrize('legacy',[False,True])
def test_cached_render_checks_actual_spoken_text(tmp_path, monkeypatch, legacy):
    import autoclip.identity as identity
    import autoclip.render as render
    from autoclip.models import Beat, Clip
    from autoclip.repair import RepairNeeded
    from autoclip.util import digest
    clip = Clip(title="八阿哥交出户部", angle="八阿哥交出户部", hook="八阿哥交出户部", beats=[
        Beat(mode="narration", start=10, end=20, text="八阿哥交出户部", evidence_ids=[1], reason="测试") for _ in range(4)])
    cfg = Settings(identity_gate=True)
    source = tmp_path/'source.mp4'
    source.write_bytes(b'source')
    inputs = {"version": render.VERSION, "clip": clip.model_dump(), "cfg": cfg.model_dump(mode="json")}
    if legacy:
        write_json(tmp_path/'source.json',render.source_identity(source))
    else:
        inputs['source'] = render.source_identity(source)
    key = digest(inputs)
    folder = tmp_path / "renders" / f"01-{key}"
    folder.mkdir(parents=True)
    (folder / "video.mp4").write_bytes(b"cached")
    write_json(folder / "manifest.json", {"duration": 10, "timeline": [{"text": "太子交出户部"} for _ in range(4)]})
    monkeypatch.setattr(render, "duration", lambda *args: 10)
    def check(video, root, actual, *args):
        if actual.beats[0].text == "太子交出户部":
            raise RepairNeeded({"stage": "identity_review", "issues": [{"message": "口播已改变"}]})
    monkeypatch.setattr(identity, "enforce_identity", check)
    monkeypatch.setattr(render, "run", lambda *args: pytest.fail("不得返回旧缓存或生成海报"))
    with pytest.raises(RepairNeeded):
        render.render_clip(tmp_path / "source.mp4", tmp_path, clip, 0, cfg, None, [])
    assert (folder / "video.mp4").read_bytes() == b"cached"


def test_configured_missing_registry_does_not_silently_disable_visual_evidence(tmp_path):
    with pytest.raises(ValueError, match="锚点文件不存在"):
        load_anchors(tmp_path / "missing.json", "雍正王朝")
    assert load_anchors(None, "雍正王朝") == ([], [])
