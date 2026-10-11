"""Bounded cut search using measured speech; candidates are never editorial approval."""
from .models import Clip, Plan, validate_plan
from .repair import timing_report, voice_fits


def safe_cut(time, lines):
    # Include uncertain ASR too: low confidence is not evidence of silence.
    return not any(line.start + .001 < time < line.end - .001 for line in lines)


def original_remainder(beat, start, end, lines):
    """Keep whole recognized utterances, and only citations still in the audio."""
    if end - start < 1:
        return None
    if start > beat.start + .001 and not safe_cut(start, lines):
        return None
    if end < beat.end - .001 and not safe_cut(end, lines):
        return None
    retained = [line.id for line in lines if line.id in beat.evidence_ids
                and start <= line.start + .001 and line.end <= end + .001]
    if not retained:
        return None
    return beat.model_copy(update={"start": start, "end": end, "evidence_ids": retained})


def shot_options(clip, index, output_seconds, lines, cfg, *, allow_original=True):
    """Only expand this shot into gaps or its immediately adjacent original audio."""
    beat = clip.beats[index]
    if beat.mode != 'narration' or output_seconds <= (beat.end - beat.start):
        return []
    previous = clip.beats[index-1] if index else None
    following = clip.beats[index+1] if index+1 < len(clip.beats) else None
    # Never reach outside the original clip or jump over another narration shot.
    left = previous.end if previous else beat.start
    right = following.start if following else beat.end
    starts, ends = {beat.start}, {beat.end}
    ratio = 1.4 if cfg.target_seconds >= 150 else 1.28
    required = (output_seconds + .08) / ratio
    starts.add(max(left, beat.end-required))
    ends.add(min(right, beat.start+required))
    starts.add(left)
    ends.add(right)
    # A small padding inside a real gap avoids clipping the final syllable;
    # exact adjacent subtitle boundaries still remain candidates when no gap exists.
    cuts = {s.start for s in lines} | {s.end for s in lines}
    cuts |= {s.end + .08 for s in lines} | {max(0, s.start-.08) for s in lines}
    cuts = {t for t in cuts if safe_cut(t, lines)}
    if allow_original and previous and previous.mode == 'original':
        starts.update(t for t in cuts if previous.start + 1 <= t <= beat.start)
    if allow_original and following and following.mode == 'original':
        ends.update(t for t in cuts if beat.end <= t <= following.end - 1)
    options = []
    for start in sorted(starts):
        for end in sorted(ends):
            if start > beat.start or end < beat.end or not 1 <= end-start <= 40:
                continue
            if not voice_fits(output_seconds, end-start, cfg):
                continue
            revised = clip.model_copy(deep=True)
            revised.beats[index].start, revised.beats[index].end = start, end
            if previous and start < previous.end:
                if previous.mode != 'original':
                    continue
                remainder = original_remainder(previous, previous.start, start, lines)
                if remainder is None:
                    continue
                revised.beats[index-1] = remainder
            if following and end > following.start:
                if following.mode != 'original':
                    continue
                remainder = original_remainder(following, end, following.end, lines)
                if remainder is None:
                    continue
                revised.beats[index+1] = remainder
            options.append(revised)
    return options


def changes_between(before, after, lines):
    changes = []
    for index, (old, new) in enumerate(zip(before.beats, after.beats)):
        if old == new:
            continue
        removed = [s.model_dump() for s in lines if old.mode == 'original'
                   and old.start <= s.start and s.end <= old.end
                   and not (new.start <= s.start and s.end <= new.end)]
        changes.append({"beat": index, "before": old.model_dump(), "after": new.model_dump(),
                        "removed_original_lines": removed})
    return changes


def cut_candidates(clip, report, lines, cfg, *, allow_original=True):
    """Search at most eight joint alternatives for up to four overrunning shots."""
    from .util import digest
    affected = [i['beat'] for i in report['issues'] if i['code'] == 'voice_alignment']
    if not lines or not 1 <= len(affected) <= 4:
        return []
    voices = {row['beat']: row['voice_seconds'] for row in report['beats'] if row['mode'] == 'narration'}
    rows = {row['beat']: row for row in report['beats']}
    if any(rows[i]['output_seconds'] < rows[i]['source_seconds'] for i in affected):
        return []
    original_seconds = sum(b.end-b.start for b in clip.beats if b.mode == 'original')
    def cost(candidate):
        lost = original_seconds - sum(b.end-b.start for b in candidate.beats if b.mode == 'original')
        moved = sum(abs(a.start-b.start)+abs(a.end-b.end) for a,b in zip(clip.beats,candidate.beats))
        return (round(lost, 6), round(moved, 6))
    beam = [clip]
    # Reserve the outro first: it cannot borrow footage beyond the clip.
    for index in reversed(affected):
        next_beam = {}
        for current in beam:
            for option in shot_options(current, index, rows[index]['output_seconds'], lines, cfg, allow_original=allow_original):
                next_beam[digest(option.model_dump())] = option
        beam = sorted(next_beam.values(), key=cost)[:8]
        if not beam:
            return []
    candidates = []
    for candidate in beam:
        try:
            # Re-validate citations, ordering and the unchanged outer boundary.
            candidate = Clip.model_validate(candidate.model_dump())
            validate_plan(Plan(episode_summary='', genre='', style='', clips=[candidate]), lines,
                          clip.beats[-1].end, 1, cfg.target_seconds, cfg.min_seconds, cfg.max_seconds, cfg.opening)
        except ValueError:
            continue
        measured = timing_report(candidate, voices, cfg)
        if any(i['code'] == 'voice_alignment' for i in measured['issues']):
            continue
        candidates.append({"clip": candidate.model_dump(), "timing": measured,
                           "removed_original_seconds": round(cost(candidate)[0], 3),
                           "changes": changes_between(clip, candidate, lines)})
    return sorted(candidates, key=lambda c: (bool(c['timing']['issues']), c['removed_original_seconds']))[:3]


def try_recut(clip, report, root, cfg, llm, lines, attempt, video, *, allow_original=True):
    """Only measured, structurally valid, explicitly reviewed candidates can pass."""
    import json
    from .repair import RepairNeeded, preflight_voice
    from .util import write_json
    candidates = cut_candidates(clip, report, lines, cfg, allow_original=allow_original)
    write_json(root / f'timeline-repair-{attempt}.json', {"candidates": candidates})
    if not candidates:
        return None
    chosen = candidates[0]
    revised = Clip.model_validate(chosen['clip'])
    measured = chosen['timing']
    if measured['issues']:
        raise RepairNeeded(dict(measured, stage='timeline_repair', timeline_repair={
            "changes": chosen['changes'], "removed_original_seconds": chosen['removed_original_seconds'],
            "editorial_review": "pending", "instruction": "这些切点已解决口播与画面匹配，但总长仍不合格。保持合法切点，重新分配必要原声和解说，仍须完整事实与连贯性审核"}))
    images = []
    if video and cfg.vision:
        from .ingest import contact_sheet
        times = [(b.start+b.end)/2 for b in revised.beats]
        images = [contact_sheet(video, root, times, f'timeline-facts-{attempt}')]
    from .research import background_context
    review = llm.json('你是影视事实与剪辑审核员，核查实测口播驱动的局部切点调整（版本1）。',
        '口播文字未改，部分画面延长、相邻原声按字幕边界缩短。字幕分段不一定等于完整句子。'
        '核查移走的原声是否破坏完整句意、必要铺垫、说话人身份或因果，保留原声是否从半句话开始。'
        '核查全部口播是否有据、画面是否对应，观点必须区别于已发生事实，不可把建议当作已确定结果。'
        '不因为程序通过时长或字幕边界就默认可用。不确定或存在实质问题必须拒绝。'
        '只返回JSON {"passed":true/false,"issues":["具体镜头与问题"]}，通过时issues必须为空。\n'
        + background_context(cfg.series) + json.dumps({"before": clip.model_dump(), "after": revised.model_dump(),
                      "changes": chosen['changes'], "evidence": [s.model_dump() for s in lines]}, ensure_ascii=False), images)
    write_json(root / f'timeline-review-{attempt}.json', review)
    if not isinstance(review, dict) or review.get('passed') is not True or review.get('issues') != []:
        raise RepairNeeded(dict(measured, stage='timeline_semantic', issues=[{
            "code": "semantic_review", "message": str(review)}], timeline_repair={"changes": chosen['changes']}))
    preflight_voice(revised, root, cfg, f'{attempt}-recut')
    return revised
