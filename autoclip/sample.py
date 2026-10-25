"""Render a development-reviewed style sample, never the unattended production path."""

from pathlib import Path

from .identity import units_for_clip, file_hash
from .models import Plan, Line, validate_plan
from .util import digest, read_json, source_identity, write_json, duration


class FrozenScript:
    frozen_script = True
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
    if review.get("watermark_index") is not None and (type(review["watermark_index"]) is not int or review["watermark_index"] < 0):
        raise ValueError("水印序号必须是非负整数")
    if review.get("cover_source_time") is not None:
        from .branding import cover_times
        cover_times([b.model_dump() for b in plan.clips[0].beats],review["cover_source_time"])
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
    # Reuse exact voice/rate/backend cache from the explicit measurement stage.
    import shutil
    prior_voices = output/'voice-preflight'/'voices'
    if prior_voices.exists():
        target_voices = root/'voices'
        target_voices.mkdir(exist_ok=True)
        for cached in prior_voices.iterdir():
            if (cached.suffix == '.mp3' and '.part.' not in cached.name) or cached.name.endswith('.boundaries.jsonl'):
                target = target_voices/cached.name
                if not target.exists(): shutil.copy2(cached,target)
    lengths = {i: duration(synthesize(b.text, root, local)) for i, b in enumerate(clip.beats) if b.mode == 'narration'}
    timing = timing_report(clip, lengths, local)
    write_json(root / "timing.json", timing)
    if timing['issues']:
        raise ValueError("试片实测时长不通过：" + str(timing['issues']))
    clean = render_clip(video, root, clip, 0, local, FrozenScript(), lines)
    if any(b.text != t['text'] for b,t in zip(clip.beats, clean['timeline'])):
        raise ValueError("实际口播与核对稿不一致")
    if review.get('cover_source_time') is not None:
        clean['cover_source_time'] = review['cover_source_time']
    clean['qa']['semantic_review'] = 'development_reviewed_sample_not_unattended'
    clean['development_review'] = str((root / 'development-review.json').resolve())
    # Keep provenance in the clean manifest as well as the packaged deliverable.
    write_json(Path(clean['video']).parent / 'manifest.json', clean)
    finished = package_clip(video, root, clean, 0, cfg.series, brand.for_clip(review.get("watermark_index") or 0))
    write_json(root / 'results.json', [finished])
    write_json(root / 'status.json', {'stage': '已完成', 'mode': 'development_reviewed_sample'})
    target = export_finished(output)
    build_report(output)
    return finished, target


def measure_sample_plan(video, plan_path, transcript, cfg, output):
    """Measure frozen text and propose footage trims, before development review."""
    from concurrent.futures import ThreadPoolExecutor
    from .render import synthesize
    from .repair import timing_report, voice_fits
    from .narrative import formal_long
    plan = Plan.model_validate(read_json(plan_path))
    lines = [Line.model_validate(x) for x in read_json(transcript)]
    if read_json(transcript.parent/'source.json') != source_identity(video):
        raise ValueError('配音预检字幕与原片版本不匹配')
    seconds = duration(video)
    check_estimate=not formal_long(cfg)
    validate_plan(plan,lines,seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening,
                  check_duration=check_estimate)
    stage = output/'voice-preflight'
    stage.mkdir(parents=True, exist_ok=True)
    clip = plan.clips[0]
    unique = list(dict.fromkeys(b.text for b in clip.beats if b.mode=='narration'))
    def voice(text):
        return text, duration(synthesize(text,stage,cfg))
    with ThreadPoolExecutor(max_workers=1) as pool:
        measured = dict(pool.map(voice,unique))
    lengths = {i:measured[b.text] for i,b in enumerate(clip.beats) if b.mode=='narration'}
    changes = []
    for i,b in enumerate(clip.beats):
        if b.mode != 'narration' or voice_fits(lengths[i]+.35,b.end-b.start,cfg):
            continue
        spoken = lengths[i]+.35
        if spoken >= .72*(b.end-b.start):
            continue  # Overlong narration requires a separately reviewed rewrite.
        original = b.model_dump()
        selected = max(1, spoken/.9)
        b.start = round(b.end-selected,3)
        try:
            validate_plan(plan,lines,seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening,
                          check_duration=check_estimate)
        except ValueError:
            b.start = original['start']
            b.end = round(b.start+selected,3)
            try:
                validate_plan(plan,lines,seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening,
                              check_duration=check_estimate)
            except ValueError:
                b.start,b.end = original['start'],original['end']
                continue
        changes.append({'beat':i,'before':[original['start'],original['end']], 'after':[b.start,b.end],
                        'reason':'保留口播、原声和引用，仅从既有口播画面范围内选取与实配匹配的子区间'})
    report = timing_report(clip,lengths,cfg)
    report['footage_changes'] = changes
    report['status'] = 'measured_not_reviewed'
    write_json(output/'timing-preflight.json',report)
    write_json(output/'plan-timed.json',plan.model_dump())
    return report
