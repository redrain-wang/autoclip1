"""Read-only subtitle evidence pilot; never changes production transcripts."""
import argparse
import hashlib
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from .config import Settings
from .llm import LLM
from .models import Line
from .util import digest, read_json, run, source_identity, write_json

VERSION = "subtitle-evidence-1"


def normalize(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text)
                   if not c.isspace() and not unicodedata.category(c).startswith(("P", "Z")))


def assess(original: str, readings: list[dict]) -> dict:
    """Agreement is a proposal, not a semantic or speaker verification."""
    result = {"status": "unresolved", "proposed_text": None}
    if len(readings) != 2 or any(not isinstance(r, dict) or r.get("readable") is not True
                                or not isinstance(r.get("text"), str) for r in readings):
        return {**result, "reason": "画面不可读或模型返回格式不合要求"}
    texts = [normalize(r["text"]) for r in readings]
    if min(map(len, texts)) < 2 or texts[0] != texts[1]:
        return {**result, "reason": "两帧读数不一致或有效文字不足"}
    similarity = SequenceMatcher(None, normalize(original), texts[0]).ratio()
    if similarity < .55:
        return {**result, "similarity": similarity, "reason": "画面与语音识别差异过大，可能未对齐"}
    if normalize(original) == texts[0]:
        return {"status": "confirmed", "proposed_text": None, "similarity": similarity,
                "reason": "两帧可见字幕与原识别一致（忽略标点及空白）"}
    return {"status": "proposed_correction", "proposed_text": readings[0]["text"].strip(),
            "similarity": similarity, "reason": "两帧读数一致，保留纠错建议；尚未自动应用"}


def inspect_subtitles(source: Path, transcript: Path, ids: list[int], output: Path, settings: Settings) -> Path:
    # Validate all input before any paid calls. IDs are scoped to this transcript.
    lines = [Line.model_validate(row) for row in read_json(transcript)]
    lookup = {line.id: line for line in lines}
    if len(lookup) != len(lines):
        raise ValueError("字幕ID重复，无法确定证据来源")
    if not ids or any(i not in lookup for i in ids):
        raise ValueError("请提供存在于该字幕文件的ID")
    selected = [lookup[i] for i in dict.fromkeys(ids)]
    identity = source_identity(source)
    transcript_hash = hashlib.sha256(transcript.read_bytes()).hexdigest()
    fingerprint = {"version": VERSION, "source": identity, "transcript_sha256": transcript_hash,
                   "ids": [line.id for line in selected], "model": settings.llm_model,
                   "endpoint": settings.llm_base_url}
    root = output.resolve() / digest(fingerprint)
    # Never replace an input file, even when the caller chose a poor output path.
    report = root / "evidence.json"
    if report.resolve() in {source.resolve(), transcript.resolve()}:
        raise ValueError("证据报告不能覆盖输入文件")
    llm = LLM(settings, root)
    rows = []
    ledger = {**fingerprint, "transcript": str(transcript.resolve()), "automatic_application": False,
              "limitation": "同一模型的两帧读数可能存在相关错误；不识别说话人，不构成事实审核通过。",
              "rows": rows}
    try:
        for line in selected:
            frames, readings = [], []
            for position in (.35, .65):
                at = round(line.start + (line.end-line.start)*position, 3)
                frame = root / "frames" / f"{line.id}-{at:.3f}.jpg"
                frame.parent.mkdir(parents=True, exist_ok=True)
                if not frame.exists():
                    run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", source,
                         "-frames:v", "1", "-vf", "crop=iw:ih*0.32:0:ih*0.68,scale=960:-2",
                         "-q:v", "2", "-y", frame])
                frames.append({"time": at, "path": str(frame),
                               "sha256": hashlib.sha256(frame.read_bytes()).hexdigest()})
                # Do not reveal the ASR reading, expected spelling, series or speaker.
                reading = llm.json(
                    "你是字幕抄录员，只抄图片中实际可见的对白字幕。不得用常识、剧情或记忆补字。",
                    '输出 {"readable": true或false, "text": "逐字可见字幕"}。'
                    '忽略台标、水印；没有字幕、看不清或被截断则 readable=false，text=""。'
                    '不要纠正图片原字，不解释，不推断说话人。', images=[frame])
                readings.append(reading)
            rows.append({"line": line.model_dump(), "frames": frames, "readings": readings,
                         **assess(line.text, readings)})
            ledger.update(status="running", usage=llm.usage(), counts=dict(Counter(r["status"] for r in rows)))
            write_json(report, ledger)
            print(f"字幕 {line.id}：{rows[-1]['status']}", flush=True)
        ledger["status"] = "completed"
        write_json(report, ledger)
        return report
    except Exception:
        # Service failure stays a failure, never becomes an OCR judgement.
        ledger.update(status="failed", usage=llm.usage())
        write_json(report, ledger)
        raise
    finally:
        llm.client.close()


def main():
    parser = argparse.ArgumentParser(description="只读核对原片字幕；保存画面证据，不改写生产字幕")
    parser.add_argument("source", type=Path)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--ids", required=True, help="逗号分隔的字幕ID")
    parser.add_argument("--output", type=Path, default=Path("output/字幕证据"))
    parser.add_argument("--config", type=Path, default=Path("configs.production.toml"))
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(args.config.resolve().parent / ".env")
    report = inspect_subtitles(args.source.resolve(), args.transcript.resolve(),
                               [int(i.strip()) for i in args.ids.split(",")],
                               args.output, Settings.load(args.config))
    print(f"证据报告：{report}")


if __name__ == "__main__":
    main()
