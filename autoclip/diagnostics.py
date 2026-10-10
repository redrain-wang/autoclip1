"""Offline diagnosis of completed batches; never changes production state."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def classify_error(message: str, minimum=150.0, maximum=240.0):
    # Failed prompts often contain the entire previous script. Its words must
    # not be mistaken for validator diagnostics or service errors.
    text = message.split("上次输出：", 1)[0]
    if re.search(r"HTTP\s*(?:401|403|429|5\d\d)\b|配音服务失败|连接超时|ConnectionError|TimeoutError", text):
        return ["service_failure"]
    if "成片音视频或总时长校验失败" in text or "ffmpeg failed" in text:
        return ["media_failure"]
    if "本批次已有覆盖此故事" in text:
        return ["already_completed"]
    issues = []
    for kind, pattern in (
        ("duplicate_footage", r"重复使用.*(?:超过|%)|新片重复"),
        ("timeline_order", r"必须按原片时间递增|重复或倒置剧情"),
        ("source_bounds", r"镜头超出|越过原片结尾"),
        ("evidence", r"不存在的字幕|没有落在画面区间|只有低置信度"),
        ("voice_alignment", r"配音与画面时长无法匹配"),
        ("semantic_review", r"语义审片.*(?:失败|未通过)|独立审核.*(?:未能|未通过)|文案事实审核失败"),
    ):
        if re.search(pattern, text):
            issues.append(kind)
    for match in re.finditer(r"(预计时长|成片时长)\s*(\d+(?:\.\d+)?)\s*秒", text):
        value = float(match[2])
        floor = minimum * .9 if match[1] == "预计时长" else minimum
        issues.append("duration_short" if value < floor else
                      "duration_long" if value > maximum else "duration_unresolved")
    return list(dict.fromkeys(issues)) or ["unknown"]


def diagnose_batch(batch: Path):
    """Read final rejection/failure ledgers, counting each candidate once per issue."""
    rows = []
    for path in sorted([*batch.glob("*/rejected.json"), *batch.glob("*/failures.json")]):
        settings_path = path.parent / "settings.json"
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        target = settings.get("target_seconds", 190)
        minimum = settings.get("min_seconds") or target * .6
        maximum = settings.get("max_seconds") or target * 1.8
        for item in json.loads(path.read_text()):
            error = item.get("error", "")
            typed=[issue["code"] for issue in item.get("issues",[]) if isinstance(issue,dict) and issue.get("code")]
            rows.append({"episode": path.parent.name, "title": item.get("title", ""),
                         "ledger": path.name, "issues": list(dict.fromkeys(typed)) or classify_error(error, minimum, maximum),
                         "diagnostic": error.split("上次输出：", 1)[0][:2000]})
    counts = Counter(issue for row in rows for issue in row["issues"])
    return {"schema_version": 1, "batch": str(batch.resolve()),
            "mode": "offline_read_only", "candidates_with_records": len(rows),
            "issue_counts": dict(sorted(counts.items())), "records": rows,
            "note": "类型可重叠；这是失败原因诊断，不判定选题价值，也不代表已完成自动修复。"}


def main():
    parser = argparse.ArgumentParser(description="只读分析已完成批次的候选失败原因，不调用模型")
    parser.add_argument("batch", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.batch.is_dir():
        parser.error("批次目录不存在")
    if args.report and args.report.resolve().is_relative_to(args.batch.resolve()):
        parser.error("诊断报告必须写入批次之外的独立目录")
    report = diagnose_batch(args.batch)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
