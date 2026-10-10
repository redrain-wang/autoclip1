"""Conservative temporal grouping of ASR lines and visible subtitle readings.

Groups describe evidence coverage, never word-level timing or speaker identity.
"""
from difflib import SequenceMatcher

from .evidence import consensus, normalize
from .models import Line


def agreed_text(row: dict) -> str | None:
    return consensus(row.get("readings", []))


def caption_sequence(row: dict) -> dict | None:
    """Record a sampled caption transition, not an exact subtitle change time."""
    frames, readings = row.get("frames", []), row.get("readings", [])
    if len(frames) != len(readings) or len(frames) < 4:
        return None
    runs = []
    for frame, reading in sorted(zip(frames, readings), key=lambda x: x[0]["time"]):
        if (not isinstance(reading, dict) or reading.get("readable") is not True
                or not isinstance(reading.get("text"), str)):
            return None
        text = normalize(reading["text"])
        if len(text) < 2:
            return None
        if runs and runs[-1]["text"] == text:
            runs[-1]["observed_times"].append(frame["time"])
        else:
            runs.append({"text": text, "observed_times": [frame["time"]]})
    if len(runs) != 2 or any(len(set(r["observed_times"])) < 2 for r in runs):
        return None
    target = "".join(r["text"] for r in runs)
    original = normalize(row["line"]["text"])
    similarity = SequenceMatcher(None, original, target).ratio()
    if similarity < .7:
        return None
    return {"line_ids": [row["line"]["id"]], "start": row["line"]["start"], "end": row["line"]["end"],
            "relation": "one_to_many", "status": "confirmed" if target == original else "proposed_correction",
            "asr_text": original, "visible_text": target, "similarity": similarity, "caption_observations": runs,
            "transition_window": [runs[0]["observed_times"][-1], runs[1]["observed_times"][0]]}


def align_rows(rows: list[dict], lines: list[Line]) -> list[dict]:
    """Combine up to three adjacent ASR lines only with consistent frame evidence.

    Each constituent line needs two agreeing frames of the same visible subtitle.
    Long gaps, repeated unrelated captions and ambiguous best matches are held.
    A line cannot participate in two groups. Different captions within one ASR
    line need two observations per caption and retain an uncertain transition
    window; exact word timing is never inferred.
    """
    ordered = sorted(lines, key=lambda s: (s.start, s.end, s.id))
    by_id = {r["line"]["id"]: r for r in rows}
    candidates = []
    for start in range(len(ordered)):
        for size in range(1, 4):
            group = ordered[start:start+size]
            if len(group) != size or any(s.id not in by_id for s in group):
                continue
            if group[-1].end-group[0].start > 12:
                continue
            if any(not 0 <= b.start-a.end <= 1.5 for a, b in zip(group, group[1:])):
                continue
            targets = [agreed_text(by_id[s.id]) for s in group]
            if targets[0] is None or len(set(targets)) != 1:
                continue
            target = targets[0]
            original = "".join(normalize(s.text) for s in group)
            score = SequenceMatcher(None, original, target).ratio()
            if score < .6:
                continue
            if size > 1:
                individual = max(SequenceMatcher(None, normalize(s.text), target).ratio() for s in group)
                if individual >= .6 and score-individual < .15:
                    continue
            candidates.append({"line_ids": [s.id for s in group], "start": group[0].start,
                               "end": group[-1].end, "asr_text": original, "visible_text": target,
                               "similarity": round(score, 6),
                               "relation": "one_to_one" if size == 1 else "many_to_one",
                               "status": "confirmed" if original == target else "proposed_correction"})
    candidates.sort(key=lambda x: (-x["similarity"], x["start"], len(x["line_ids"])))
    chosen, used, ambiguous = [], set(), set()
    for candidate in candidates:
        ids = set(candidate["line_ids"])
        if ids & (used | ambiguous):
            continue
        competing = [c for c in candidates if c is not candidate and ids & set(c["line_ids"])
                     and c["line_ids"] != candidate["line_ids"]
                     and abs(c["similarity"]-candidate["similarity"]) < .08]
        if competing:
            ambiguous.update(ids)
            for other in competing:
                ambiguous.update(other["line_ids"])
            continue
        chosen.append(candidate)
        used.update(ids)
    for row in rows:
        line = row["line"]
        if line["id"] not in used:
            sequence = caption_sequence(row)
            if sequence is not None:
                chosen.append(sequence)
                continue
            chosen.append({"line_ids": [line["id"]], "start": line["start"], "end": line["end"],
                           "status": "unresolved", "relation": "unresolved",
                           "reason": "缺少一致且唯一的时间邻接证据；不推测字幕切换或逐字时刻"})
    return sorted(chosen, key=lambda x: (x["start"], x["line_ids"]))
