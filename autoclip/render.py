import asyncio
import json
import re
from pathlib import Path

import edge_tts
from rich.console import Console

from .config import Settings
from .llm import LLM
from .models import Clip, Line
from .repair import voice_fits
from .util import digest, duration, probe, run, timestamp, write_json

console = Console()
VERSION = "render-7"


def synthesize(text: str, folder: Path, cfg: Settings):
    name = digest({"text": text, "voice": cfg.voice, "rate": cfg.tts_rate, "backend": cfg.tts})
    target = folder / "voices" / (name + ".mp3")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 100:
        return target
    partial = target.with_suffix(".part.mp3")
    if cfg.tts == "edge":
        async def speak():
            for attempt in range(3):
                try:
                    await edge_tts.Communicate(text, cfg.voice, rate=cfg.tts_rate).save(str(partial), str(target.with_suffix(".boundaries.jsonl")))
                    return
                except Exception:
                    if attempt == 2:
                        raise RuntimeError("配音服务失败；重试或显式将 tts 改为 macos 使用系统语音。") from None
                    await asyncio.sleep(2 ** attempt)
        asyncio.run(speak())
    elif cfg.tts == "macos":
        spoken = target.with_suffix(".txt")
        spoken.write_text(text, encoding="utf-8")
        aiff = target.with_suffix(".aiff")
        run(["say", "-v", "Tingting", "-r", "240", "-f", spoken, "-o", aiff])
        run(["ffmpeg", "-v", "error", "-y", "-i", aiff, partial])
        aiff.unlink(missing_ok=True)
    else:
        raise ValueError("tts 必须是 edge 或 macos")
    if duration(partial) <= 0:
        raise ValueError("配音输出为空")
    partial.replace(target)
    return target


def ass_escape(text: str):
    return text.replace("\\", "＼").replace("{", "（").replace("}", "）").replace("\n", " ").replace("\r", "")


def ass_time(t):
    centis = max(0, round(t * 100))
    h, centis = divmod(centis, 360000)
    m, centis = divmod(centis, 6000)
    s, cs = divmod(centis, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def subtitle_chunks(text: str, limit=17):
    parts = re.findall(r"[^，。！？；：、,.!?;:]+[，。！？；：、,.!?;:]?", text)
    result = []
    for part in parts:
        part = part.strip()
        result.extend(part[i:i+limit] for i in range(0,len(part),limit))
    return result or [text]


def subtitle_display(text: str) -> str:
    """Keep TTS punctuation for prosody, omit trailing marks from visible captions."""
    return re.sub(r'[\s，。！？；：、,.!?;:…—]+([”’」』）)]*)$', r'\1', text.strip())


def voice_captions(text: str, voice: Path, voice_seconds: float):
    """Use service sentence boundaries; proportional fallback is labelled in the manifest."""
    path = voice.with_suffix(".boundaries.jsonl")
    boundaries = []
    if path.exists():
        boundaries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if boundaries and all(b.get("text") for b in boundaries):
        rows = []
        for b in boundaries:
            start = b["offset"] / 1e7
            end = min(voice_seconds, (b["offset"] + b["duration"]) / 1e7)
            if end <= start:
                continue
            pieces = subtitle_chunks(b["text"])
            total = sum(len(p) for p in pieces)
            cursor = start
            for piece in pieces:
                stop = cursor + (end-start) * len(piece)/total
                rows.append((cursor, stop, piece))
                cursor = stop
        if rows:
            return rows, "tts_sentence_boundary_with_proportional_phrase_split"
    pieces = subtitle_chunks(text)
    length = sum(len(p) for p in pieces)
    cursor, rows = 0, []
    for piece in pieces:
        stop = cursor + voice_seconds*len(piece)/length
        rows.append((cursor, stop, piece))
        cursor = stop
    return rows, "proportional_estimate"


def write_ass(path: Path, title: str, series: str, seconds: float, captions, portrait=True):
    width, height = (720,1280) if portrait else (1280,720)
    font, title_size = (36,46) if portrait else (32,36)
    title_y, sub_margin = (205, 350) if portrait else (45, 35)
    title_parts = subtitle_chunks(title, 13 if portrait else 26)
    title_text = "\\N".join(ass_escape(x) for x in title_parts[:3])
    text = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Narration,Arial Unicode MS,{font},&H00FFFFFF,&H000000FF,&H00141414,&H80000000,-1,0,0,0,100,100,1,0,1,2,1,2,42,42,{sub_margin},1
Style: Title,Arial Unicode MS,{title_size},&H00FFFFFF,&H000000FF,&H00121212,&H00000000,-1,0,0,0,100,100,1,0,1,2,0,8,35,35,0,1
Style: Label,Arial Unicode MS,22,&H00B4E5FF,&H000000FF,&H00121212,&H00000000,0,0,0,0,100,100,2,0,1,1,0,8,35,35,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,{ass_time(seconds)},Title,,0,0,0,,{{\\pos({width//2},{title_y})}}{title_text}
"""
    if portrait:
        text += f"Dialogue: 0,0:00:00.00,{ass_time(seconds)},Label,,0,0,0,,{{\\pos(360,1130)}}{ass_escape(series)}\n"
    for start,end,body,mode in captions:
        if mode == "narration":
            text += f"Dialogue: 1,{ass_time(start)},{ass_time(end)},Narration,,0,0,0,,{ass_escape(body)}\n"
    path.write_text(text, encoding="utf-8")


def fit_narration(beat, root, cfg, llm, lines):
    text = beat.text
    available = beat.end - beat.start
    for attempt in range(3):
        voice = synthesize(text, root, cfg)
        length = duration(voice)
        ratio = (length + .35) / available
        if voice_fits(length + .35, available, cfg):
            return text, voice, length
        if attempt < 2:
            desired = max(6, round(len(text) * max(.5, available-.35) / length * .92))
            evidence = [s.model_dump() for s in lines if s.id in beat.evidence_ids]
            revision = llm.json("你是解说口播编辑。只调整表达长度，不增添人物、数字、剧情、因果或观点。只输出JSON。",
                f"原稿：{text}\n原稿实际配音 {length:.1f} 秒，可用画面 {available:.1f} 秒。将口播改为约{desired}字，最多{desired+2}字，保持核心意思与必要限定。"
                "删去重复复述和修饰语，不要照抄超时句子；不能把推测改成事实。"
                f"证据：{json.dumps(evidence,ensure_ascii=False)}。返回 {{\"text\":\"调整后的全文\"}}。")
            text = revision["text"].strip()
    raise ValueError(f"配音与画面时长无法匹配：{available:.1f} 秒画面 / {length:.1f} 秒配音")


def render_clip(video: Path, root: Path, clip: Clip, index: int, cfg: Settings, llm: LLM, lines: list[Line]):
    from .identity import enforce_identity
    enforce_identity(video, root, clip, cfg, llm, lines)
    key = digest({"version": VERSION, "clip": clip.model_dump(), "cfg": cfg.model_dump(mode="json")})
    folder = root / "renders" / f"{index+1:02}-{key}"
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / "video.mp4"
    manifest_path = folder / "manifest.json"
    if final.exists() and manifest_path.exists():
        from .util import read_json
        manifest = read_json(manifest_path)
        if abs(duration(final) - manifest["duration"]) < .3:
            # Cached audio may contain a shortened version of the original plan.
            # Recheck the actual spoken text when identity references change.
            if cfg.identity_gate:
                cached_timeline = manifest.get("timeline", [])
                if len(cached_timeline) != len(clip.beats):
                    raise ValueError("缓存成片缺少完整口播时间线，无法复核人物归属")
                actual_clip = clip.model_copy(deep=True)
                for beat, item in zip(actual_clip.beats, cached_timeline):
                    beat.text = item["text"]
                enforce_identity(video, root, actual_clip, cfg, llm, lines)
            if not manifest.get("poster") or not Path(manifest["poster"]).exists():
                poster = folder / "poster.jpg"
                run(["ffmpeg", "-v", "error", "-y", "-ss", "2", "-i", final, "-frames:v", "1", "-q:v", "2", poster])
                manifest["poster"] = str(poster.resolve())
                write_json(manifest_path, manifest)
            return manifest
    captions, timeline, segments = [], [], []
    cursor = 0.0
    width, height = (720,1280) if cfg.portrait else (1280,720)
    for i, beat in enumerate(clip.beats):
        source_length = beat.end-beat.start
        target_length, text = source_length, beat.text
        voice = None
        alignment = "source_asr"
        if beat.mode == "narration":
            text, voice, voice_seconds = fit_narration(beat, root, cfg, llm, lines)
            target_length = voice_seconds + .35
            rows, alignment = voice_captions(text, voice, voice_seconds)
            for a,b,body in rows:
                body = subtitle_display(body)
                if body:
                    captions.append((cursor+a, cursor+b, body, "narration"))
        # Original audio keeps the source's burned-in subtitles, without a
        # repeated editorial label telling the viewer how to watch it.
        segment_key = digest({"beat": beat.model_dump(), "text": text, "target": target_length, "version": VERSION})
        segment = folder / f"part-{i:02}-{segment_key}.mp4"
        if not segment.exists():
            args = ["ffmpeg", "-v", "error", "-y", "-ss", beat.start, "-t", source_length, "-i", video]
            if voice:
                args += ["-i", voice]
            factor = target_length/source_length
            if cfg.portrait:
                # Preserve the entire original composition on a soft background.
                vf = (f"[0:v]setpts={factor:.8f}*(PTS-STARTPTS),split=2[bg][fg];"
                      f"[bg]scale=180:320:force_original_aspect_ratio=increase,crop=180:320,boxblur=12:2,eq=brightness=-0.22,scale={width}:{height}[back];"
                      f"[fg]scale={width}:-2,setsar=1[front];[back][front]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=25,format=yuv420p[v]")
            else:
                vf = f"[0:v]setpts={factor:.8f}*(PTS-STARTPTS),scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,format=yuv420p[v]"
            if voice:
                vf += (f";[0:a]asetpts=PTS-STARTPTS,atempo={1/factor:.8f},volume={cfg.original_volume}[orig];"
                       f"[1:a]asetpts=PTS-STARTPTS,apad,atrim=duration={target_length}[voice];"
                       "[orig][voice]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]")
            else:
                vf += ";[0:a]asetpts=PTS-STARTPTS,alimiter=limit=0.95[a]"
            args += ["-filter_complex", vf, "-map", "[v]", "-map", "[a]", "-t", target_length,
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-threads", "4",
                     "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", segment]
            run(args)
        # Concat uses 25fps packets. Use actual encoded duration for all subsequent subtitle offsets.
        actual = duration(segment)
        timeline.append({**beat.model_dump(), "text": text, "output_start": cursor, "output_end": cursor+actual,
                         "speed": source_length/target_length, "subtitle_alignment": alignment})
        cursor += actual
        segments.append(segment)
        console.print(f"[cyan]{video.stem} 成片{index+1} 镜头 {i+1}/{len(clip.beats)} 完成[/cyan]", highlight=False)
    changes = [{"before": b.text, "after": t["text"], "evidence": [s.model_dump() for s in lines if s.id in b.evidence_ids]}
               for b,t in zip(clip.beats,timeline) if b.text != t["text"]]
    if changes:
        from .research import background_context
        check = llm.json("你是事实审核员，检查口播扩写/缩写有没有改变原意或新增无依据的事实。",
                         '仅依据提供的证据审核。返回JSON {"passed":true/false,"issues":["具体问题"]}。\n'
                         + background_context(cfg.series) + json.dumps(changes, ensure_ascii=False))
        write_json(folder / "voice-revision-review.json", check)
        if check.get("passed") is not True or check.get("issues") != []:
            raise ValueError("配音时长调整后的文案事实审核失败：" + str(check.get("issues")))
        actual_clip = clip.model_copy(deep=True)
        for beat, item in zip(actual_clip.beats, timeline):
            beat.text = item["text"]
        enforce_identity(video, root, actual_clip, cfg, llm, lines)
    ass = folder / "subtitles.ass"
    write_ass(ass, clip.title, cfg.series, cursor, captions, cfg.portrait)
    srt = folder / "subtitles.srt"
    srt.write_text("\n\n".join(f"{i+1}\n{timestamp(a)} --> {timestamp(b)}\n{text}" for i,(a,b,text,_) in enumerate(captions)), encoding="utf-8")
    concat = folder / "concat.txt"
    concat.write_text("\n".join("file '" + str(p.resolve()).replace("'", "'\\''") + "'" for p in segments), encoding="utf-8")
    raw = folder / "joined.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", concat, "-c", "copy", raw])
    partial = folder / "video.partial.mp4"
    # Use a relative subtitle path and cwd to avoid FFmpeg filter-path escaping pitfalls.
    import subprocess
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", "joined.mp4", "-vf", "ass=subtitles.ass", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "4", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", "video.partial.mp4"]
    proc = subprocess.run(cmd, cwd=folder, capture_output=True, text=True, timeout=1800)
    if proc.returncode:
        raise RuntimeError(proc.stderr[-2500:])
    info = probe(partial)
    final_seconds = float(info["format"]["duration"])
    if abs(final_seconds-cursor) > .5 or not {"video","audio"}.issubset({s["codec_type"] for s in info["streams"]}):
        raise ValueError("成片音视频或总时长校验失败")
    # The target is a preference, not an exact cut-off. At the default 100s,
    # deliver the requested 1–3 minute format while retaining complete dialogue.
    if not (cfg.min_seconds or cfg.target_seconds * .6) <= final_seconds <= (cfg.max_seconds or cfg.target_seconds * 1.8):
        raise ValueError(f"成片时长{final_seconds:.1f}秒偏离目标{cfg.target_seconds}秒，保留中间文件供检查")
    # Decode entire deliverable, detecting corrupt packets rather than trusting only metadata.
    run(["ffmpeg", "-v", "error", "-xerror", "-i", partial, "-f", "null", "-"])
    partial.replace(final)
    raw.unlink(missing_ok=True)
    poster = folder / "poster.jpg"
    run(["ffmpeg", "-v", "error", "-y", "-ss", min(2,final_seconds/2), "-i", final, "-frames:v", "1", "-q:v", "2", poster])
    from .metrics import audio_mix
    manifest = {"title": clip.title, "angle": clip.angle, "hook": clip.hook, "video": str(final.resolve()),
                "audio_mix": audio_mix(timeline),
                "poster": str(poster.resolve()),
                "duration": final_seconds, "width": width, "height": height, "timeline": timeline,
                "qa": {"full_decode": "passed", "audio_video_streams": "passed", "duration": "passed",
                       "semantic_review": "model_review_not_human_certification"}}
    write_json(manifest_path, manifest)
    return manifest
