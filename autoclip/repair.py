"""Measured timing diagnostics for bounded editorial repair before encoding."""

from .diagnostics import classify_error


def enrich_constraints(report, lines, bounds=None):
    """Offer real, temporally eligible evidence; never silently change citations."""
    import re
    from .models import Beat, evidence_matches
    report = dict(report)
    clip = report.get("clip")
    if bounds:
        report["source_bounds"] = list(bounds)
        report["outro_budget"] = {"start": max(bounds[0],bounds[1]-15), "end":bounds[1],
                                  "instruction":"若结尾没画面，改写或替换这里的末尾原声，不能在边界之后追加总结"}
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


def prepare_voice(clip, root, cfg, llm, lines, attempt, video=None):
    """Repair a small number of spoken phrases before considering a new edit."""
    from .render import fit_narration
    from .util import write_json
    import json
    try:
        preflight_voice(clip, root, cfg, attempt)
        return clip
    except RepairNeeded as error:
        report = error.report
        issues = report["issues"]
        if not (1 <= len(issues) <= 4 and all(i["code"] == "voice_alignment" for i in issues)):
            raise
    revised = clip.model_copy(deep=True)
    changes = []
    for issue in issues:
        index = issue["beat"]
        beat = revised.beats[index]
        text, _, _ = fit_narration(beat, root, cfg, llm, lines)
        changes.append({"beat": index, "before": beat.text, "after": text,
                        "evidence_ids": beat.evidence_ids})
        beat.text = text
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
    review = llm.json("你是影视口播事实审核员，核查局部时长改写后的文案。",
        "只拦截新增无依据事实、删除必要限定导致误导、错误人物归属或矛盾因果。"
        "简洁改写、删除重复解释可以。原稿本身无依据的限定不必保留，但改后剩余内容必须有证据。"
        "不要为了保留原句而要求补回无法证实的剧情。根据完整上下文核查，不以ASR错字直接判定人物身份。"
        "特别核验‘谁提议、谁回答、谁拒绝’：字幕没有说话人标签，不能把台词相邻当成同一人说话；"
        "结合称呼、前后回应与所附画面交叉判断。不能确定主语时改为不指定说话人的准确表述，不得猜测。"
        '返回JSON {"passed":true/false,"issues":["具体镜头与问题"]}。\n'
        + json.dumps({"changes": changes, "clip": revised.model_dump(),
                      "evidence": [s.model_dump() for s in lines]}, ensure_ascii=False), images)
    write_json(root / f"voice-local-repair-{attempt}.json", {"changes": changes, "review": review})
    if review.get("passed") is not True:
        raise RepairNeeded({"stage": "voice_semantic", "clip": revised.model_dump(),
                            "issues": [{"code": "semantic_review", "message": str(review.get("issues", []))}]})
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
