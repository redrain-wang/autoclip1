"""Opt-in, source-scoped correction layers backed by retained frame evidence."""
import argparse
import hashlib
from pathlib import Path

from .alignment import agreed_text
from .evidence import normalize
from .models import Line
from .util import digest, read_json, source_identity

VERSION = "subtitle-layer-1"


def bounded_replacement(row: dict) -> tuple[str | None, str]:
    """Limit automatic edits; these gates do not guarantee OCR or fact accuracy."""
    target = agreed_text(row)
    original = row["line"]["text"]
    # No punctuation removal, inserts, deletes, normalization or timing guesses.
    # Multi-line alignments remain useful evidence but are not split into words.
    if row.get("status") != "proposed_correction" or target is None:
        return None, "不是两帧一致的纠错建议"
    if original != normalize(original) or len(original) != len(target) or len(original) < 7:
        return None, "短句、标点或字数变化需进一步对齐"
    changes = [(a, b) for a, b in zip(original, target) if a != b]
    if not 1 <= len(changes) <= 2 or (len(original)-len(changes))/len(original) < .7:
        return None, "差异超过局部替字范围"
    if any(not ('\u4e00' <= a <= '\u9fff' and '\u4e00' <= b <= '\u9fff') for a, b in changes):
        return None, "仅允许局部汉字替换"
    numbers = set("零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟")
    if any(a in numbers or b in numbers for a, b in changes):
        return None, "金额或数字相关改动需复核"
    return target, "两帧一致、同字数、最多两字局部替换；仍需通过后续事实审核"


def build_layer(report: Path) -> dict:
    data = read_json(report)
    if data.get("status") != "completed" or not data.get("raw_lines_digest"):
        raise ValueError("需要已完成且含原始字幕指纹的字幕证据报告")
    entries, deferred, seen = [], [], set()
    for row in data["rows"]:
        line = Line.model_validate(row["line"])
        if line.id in seen:
            raise ValueError("证据报告包含重复字幕ID")
        seen.add(line.id)
        target, reason = bounded_replacement(row)
        if target is None:
            if row.get("status") != "confirmed":
                deferred.append({"id": line.id, "reason": reason})
            continue
        frames = row.get("frames", [])
        if len(frames) < 2 or len({f["time"] for f in frames}) != len(frames):
            raise ValueError("纠错证据必须有两个不同时间的画面")
        for frame in frames:
            if not line.start <= frame["time"] <= line.end:
                raise ValueError("纠错画面超出原字幕时间范围")
            path = Path(frame["path"])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != frame["sha256"]:
                raise ValueError("纠错画面缺失或内容已改变")
        entries.append({"id": line.id, "start": line.start, "end": line.end,
                        "original": line.text, "corrected": target, "reason": reason})
    return {"version": VERSION, "source": data["source"], "raw_lines_digest": data["raw_lines_digest"],
            "evidence_report": str(report.resolve()),
            "evidence_rows_digest": digest(data["rows"]), "entries": entries, "deferred": deferred}


def apply_layer(video: Path, lines: list[Line], layer_path: Path) -> tuple[list[Line], dict]:
    layer = read_json(layer_path)
    if layer.get("version") != VERSION or layer.get("source") != source_identity(video):
        raise ValueError("字幕纠错层与当前素材不匹配")
    if layer.get("raw_lines_digest") != digest([s.model_dump() for s in lines]):
        raise ValueError("字幕纠错层与当前原始字幕不匹配，需要重新核对")
    if layer != build_layer(Path(layer["evidence_report"])):
        raise ValueError("字幕纠错层与证据不一致，需要重新导出")
    lookup = {s.id: s for s in lines}
    if len(lookup) != len(lines):
        raise ValueError("原始字幕ID重复")
    replacements = {}
    for entry in layer["entries"]:
        line = lookup.get(entry["id"])
        if line is None or (line.start, line.end, line.text) != (entry["start"], entry["end"], entry["original"]):
            raise ValueError("纠错条目与原始字幕的时间或文字不一致")
        replacements[line.id] = entry["corrected"]
    effective = [s.model_copy(update={"text": replacements[s.id]}) if s.id in replacements else s.model_copy()
                 for s in lines]
    return effective, {"layer": str(layer_path.resolve()), "layer_digest": digest(layer),
                       "applied_ids": sorted(replacements), "deferred": layer["deferred"]}


def main():
    parser = argparse.ArgumentParser(description="从字幕证据导出可撤销的局部纠错层，不修改原字幕")
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Require a new destination; never overwrite a transcript or an existing layer.
    layer = build_layer(args.report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    import json
    with args.output.open("x", encoding="utf-8") as file:
        json.dump(layer, file, ensure_ascii=False, indent=2)
    print(f"已导出{len(layer['entries'])}条局部纠错；{len(layer['deferred'])}条暂缓：{args.output.resolve()}")


if __name__ == "__main__":
    main()
