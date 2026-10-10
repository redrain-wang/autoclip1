import math
import re
from pathlib import Path

from rich.console import Console

from .config import Settings
from .models import Line
from .util import digest, duration, read_json, run, timestamp, write_json

console = Console()


def load_srt(path: Path) -> list[Line]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    pattern = r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    lines = []
    for block in re.split(r"\n\s*\n", text.strip()):
        rows = block.strip().splitlines()
        time_index = next((i for i, row in enumerate(rows) if "-->" in row), None)
        if time_index is None:
            continue
        values = re.findall(pattern, rows[time_index])
        if len(values) != 2:
            continue
        times = [int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000 for h,m,s,ms in values]
        body = re.sub(r"<[^>]+>", "", " ".join(rows[time_index + 1:])).strip()
        if body and times[1] > times[0]:
            lines.append(Line(id=len(lines), start=times[0], end=times[1], text=body))
    if not lines:
        raise ValueError(f"字幕文件没有可用条目：{path}")
    return lines


def transcribe(video: Path, root: Path, cfg: Settings) -> list[Line]:
    sidecar = video.with_suffix(".srt")
    signature = digest({"asr": cfg.asr_backend, "model": cfg.asr_model, "prompt": cfg.asr_prompt,
                        "chunk": cfg.chunk_seconds, "srt": sidecar.read_text(errors="replace") if sidecar.exists() else None})
    folder = root / "asr" / signature
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / "transcript.json"
    if final.exists():
        return [Line.model_validate(v) for v in read_json(final)]
    if sidecar.exists():
        lines = load_srt(sidecar)
    else:
        total = duration(video)
        lines = []
        faster_model = None
        for chunk, start in enumerate(range(0, math.ceil(total), cfg.chunk_seconds)):
            end = min(total, start + cfg.chunk_seconds)
            cache = folder / f"chunk_{chunk:03}.json"
            if cache.exists():
                segments = read_json(cache)
            else:
                audio = folder / f"chunk_{chunk:03}.wav"
                run(["ffmpeg", "-v", "error", "-y", "-ss", start, "-i", video, "-t", end-start,
                     "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio])
                console.print(f"[cyan]{video.stem} 转写 {start / 60:.1f}–{end / 60:.1f} 分钟[/cyan]", highlight=False)
                if cfg.asr_backend == "mlx":
                    import mlx_whisper
                    from huggingface_hub import snapshot_download
                    model_path = cfg.asr_model
                    if not Path(model_path).is_dir():
                        try:
                            model_path = snapshot_download(cfg.asr_model, local_files_only=True)
                        except Exception:
                            pass
                    result = mlx_whisper.transcribe(str(audio), path_or_hf_repo=model_path, language="zh",
                        initial_prompt=cfg.asr_prompt, condition_on_previous_text=False, word_timestamps=True,
                        hallucination_silence_threshold=2.0, verbose=None)
                    segments = [{"start": s["start"], "end": s["end"], "text": s["text"],
                                 "confidence": math.exp(min(0, s.get("avg_logprob", 0)))} for s in result["segments"]]
                elif cfg.asr_backend == "faster":
                    from faster_whisper import WhisperModel
                    if faster_model is None:
                        faster_model = WhisperModel(cfg.asr_model, device="cpu", compute_type="int8")
                    result, _ = faster_model.transcribe(str(audio), language="zh", initial_prompt=cfg.asr_prompt,
                        vad_filter=True, condition_on_previous_text=False, word_timestamps=True)
                    segments = [{"start": s.start, "end": s.end, "text": s.text,
                                 "confidence": math.exp(min(0, s.avg_logprob))} for s in result]
                else:
                    raise ValueError("asr_backend 必须是 mlx 或 faster")
                write_json(cache, segments)
                audio.unlink(missing_ok=True)
            for s in segments:
                a, b = max(start, start+s["start"]), min(end, start+s["end"])
                if b > a and s["text"].strip():
                    lines.append(Line(id=len(lines), start=round(a, 3), end=round(b, 3), text=s["text"].strip(), confidence=s["confidence"]))
    if not lines:
        raise ValueError("没有识别到可用对白，停止生成以避免无依据写稿")
    write_json(final, [s.model_dump() for s in lines])
    return lines


def save_transcript(lines: list[Line], root: Path):
    write_json(root / "transcript.json", [s.model_dump() for s in lines])
    (root / "transcript.srt").write_text("\n\n".join(f"{i+1}\n{timestamp(s.start)} --> {timestamp(s.end)}\n{s.text}" for i,s in enumerate(lines)), encoding="utf-8")


def contact_sheet(video: Path, root: Path, times: list[float], name: str) -> Path:
    from PIL import Image, ImageDraw
    target = root / "frames" / f"{name}-{digest(times)}.jpg"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    width, height = 480, 294
    canvas = Image.new("RGB", (width*2, height*math.ceil(len(times)/2)), "#10151c")
    draw = ImageDraw.Draw(canvas)
    for index, t in enumerate(times):
        frame = target.parent / f"frame-{t:.3f}.jpg"
        if not frame.exists():
            run(["ffmpeg", "-v", "error", "-y", "-ss", t, "-i", video, "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "3", frame])
        with Image.open(frame) as im:
            im.thumbnail((480, 266))
            x, y = (index%2)*width, (index//2)*height
            canvas.paste(im, (x,y+24))
            draw.text((x+8,y+5), f"SOURCE {t:.2f}s", fill="white")
    canvas.save(target, quality=85)
    return target
