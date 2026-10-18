"""Create a convenient local delivery folder from completed episode results."""

import os
import re
import shutil
import time
from pathlib import Path

from .util import read_json, write_json


def export_finished(output: Path) -> Path:
    target = output / "成片"
    target.mkdir(parents=True, exist_ok=True)
    previous = read_json(target / "清单.json") if (target / "清单.json").exists() else []
    completed = 0
    entries, script = [], ["# 成片与最终解说稿", "", "时间码指向原片。以下文本来自实际成片，包含配音时长校准后的修改。", ""]
    for result in sorted(output.glob("*/results.json")):
        state = result.parent / "status.json"
        if not state.exists() or read_json(state).get("stage") not in {"已完成", "部分完成"}:
            continue
        completed += 1
        episode = result.parent.name.split("-", 1)[0]
        for number, clip in enumerate(read_json(result), 1):
            safe_title = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", clip["title"]).strip("_")[:45]
            prefix = '测试样片_' if clip.get('task_kind') == 'test_sample' else ''
            filename = f"{prefix}{episode}-{number:02d}_{safe_title}.mp4"
            destination = target / filename
            source = Path(clip["video"])
            if not destination.exists() or not os.path.samefile(source, destination):
                temporary = destination.with_suffix(".partial.mp4")
                temporary.unlink(missing_ok=True)
                try:
                    os.link(source, temporary)
                except OSError:
                    shutil.copy2(source, temporary)
                temporary.replace(destination)
            entry = {"episode": episode, "title": clip["title"], "duration": clip["duration"], "file": filename}
            if clip.get('task_kind'):
                entry.update(task_kind=clip['task_kind'],duration_policy=clip['duration_policy'])
            if clip.get("cover"):
                cover_name = Path(filename).with_suffix(".jpg").name
                cover_target = target / cover_name
                temporary = cover_target.with_suffix(".partial.jpg")
                shutil.copy2(clip["cover"], temporary)
                temporary.replace(cover_target)
                entry["cover"] = cover_name
            entries.append(entry)
            script.extend([f"## {episode}-{number:02d} {clip['title']}", "", f"时长：{clip['duration']:.1f} 秒。角度：{clip['angle']}", ""])
            if clip.get('task_kind') == 'test_sample':
                script.extend(['用途：开发测试样片，不是正式长片交付。',''])
            for beat in clip["timeline"]:
                content = beat["text"] if beat["mode"] == "narration" else "【保留原声】"
                script.extend([f"原片 {beat['start']:.2f}–{beat['end']:.2f} 秒：{content}", ""])
    if not entries and not completed:
        raise ValueError("还没有完整通过当前任务的成片可导出")
    if not entries:
        script.extend(["当前已完成的任务没有符合选题标准的成片。", ""])
    # Retire only files from our previous manifest, preserving unrelated files.
    # Keep old versions recoverable when a story title changes between runs.
    current = {entry[field] for entry in entries for field in ("file", "cover") if entry.get(field)}
    archive = target / "历史版本" / str(time.time_ns())
    for entry in previous:
        for field in ("file", "cover"):
            name = entry.get(field, "")
            if name and Path(name).name == name and name not in current:
                stale = target / name
                if stale.is_file():
                    archive.mkdir(parents=True, exist_ok=True)
                    stale.replace(archive / name)
    write_json(target / "清单.json", entries)
    (target / "最终解说稿.md").write_text("\n".join(script), encoding="utf-8")
    return target.resolve()
