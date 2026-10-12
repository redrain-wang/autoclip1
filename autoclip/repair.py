"""Measured timing diagnostics for bounded editorial repair before encoding."""

from .diagnostics import classify_error


def enrich_constraints(report, lines, bounds=None, cfg=None):
    """Offer real, temporally eligible evidence; never silently change citations."""
    import re
    from .models import Beat, evidence_matches
    report = dict(report)
    clip = report.get("clip")
    if bounds:
        report["source_bounds"] = list(bounds)
        report["outro_budget"] = {"start": max(bounds[0],bounds[1]-15), "end":bounds[1],
                                  "instruction":"若结尾没画面，改写或替换这里的末尾原声，不能在边界之后追加总结"}
        if cfg is not None:
            from .budget import story_budget
            report["timing_budget"] = story_budget(lines, bounds, cfg, report)
    if not clip:
        return report
    affected = {i["beat"] for i in report.get("issues",[]) if "beat" in i}
    for issue in report.get("issues",[]):
        affected.update(int(m) for m in re.findall(r"beats\[(\d+)\]",issue["message"]))
    lookup={line.id:line for line in lines}
    context=set()
    details=[]
    for index, raw in enumerate(clip.get("beats",[])):
        try:
            beat=Beat.model_validate(raw)
        except ValueError:
            continue
        if index in affected:
            eligible=[s for s in lines if s.confidence>=.4 and evidence_matches(s,beat,context)]
            eligible.sort(key=lambda s: (not (s.start<beat.end and s.end>beat.start),abs(s.end-beat.start)))
            details.append({"beat":index,"shot":[beat.start,beat.end],
                "cited_lines":[lookup[i].model_dump() for i in beat.evidence_ids if i in lookup],
                "missing_ids":[i for i in beat.evidence_ids if i not in lookup],
                "eligible_evidence":[s.model_dump() for s in eligible[:20]],
                "instruction":"这些仅在时间上可用。必须核实语义；没有证据就删掉断言或换镜头，不得只改ID蒙混过关"})
        context.update(i for i in beat.evidence_ids if i in lookup and lookup[i].start<beat.end and lookup[i].end>beat.start)
    report["affected_shots"]=details
    return report


def voice_fits(output_seconds, source_seconds, cfg):
    # Rendering is quantized to 25fps. A single frame is the tolerance for
    # duration rounding, never an excuse to accommodate multi-second overruns.
    ceiling = 1.4 if cfg.target_seconds >= 150 else 1.28
    return .72 * source_seconds - .04 <= output_seconds <= ceiling * source_seconds + .04


class RepairNeeded(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__("成片预检需要修复：" + "; ".join(i["message"] for i in report["issues"]))


def timing_report(clip, voice_seconds, cfg):
    """Inspect real narration lengths. Never stretch footage or rewrite facts."""
    minimum = cfg.min_seconds or cfg.target_seconds * .6
    maximum = cfg.max_seconds or cfg.target_seconds * 1.8
    max_ratio = 1.4 if cfg.target_seconds >= 150 else 1.28
    rows, issues, total = [], [], 0.0
    for index, beat in enumerate(clip.beats):
        available = beat.end - beat.start
        output = voice_seconds[index] + .35 if beat.mode == "narration" else available
        row = {"beat": index, "mode": beat.mode, "start": beat.start, "end": beat.end,
               "source_seconds": round(available, 3), "output_seconds": round(output, 3)}
        if beat.mode == "narration":
            ratio = output / available
            row.update(text=beat.text, voice_seconds=voice_seconds[index],
                       allowed_source_seconds=[round(output/max_ratio, 3), round(output/.72, 3)])
            if not voice_fits(output, available, cfg):
                issues.append({"code": "voice_alignment", "beat": index,
                               "message": f"镜头{index}口播需{output:.2f}秒，画面为{available:.2f}秒；可用画面须在{output/max_ratio:.2f}–{output/.72:.2f}秒"})
        rows.append(row)
        total += output
    if not minimum <= total <= maximum:
        issues.append({"code": "duration_short" if total < minimum else "duration_long",
                       "message": f"实测配音加原声合计{total:.2f}秒，必须在{minimum:.0f}–{maximum:.0f}秒"})
    return {"schema_version": 1, "stage": "voice_preflight", "issues": issues,
            "measured_total_seconds": round(total, 3), "required_total_seconds": [minimum, maximum],
            "beats": rows, "clip": clip.model_dump()}


def preflight_voice(clip, root, cfg, attempt):
    from .render import synthesize
    from .util import duration, write_json
    lengths = {i: duration(synthesize(b.text, root, cfg))
               for i, b in enumerate(clip.beats) if b.mode == "narration"}
    report = timing_report(clip, lengths, cfg)
    write_json(root / f"voice-preflight-{attempt}.json", report)
    if report["issues"]:
        raise RepairNeeded(report)
    return report


def review_voice_changes(revised, changes, root, cfg, llm, lines, attempt, video=None, *, expansion=False):
    """Review new text against the full context; ambiguous verdicts fail closed."""
    import json
    from .util import write_json
    from .research import background_context
    images=[]
    if video and cfg.vision:
        from .ingest import contact_sheet
        times=[]
        for change in changes:
            beat=revised.beats[change["beat"]]
            cited=[s for s in lines if s.id in beat.evidence_ids]
            first=min([beat.start]+[s.start for s in cited])
            last=max([beat.end]+[s.end for s in cited])
            times.extend([max(0,first-3),first+.2,(first+last)/2,last-.2])
        images=[contact_sheet(video,root,times,f"voice-facts-{attempt}")]
    from .editorial import WRITING_RULES
    extra = ("本次为局部扩写，还须拦截凑字数的空话、重复复述，以及新增内容超出原引用依据。"
             "解释或人物动机推测必须明确是分析，不能当成已经发生的事实。" if expansion else "")
    review = llm.json("你是影视口播事实审核员，核查局部时长改写后的文案。",
        "只拦截新增无依据事实、删除必要限定导致误导、错误人物归属或矛盾因果。"
        "简洁改写、删除重复解释可以。原稿本身无依据的限定不必保留，但改后剩余内容必须有证据。"
        "不要为了保留原句而要求补回无法证实的剧情。根据完整上下文核查，不以ASR错字直接判定人物身份。"
        "特别核验‘谁提议、谁回答、谁拒绝’：字幕没有说话人标签，不能把台词相邻当成同一人说话；"
        "结合称呼、前后回应与所附画面交叉判断。不能确定主语时改为不指定说话人的准确表述，不得猜测。"
        '返回JSON {"passed":true/false,"issues":["具体镜头与问题"]}。\n'
        + WRITING_RULES + extra + background_context(cfg.series) + json.dumps({"changes": changes, "clip": revised.model_dump(),
                      "evidence": [s.model_dump() for s in lines]}, ensure_ascii=False), images)
    write_json(root / f"voice-local-repair-{attempt}.json", {"changes": changes, "review": review})
    if review.get("passed") is not True or review.get("issues") != []:
        raise RepairNeeded({"stage": "voice_semantic", "clip": revised.model_dump(),
                            "issues": [{"code": "semantic_review", "message": str(review.get("issues", []))}]})


def expansion_slots(clip, report, cfg):
    """Offer measured slots only if two can cover the deficit; never change cuts."""
    from .budget import spoken_characters
    if [i["code"] for i in report["issues"]] != ["duration_short"]:
        return []
    minimum, maximum = report["required_total_seconds"]
    deficit = minimum - report["measured_total_seconds"]
    if not 0 < deficit <= 12:
        return []
    remaining = min(deficit + 2, maximum - report["measured_total_seconds"] - .25)
    if remaining < deficit:
        return []
    ratio = 1.4 if cfg.target_seconds >= 150 else 1.28
    candidates = []
    for row in report["beats"]:
        if row["mode"] != "narration" or row["voice_seconds"] <= 0:
            continue
        beat = clip.beats[row["beat"]]
        # Leave half a second below the stretch ceiling for TTS variability.
        capacity = ratio * (beat.end - beat.start) - row["output_seconds"] - .5
        if capacity >= 1:
            candidates.append((capacity, row))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if sum(capacity for capacity, _ in candidates[:2]) + .001 < remaining:
        return []
    slots = []
    for capacity, row in candidates:
        extra = min(capacity, remaining)
        rate = spoken_characters(row["text"]) / row["voice_seconds"]
        target = row["voice_seconds"] + extra
        slots.append({"beat": row["beat"], "extra_seconds": round(extra, 3),
                      "target_voice_seconds": round(target, 3),
                      "reference_characters": round(target * rate),
                      "maximum_output_seconds": round(row["output_seconds"] + capacity, 3)})
    return slots


def expand_short_voice(clip, report, slots, root, llm, lines, attempt):
    """One bounded proposal, with a text-only schema and explicit right to decline."""
    import json
    from .budget import spoken_characters
    from .util import write_json
    from .editorial import WRITING_RULES
    result = llm.json("你是影视解说编辑，进行一次有证据的局部口播扩写（版本2）。",
        "这条片子仅略短，所有镜头和口播已经匹配。从候选slots中选择最值得补充的1–2段口播改写全文，镜头、原声和引用不得改变。"
        "余量最大的镜头不一定最值得扩写，请按证据与必要解释选择。各段extra_seconds是可分配上限，不要求每段都用满；"
        "合计增加约missing_seconds+2秒作为实测余量。最多改两段，其他原文保持不变。"
        "依据原引用补充观众理解冲突所需的解释，避免空话、重复复述、煽情套话。"
        "人物动机只可作有依据且明确的分析，不得增加事件、人物归属、数字或确定因果。"
        "完整上下文只用于核实语义，不能把后文事实提前当作本镜头的引用；不改其他镜头。"
        "目标配音秒数和有效字数只是参考，实测仍须通过。"
        '若没有值得增加的内容，返回 {"declined":true,"reason":"原因"}；否则返回 '
        '{"changes":[{"beat":镜头索引,"text":"扩写后完整口播","reason":"增加什么必要信息及引用依据"}]}。\n'
        + WRITING_RULES + json.dumps({"slots": slots,
                      "missing_seconds": round(report["required_total_seconds"][0] - report["measured_total_seconds"], 3),
                      "clip": clip.model_dump(),
                      "evidence": [s.model_dump() for s in lines]}, ensure_ascii=False))
    write_json(root / f"voice-expansion-{attempt}.json", {"slots": slots, "proposal": result})
    changes = result.get("changes") if isinstance(result, dict) else None
    expected = {slot["beat"] for slot in slots}
    valid = isinstance(changes, list) and 1 <= len(changes) <= 2
    seen = set()
    if valid:
        for change in changes:
            if not isinstance(change, dict):
                valid = False
                break
            index, text = change.get("beat"), change.get("text")
            if (type(index) is not int or index not in expected or index in seen
                    or not isinstance(text, str) or not text.strip()
                    or spoken_characters(text) <= spoken_characters(clip.beats[index].text)
                    or not isinstance(change.get("reason"), str) or not change["reason"].strip()
                    or set(change) != {"beat", "text", "reason"}):
                valid = False
                break
            seen.add(index)
    if not valid or (isinstance(result, dict) and result.get("declined")):
        failed = dict(report, local_expansion={"status": "declined_or_invalid", "proposal": result})
        raise RepairNeeded(failed)
    revised = clip.model_copy(deep=True)
    recorded = []
    for change in changes:
        beat = revised.beats[change["beat"]]
        recorded.append({"beat": change["beat"], "before": beat.text, "after": change["text"].strip(),
                         "evidence_ids": beat.evidence_ids, "reason": change["reason"]})
        beat.text = change["text"].strip()
    return revised, recorded


def prepare_voice(clip, root, cfg, llm, lines, attempt, video=None):
    """Try a bounded local text repair before rewriting the entire story."""
    from .render import fit_narration
    try:
        preflight_voice(clip, root, cfg, attempt)
        return clip
    except RepairNeeded as error:
        report = error.report
        issues = report["issues"]
        from .recut import try_recut
        recut = try_recut(clip, report, root, cfg, llm, lines, attempt, video, allow_original=False)
        if recut is not None:
            return recut
        slots = expansion_slots(clip, report, cfg)
        if not slots and not (1 <= len(issues) <= 4 and all(i["code"] == "voice_alignment" for i in issues)):
            raise
    if slots:
        revised, changes = expand_short_voice(clip, report, slots, root, llm, lines, attempt)
        # Cheap timing rejection first, without spending on visual review.
        preflight_voice(revised, root, cfg, f"{attempt}-expanded")
    else:
        revised = clip.model_copy(deep=True)
        changes = []
        for issue in issues:
            index = issue["beat"]
            beat = revised.beats[index]
            try:
                text, _, _ = fit_narration(beat, root, cfg, llm, lines)
            except ValueError as error:
                if not str(error).startswith("配音与画面时长无法匹配"):
                    raise
                # Keep original measurements when local shortening fails. A
                # generic error used to discard the evidence and trigger the
                # same unsuccessful full rewrite on every remaining retry.
                from .util import write_json
                # Preserve dialogue first. Only borrow from original audio if
                # both unused footage and local wording adjustments failed.
                try:
                    partial_report = preflight_voice(revised, root, cfg, f"{attempt}-partial")
                except RepairNeeded as measured_error:
                    partial_report = measured_error.report
                recut = try_recut(revised, partial_report, root, cfg, llm, lines, attempt, video)
                if recut is not None:
                    write_json(root / f"voice-local-repair-{attempt}.json", {
                        "changes": changes, "review": "Full clip checked in timeline-review report",
                        "timeline_review": f"timeline-review-{attempt}.json"})
                    return recut
                failed = dict(report, local_repair={"status": "voice_alignment_failed",
                    "beat": index, "error": str(error),
                    "discarded_changes": changes,
                    "instruction": "局部改写已失败；原稿实测见beats。须重新分配此镜头与相邻完整原声，不要原样重试"})
                write_json(root / f"voice-local-repair-{attempt}.json", failed)
                raise RepairNeeded(failed) from error
            changes.append({"beat": index, "before": beat.text, "after": text,
                            "evidence_ids": beat.evidence_ids})
            beat.text = text
    review_voice_changes(revised, changes, root, cfg, llm, lines, attempt, video, expansion=bool(slots))
    preflight_voice(revised, root, cfg, f"{attempt}-repaired")
    return revised


def repair_context(error, clip, cfg):
    if isinstance(error, RepairNeeded):
        return error.report
    # Legacy errors remain supported until every validator emits typed issues.
    message = str(error).split("上次输出：", 1)[0][:2200]
    return {"schema_version": 1, "stage": "validation",
            "issues": [{"code": kind, "message": message} for kind in classify_error(
                message, cfg.min_seconds or cfg.target_seconds*.6, cfg.max_seconds or cfg.target_seconds*1.8)],
            "clip": clip.model_dump() if clip else None}
