"""Read-only footage and speech budgets for the director, never automatic padding."""
import math
import re


def spoken_characters(text):
    return len(re.sub(r"[\s\W_]", "", text, flags=re.UNICODE))


def uncovered_intervals(bounds, beats):
    cursor, end = bounds
    gaps = []
    for beat in sorted(beats, key=lambda b: b["start"]):
        a, b = max(bounds[0], beat["start"]), min(end, beat["end"])
        if b <= a:
            continue
        if a > cursor:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def story_budget(lines, bounds, cfg, report=None):
    minimum = cfg.min_seconds or cfg.target_seconds * .6
    maximum = cfg.max_seconds or cfg.target_seconds * 1.8
    span = bounds[1]-bounds[0]
    ratio = 1.4 if cfg.target_seconds >= 150 else 1.28
    result = {"source_bounds": list(bounds), "source_span_seconds": round(span, 3),
              "required_output_seconds": [minimum, maximum],
              "absolute_output_ceiling_seconds": round(span*ratio, 3),
              "ceiling_note": "仅为不重复画面且使用最大允许慢放的数学上限，不能据此认定故事或画面可用",
              "instructions": ["先为冲突、升级、结果分配完整原声，再按剩余目标时长写解说",
                               "无证据或重复解释不能用来补时长；保留审核和真实配音检查",
                               "所列空余区间只是未被剪入，必须审核其对白和画面是否属于当前故事"]}
    original_target = min(65, max(30, span*.32))
    rate = 5.0
    clip = (report or {}).get("clip") or {}
    beats = clip.get("beats", [])
    if not isinstance(beats, list) or any(not isinstance(b, dict) or b.get("mode") not in ("original", "narration")
            or any(not isinstance(b.get(k), (int, float)) or not math.isfinite(b[k]) for k in ("start", "end"))
            or b["start"] >= b["end"] for b in beats):
        beats = []
        result["timeline_budget_unavailable"] = "上版时间轴格式无效；先按校验错误修复，不推测空余区间"
    measured = (report or {}).get("beats", [])
    voices = [b for b in measured if b.get("mode") == "narration" and b.get("voice_seconds", 0) > 0]
    if voices:
        rate = sum(spoken_characters(b.get("text", "")) for b in voices)/sum(b["voice_seconds"] for b in voices)
    result["speech_rate"] = {"characters_per_second": round(rate, 3),
                             "basis": "same_script_measured_tts" if voices else "initial_estimate",
                             "note": "不含标点；仅供重写预算，最终仍合成并实测"}
    if beats:
        original_target = sum(b["end"]-b["start"] for b in beats if b["mode"] == "original")
    result["planning_reference"] = {
        "target_output_seconds": cfg.target_seconds,
        "original_seconds": round(original_target, 3),
        "narration_characters": math.ceil(max(0, cfg.target_seconds-original_target)*rate),
        "note": "总字数参考，不是硬性配额；目标超出故事承载能力时允许淘汰，不凑字数"}
    if not beats:
        return result
    gaps = uncovered_intervals(bounds, beats)
    opportunities = []
    for start, end in gaps:
        if end-start < 1:
            continue
        complete = [s for s in lines if s.confidence >= .4 and start <= s.start and s.end <= end]
        opportunities.append({"start": round(start, 3), "end": round(end, 3),
                              "seconds": round(end-start, 3),
                              "complete_lines": [s.model_dump() for s in complete],
                              "has_complete_dialogue": bool(complete)})
    result.update(original_seconds=round(original_target, 3),
                  unused_source_seconds=round(sum(b-a for a, b in gaps), 3),
                  unused_intervals=opportunities)
    total = (report or {}).get("measured_total_seconds")
    if total is not None:
        result.update(measured_output_seconds=total,
                      missing_to_minimum_seconds=round(max(0, minimum-total), 3),
                      missing_to_target_seconds=round(max(0, cfg.target_seconds-total), 3),
                      excess_above_maximum_seconds=round(max(0, total-maximum), 3))
    slots = []
    for row in voices:
        index = row["beat"]
        if not 0 <= index < len(beats):
            continue
        beat = beats[index]
        previous = max(bounds[0], beats[index-1]["end"]) if index else bounds[0]
        following = min(bounds[1], beats[index+1]["start"]) if index+1 < len(beats) else bounds[1]
        required = row["output_seconds"]/ratio
        room = max(0, min(40, following-previous))
        slots.append({"beat": index, "minimum_source_seconds": round(required, 3),
                      "free_window": [round(previous, 3), round(following, 3)],
                      "maximum_source_without_touching_neighbors": round(room, 3),
                      "must_rewrite_or_reallocate_neighbors": room+.04 < required,
                      "note": "不得直接照此扩镜头，须核实画面、字幕证据和完整原声切点"})
    result["narration_slots"] = slots
    return result
