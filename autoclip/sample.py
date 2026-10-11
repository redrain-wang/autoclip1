"""Render a development-reviewed style sample, never the unattended production path."""

from pathlib import Path

from .identity import units_for_clip, file_hash
from .models import Plan, Line, validate_plan
from .util import digest, read_json, source_identity, write_json, duration


class FrozenScript:
    def json(self, *args, **kwargs):
        raise ValueError("试片稿已核对，禁止自动改写；请调整时间线或文字后重新核对")


def check_review(review, video, plan, lines):
    if review.get("source") != source_identity(video):
        raise ValueError("试片核对记录不属于当前原片版本")
    if review.get("plan_digest") != digest(plan.model_dump()):
        raise ValueError("试片稿已改变，需要重新核对")
    if len(plan.clips) != 1:
        raise ValueError("一次只制作一条风格试片")
    if review.get("mode") != "development_reviewed_sample" or not review.get("reviewer"):
        raise ValueError("缺少开发核对记录；不能当作自动审片结果")
    if review.get("transcript_digest") != digest([s.model_dump() for s in lines]):
        raise ValueError("试片字幕证据已改变，需要重新核对")
    units = {u['id']: u['text'] for u in units_for_clip(plan.clips[0])}
    decisions = review.get("units", [])
    if len(decisions) != len(units) or {d.get('id') for d in decisions} != set(units):
        raise ValueError("开发核对必须覆盖全部标题、观点与口播")
    ids = {s.id for s in lines}
    for decision in decisions:
        if (decision.get("text") != units[decision['id']] or decision.get("decision") != "accepted"
                or not decision.get("basis") or not decision.get("evidence_ids")
                or not set(decision['evidence_ids']).issubset(ids)):
            raise ValueError("开发核对条目不完整或未通过")
    if not review.get("visual_evidence"):
        raise ValueError("试片需要保留实际核对的原片画面")
    for frame in review["visual_evidence"]:
        path = Path(frame.get("path", ""))
        if not path.is_file() or file_hash(path) != frame.get("sha256"):
            raise ValueError("试片核对画面缺失或已改变")
    if not review.get("limitations"):
        raise ValueError("试片必须记录开发参与与核对局限")


def render_sample(video: Path, plan_path: Path, transcript: Path, review_path: Path, cfg, brand, output: Path):
    from .render import render_clip, synthesize
    from .repair import timing_report
    from .branding import package_clip
    from .deliver import export_finished
    from .report import build_report
    plan = Plan.model_validate(read_json(plan_path))
    lines = [Line.model_validate(row) for row in read_json(transcript)]
    review = read_json(review_path)
    check_review(review, video, plan, lines)
    clip = plan.clips[0]
    validate_plan(plan, lines, duration(video), 1, cfg.target_seconds,
                  cfg.min_seconds, cfg.max_seconds, cfg.opening)
    # Explicit, isolated development path. Production settings are not edited.
    local = cfg.model_copy(update={"identity_gate": False, "clips": 1})
    root = output / (video.stem + '-sample-' + digest({"plan": plan.model_dump(), "review": review,
                                                     "cfg": local.model_dump(mode="json")})[:8])
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "development-review.json", review)
    write_json(root / "plan.json", plan.model_dump())
    lengths = {i: duration(synthesize(b.text, root, local)) for i, b in enumerate(clip.beats) if b.mode == 'narration'}
    timing = timing_report(clip, lengths, local)
    write_json(root / "timing.json", timing)
    if timing['issues']:
        raise ValueError("试片实测时长不通过：" + str(timing['issues']))
    clean = render_clip(video, root, clip, 0, local, FrozenScript(), lines)
    if any(b.text != t['text'] for b,t in zip(clip.beats, clean['timeline'])):
        raise ValueError("实际口播与核对稿不一致")
    clean['qa']['semantic_review'] = 'development_reviewed_sample_not_unattended'
    clean['development_review'] = str((root / 'development-review.json').resolve())
    # Keep provenance in the clean manifest as well as the packaged deliverable.
    write_json(Path(clean['video']).parent / 'manifest.json', clean)
    finished = package_clip(video, root, clean, 0, cfg.series, brand)
    write_json(root / 'results.json', [finished])
    write_json(root / 'status.json', {'stage': '已完成', 'mode': 'development_reviewed_sample'})
    target = export_finished(output)
    build_report(output)
    return finished, target
