"""Replay failed story selections in a new batch, with current evidence gates."""
from pathlib import Path

from .production import Selection, Story, overlap_fraction
from .util import digest, read_json, write_json


def retry_selection(previous: Path, root: Path, identity, lines, seconds, existing, maximum):
    if previous.resolve() == root.resolve():
        raise ValueError("失败重试必须使用新批次目录，保留原失败记录")
    if read_json(previous/"source.json") != identity:
        raise ValueError("失败重试的素材与当前原片不一致")
    original = Selection.model_validate(read_json(previous/"selection.json"))
    failures = read_json(previous/"rejected.json")
    titles = {r["title"] for r in failures if not str(r.get("error", "")).startswith("本批次已有覆盖")}
    if len({s.title for s in original.stories}) != len(original.stories):
        raise ValueError("原选题包含同名故事，无法唯一确定重试目标")
    lookup = {s.id: s for s in lines}
    chosen = []
    skipped = [{"title": title, "reason": "原selection记录中找不到同名选题，未猜测重试目标"}
               for title in sorted(titles-{s.title for s in original.stories})]
    for story in original.stories:
        if story.title not in titles:
            continue
        try:
            if story.end > seconds or any(i not in lookup for i in story.evidence_ids):
                raise ValueError("当前字幕缺少原选题证据或选题越界")
            first = min(lookup[i].start for i in story.evidence_ids)
            last = max(lookup[i].end for i in story.evidence_ids)
            if first < story.start-30 or last > story.end+30:
                raise ValueError("当前字幕与原选题证据时间不匹配")
            story = Story.model_validate({**story.model_dump(), "start": min(story.start, first), "end": max(story.end, last)})
            spans = [(story.start, story.end)]
            if any(overlap_fraction(spans, [(b["start"], b["end"]) for b in c["timeline"]]) > .3 for c in existing):
                raise ValueError("这个故事已被历史成片覆盖")
            if any(overlap_fraction(spans, [(s.start, s.end)]) > .3 for s in chosen):
                raise ValueError("重试选题之间重合过多")
            if len(chosen) < maximum:
                chosen.append(story)
            else:
                skipped.append({"title": story.title, "reason": "超出本次max-new上限，未重试"})
        except ValueError as error:
            skipped.append({"title": story.title, "reason": str(error)})
    selected = Selection(episode_summary=original.episode_summary, stories=chosen)
    record = {"previous": str(previous.resolve()), "requested_titles": sorted(titles),
              "accepted_titles": [s.title for s in chosen], "skipped": skipped}
    write_json(root/"retry-selection.json", record)
    write_json(root/"selections"/f"retry-{digest(selected.model_dump())}.json", selected.model_dump())
    return selected
