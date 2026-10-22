import json
import re
from pathlib import Path

from rich.console import Console

from .config import Settings
from .editorial import WRITING_RULES, require_oral_style, require_audience_timeline
from .llm import LLM
from .models import Clip, Line
from .repair import voice_fits
from .util import digest, duration, probe, run, timestamp, write_json, source_identity

console = Console()
VERSION = "render-9"


def synthesize(text: str, folder: Path, cfg: Settings):
    import fcntl
    import shutil
    effective_voice = cfg.macos_voice if cfg.tts == "macos" else cfg.voice
    effective_rate = cfg.macos_rate if cfg.tts == "macos" else cfg.tts_rate
    name = digest({"text": text, "voice": effective_voice, "rate": effective_rate, "backend": cfg.tts})
    legacy = folder / 'voices' / (name + '.mp3')
    target = (cfg.media_cache or folder) / "voices" / (name + ".mp3")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists() and target.stat().st_size > 100:
            return target
        if legacy != target and legacy.exists() and legacy.stat().st_size > 100:
            for suffix in ('.mp3', '.boundaries.jsonl'):
                old = legacy.with_suffix(suffix)
                if old.exists():
                    shutil.copy2(old, target.with_suffix(suffix))
            return target
        return _synthesize(text, target, cfg)


def _synthesize(text, target, cfg):
    if target.exists() and target.stat().st_size > 100:
        return target
    partial = target.with_suffix(".part.mp3")
    if cfg.tts == "edge":
        from .speech import edge_synthesize
        edge_synthesize(text, partial, target.with_suffix(".boundaries.jsonl"), cfg)
    elif cfg.tts == "azure":
        from .speech import azure_synthesize
        azure_synthesize(text, partial, cfg)
    elif cfg.tts == "macos":
        spoken = target.with_suffix(".txt")
        spoken.write_text(text, encoding="utf-8")
        aiff = target.with_suffix(".aiff")
        run(["say", "-v", cfg.macos_voice, "-r", cfg.macos_rate, "-f", spoken, "-o", aiff])
        run(["ffmpeg", "-v", "error", "-y", "-i", aiff, partial])
        aiff.unlink(missing_ok=True)
    else:
        raise ValueError("tts 必须是 edge、azure 或 macos")
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


def subtitle_chunks(text: str, limit=14):
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
    """Warm-gold headline and readable caption cards, separate from source subtitles."""
    width, height = (720,1280) if portrait else (1280,720)
    font, title_size = (42,48) if portrait else (38,40)
    title_y = 208 if portrait else 36
    caption_y = 942 if portrait else 645
    limit=12 if portrait else 25
    title_parts=[]
    for phrase in subtitle_chunks(title,limit):
        phrase=subtitle_display(phrase)
        if title_parts and len(title_parts[-1])+len(phrase)+1<=limit:
            title_parts[-1]+=' '+phrase
        else:
            title_parts.append(phrase)
    text = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Narration,Hiragino Sans GB,{font},&H00ECF7FB,&H000000FF,&H00151518,&H90000000,-1,0,0,0,100,100,0.5,0,1,1.6,1,5,40,40,0,1
Style: Title,Kaiti SC,{title_size},&H00ECF7FB,&H000000FF,&H00101820,&H90000000,-1,0,0,0,100,100,2,0,1,1.2,2,8,35,35,0,1
Style: Label,Hiragino Sans GB,24,&H007EC0E1,&H000000FF,&H00101820,&H00000000,0,0,0,0,100,100,3,0,1,0,0,8,35,35,0,1
Style: Shape,Arial,10,&H007EC0E1,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def event(layer,start,end,style,body):
        return f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{body}\n"
    # Quiet geometric accents establish hierarchy without flashing or karaoke.
    if portrait:
        text += event(0,0,seconds,'Shape',r'{\p1\pos(0,0)}m 294 178 l 350 178 350 180 294 180 m 370 178 l 426 178 426 180 370 180 m 360 174 l 365 179 360 184 355 179')
    for index,part in enumerate(title_parts):
        accent=index==len(title_parts)-1
        size=title_size+(6 if accent else 0)
        color=r'\c&H007EC0E1&' if accent else r'\c&H00ECF7FB&'
        text += event(1,0,seconds,'Title',f'{{\\pos({width//2},{title_y+index*(66 if portrait else 51)})\\fs{size}{color}}}'+ass_escape(subtitle_display(part)))
    if portrait:
        text += event(0,0,seconds,'Label',r'{\pos(360,1130)}'+ass_escape(series))
    for start,end,body,mode in captions:
        if mode!='narration':continue
        box_width=min(width-80,max(220,len(body)*(font+1)+52))
        left=(width-box_width)/2;right=left+box_width
        top=caption_y-36;bottom=caption_y+36
        shape=f'm {left+12} {top} l {right-12} {top} b {right} {top} {right} {top} {right} {top+12} l {right} {bottom-12} b {right} {bottom} {right} {bottom} {right-12} {bottom} l {left+12} {bottom} b {left} {bottom} {left} {bottom} {left} {bottom-12} l {left} {top+12} b {left} {top} {left} {top} {left+12} {top}'
        text += event(2,start,end,'Shape',r'{\p1\pos(0,0)\c&H001B1715&\alpha&H35&\fad(70,70)}'+shape)
        text += event(3,start,end,'Shape',f'{{\\p1\\pos(0,0)\\c&H007EC0E1&\\fad(70,70)}}m {left} {top+20} l {left+3} {top+20} {left+3} {bottom-20} {left} {bottom-20}')
        text += event(4,start,end,'Narration',f'{{\\pos({width//2},{caption_y})\\fad(70,70)}}'+ass_escape(body))
    path.write_text(text,encoding='utf-8')


def fit_narration(beat, root, cfg, llm, lines):
    text = beat.text
    available = beat.end - beat.start
    for attempt in range(3):
        require_audience_timeline([{'mode':'narration','text':text}])
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
                + WRITING_RULES + f"证据：{json.dumps(evidence,ensure_ascii=False)}。返回 {{\"text\":\"调整后的全文\"}}。")
            text = revision["text"].strip()
    raise ValueError(f"配音与画面时长无法匹配：{available:.1f} 秒画面 / {length:.1f} 秒配音")


def render_clip(video: Path, root: Path, clip: Clip, index: int, cfg: Settings, llm: LLM, lines: list[Line]):
    from .identity import enforce_identity
    require_oral_style(clip)
    enforce_identity(video, root, clip, cfg, llm, lines)
    from .media_cache import cached_shot, shot_inputs
    source = source_identity(video)
    key = digest({"version": VERSION, "source": source, "clip": clip.model_dump(), "cfg": cfg.model_dump(mode="json")})
    folder = root / "renders" / f"{index+1:02}-{key}"
    # Keep exact older deliveries reusable only when their source binding exists.
    # Older render keys lacked a source fingerprint; never trust them unbound.
    from .util import read_json
    if not (folder/'manifest.json').exists():
        legacy_key = digest({'version': VERSION, 'clip': clip.model_dump(), 'cfg': cfg.model_dump(mode='json')})
        legacy = root/'renders'/f'{index+1:02}-{legacy_key}'
        if (legacy/'manifest.json').exists() and (legacy/'video.mp4').exists():
            for marker in (root/'source.json', root/'development-review.json'):
                if marker.exists():
                    bound = read_json(marker)
                    if (bound.get('source', bound)) == source:
                        folder = legacy
                        break
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / "video.mp4"
    manifest_path = folder / "manifest.json"
    if final.exists() and manifest_path.exists():
        from .util import read_json
        manifest = read_json(manifest_path)
        if abs(duration(final) - manifest["duration"]) < .3:
            cached_timeline = manifest.get('timeline',[])
            if len(cached_timeline) != len(clip.beats):
                raise ValueError('缓存成片缺少完整口播时间线，无法复核实际口播')
            require_audience_timeline(cached_timeline)
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
    cache_hits = 0
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
        def encode(segment):
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
        inputs = shot_inputs(source, beat, target_length, voice, cfg)
        segment, actual, reused = cached_shot(cfg.media_cache or root/'shot-cache', inputs, encode)
        cache_hits += int(reused)
        # Concat uses 25fps packets. Use actual encoded duration for all subsequent subtitle offsets.
        timeline.append({**beat.model_dump(), "text": text, "output_start": cursor, "output_end": cursor+actual,
                         "speed": source_length/target_length, "subtitle_alignment": alignment})
        cursor += actual
        segments.append(segment)
        console.print(f"[cyan]{video.stem} 成片{index+1} 镜头 {i+1}/{len(clip.beats)} 完成[/cyan]", highlight=False)
    require_audience_timeline(timeline)
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
                "shot_cache": {"reused": cache_hits, "encoded": len(segments)-cache_hits},
                "audio_mix": audio_mix(timeline),
                "speech": {"backend": cfg.tts, "voice": cfg.macos_voice if cfg.tts == "macos" else cfg.voice,
                           "rate": cfg.macos_rate if cfg.tts == "macos" else cfg.tts_rate},
                "poster": str(poster.resolve()),
                "duration": final_seconds, "width": width, "height": height, "timeline": timeline,
                "qa": {"full_decode": "passed", "audio_video_streams": "passed", "duration": "passed",
                       "semantic_review": "model_review_not_human_certification"}}
    write_json(manifest_path, manifest)
    return manifest
