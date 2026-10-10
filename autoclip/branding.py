"""Local, repeatable branding and cover layout for approved finished stories."""

import hashlib
import re
import tomllib
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat
from pydantic import BaseModel, Field

from .util import digest, duration, read_json, run, source_identity, write_json

VERSION = "brand-1"
COVER_VERSION = "cover-3"


class BrandSettings(BaseModel):
    account: str = "爱大片儿"
    enabled: bool = True
    watermark: Path
    watermarks: list[Path] = Field(default_factory=list)
    watermark_width: float = Field(default=.27, ge=.1, le=.4)
    watermark_opacity: float = Field(default=.88, ge=.2, le=1)
    cover: bool = True
    font: Path | None = None

    @classmethod
    def load(cls, path: Path = Path("branding.toml")):
        if not path.exists():
            raise ValueError(f"缺少品牌配置：{path}，请参阅 README.md")
        values = tomllib.loads(path.read_text(encoding="utf-8"))
        for field in ("watermark", "font"):
            if values.get(field):
                values[field] = (path.resolve().parent / values[field]).resolve()
        values["watermarks"] = [(path.resolve().parent / p).resolve() for p in values.get("watermarks", [])]
        return cls(**values)

    def for_clip(self, index: int):
        if not self.watermarks:
            return self
        return self.model_copy(update={"watermark": self.watermarks[index % len(self.watermarks)], "watermarks": []})


def font_path(brand: BrandSettings) -> Path:
    candidates = [brand.font] if brand.font else []
    candidates += [Path("/System/Library/Fonts/STHeiti Medium.ttc"),
                   Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
                   Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")]
    for path in candidates:
        if path and path.exists():
            return path
    raise ValueError("封面需要中文字体，请在 branding.toml 的 font 中指定字体文件")


def logo_bounds(path: Path):
    with Image.open(path) as im:
        if "A" not in im.getbands():
            raise ValueError("台标必须是带透明通道的 PNG")
        alpha = im.getchannel("A")
        if alpha.getextrema() == (255, 255):
            raise ValueError("台标背景不透明，请使用透明水印图")
        # Ignore negligible alpha noise when finding the artwork's visible bounds.
        bounds = alpha.point(lambda p: 255 if p >= 64 else 0).getbbox()
        if not bounds:
            raise ValueError("台标是空白透明图")
        return bounds


def wrap_title(text, font, max_width):
    lines, line = [], ""
    # A short quoted payoff is a single unit, so “由他们去” stays together.
    tokens = re.findall(r'“[^”]+”|[^\n]|\n', text)
    units = [part for token in tokens for part in ([token] if font.getlength(token) <= max_width else list(token))]
    for char in units:
        if char in "，：；\n":
            if line.strip():
                lines.append(line.strip())
            line = ""
            continue
        if font.getlength(line + char) > max_width and line:
            lines.append(line.strip())
            line = ""
        line += char
    if line.strip():
        lines.append(line.strip())
    return lines


def make_cover(source: Path, manifest: dict, folder: Path, series: str, brand: BrandSettings):
    """Use an actual selected shot, with a deterministic typeset cover template."""
    beats = manifest["timeline"]
    # Prefer a story's original dialogue and include the opening shot as fallback.
    selected = [b for b in beats if b["mode"] == "original"] + beats[:1]
    frames = []
    for i, beat in enumerate(selected[:6]):
        stamp = (beat["start"] + beat["end"]) / 2
        path = folder / f"cover-source-{i}.jpg"
        run(["ffmpeg", "-v", "error", "-y", "-ss", stamp, "-i", source,
             "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "2", path])
        frame = Image.open(path).convert("RGB")
        small = frame.resize((320, 180)).convert("L")
        stats = ImageStat.Stat(small)
        edges = ImageStat.Stat(small.filter(ImageFilter.FIND_EDGES)).mean[0]
        score = stats.stddev[0] + edges * .4
        if stats.mean[0] < 22 or stats.mean[0] > 235:
            score *= .1
        frames.append((score, stamp, frame, path))
    if not frames:
        raise ValueError("没有可用于封面的已选镜头")
    _, stamp, frame, frame_path = max(frames, key=lambda item: item[0])
    portrait = manifest["height"] > manifest["width"]
    size = (1080, 1920) if portrait else (1920, 1080)
    canvas = ImageOps.fit(frame, size).filter(ImageFilter.GaussianBlur(45))
    canvas = ImageEnhance.Brightness(canvas).enhance(.28).convert("RGBA")
    shade = Image.new("RGBA", size)
    sd = ImageDraw.Draw(shade)
    for y in range(size[1]):
        sd.line((0, y, size[0], y), fill=(8, 15, 25, int(90 + 110*y/size[1])))
    canvas = Image.alpha_composite(canvas, shade)
    fontfile = str(font_path(brand))
    small_font = ImageFont.truetype(fontfile, 31 if portrait else 36)
    series_font = ImageFont.truetype(fontfile, 54)
    gold, ivory = (225, 192, 126), (251, 247, 236)
    logo = Image.open(brand.watermark).convert("RGBA").crop(logo_bounds(brand.watermark))
    logo.thumbnail((360, 145), Image.Resampling.LANCZOS)
    canvas.alpha_composite(logo, (76, 92))
    draw = ImageDraw.Draw(canvas)
    if portrait:
        draw.line((78, 300, 142, 300), fill=gold, width=6)
        draw.text((168, 269), series, font=series_font, fill=ivory)
        hero = frame.resize((1080, round(1080*frame.height/frame.width)), Image.Resampling.LANCZOS)
        if hero.height > 740:
            hero = ImageOps.contain(hero, (1080, 740))
        hero_y = 450 + (740 - hero.height)//2
        canvas.alpha_composite(hero.convert("RGBA"), ((1080-hero.width)//2, hero_y))
        title_x, title_y, max_width, max_height = 76, 1240, 928, 460
        draw.text((78, 1160), "剧情解读", font=small_font, fill=gold)
        footer_y = 1780
    else:
        hero = ImageOps.contain(frame, (1030, 700))
        canvas.alpha_composite(hero.convert("RGBA"), (76, 265+(700-hero.height)//2))
        draw.text((1190, 280), series, font=series_font, fill=gold)
        title_x, title_y, max_width, max_height = 1190, 390, 650, 500
        footer_y = 965
    for fontsize in range(100, 37, -2):
        title_font = ImageFont.truetype(fontfile, fontsize)
        clauses = re.split(r"[，：；]", manifest["title"])
        # Preserve short semantic phrases, such as 答谢宴, instead of leaving
        # a lone character on the next line just to keep a larger font.
        if any(len(c) <= 12 and title_font.getlength(c) > max_width for c in clauses):
            continue
        title_lines = wrap_title(manifest["title"], title_font, max_width)
        if len(title_lines) * int(fontsize*1.3) <= max_height:
            break
    for i, line in enumerate(title_lines):
        draw.text((title_x, title_y+i*int(fontsize*1.3)), line, font=title_font,
                  fill=gold if i == len(title_lines)-1 else ivory, stroke_width=1)
    draw.line((76, footer_y-30, size[0]-76, footer_y-30), fill=(92, 83, 65), width=2)
    draw.text((78, footer_y), brand.account + "  /  影视解说", font=small_font, fill=ivory)
    cover = folder / "cover.jpg"
    canvas.convert("RGB").save(cover, quality=95, subsampling=0)
    return {"version": COVER_VERSION, "file": str(cover.resolve()), "width": size[0], "height": size[1],
            "source": str(source), "source_time": stamp, "source_frame": str(frame_path.resolve()),
            "selection": "contrast_and_sharpness_among_selected_shots", "title": manifest["title"]}


def package_clip(source: Path, root: Path, manifest: dict, index: int, series: str, brand: BrandSettings):
    """Brand from the clean render, never from an already watermarked output."""
    if not brand.enabled:
        base = manifest.get("base_manifest")
        return read_json(Path(base)) if base else manifest
    brand = brand.for_clip(index)
    if manifest.get("base_manifest"):
        manifest = read_json(Path(manifest["base_manifest"]))
    clean = Path(manifest["video"])
    logo_sha = hashlib.sha256(brand.watermark.read_bytes()).hexdigest()
    signature = digest({"version": VERSION, "video": source_identity(clean), "title": manifest["title"],
                        "series": series, "brand": brand.model_dump(mode="json"), "logo": logo_sha,
                        "font": str(font_path(brand)) if brand.cover else None})
    folder = root / "published" / f"{index+1:02}-{signature}"
    saved = folder / "manifest.json"
    if saved.exists():
        cached = read_json(saved)
        files = [cached["video"], cached["poster"]] + ([cached["cover"]] if brand.cover else [])
        if all(Path(p).is_file() for p in files):
            if brand.cover and cached.get("cover_details", {}).get("version") != COVER_VERSION:
                details = make_cover(source, manifest, folder, series, brand)
                cached.update(cover=details["file"], cover_details=details)
                write_json(saved, cached)
            return cached
    folder.mkdir(parents=True, exist_ok=True)
    width = manifest["width"]
    left, top, right, bottom = logo_bounds(brand.watermark)
    wm_width = round(width * brand.watermark_width / 2) * 2
    margin = round(width*.045)
    wm_y = round(manifest["height"]*.043)
    watermark_filter = (f"[1:v]crop={right-left}:{bottom-top}:{left}:{top},scale={wm_width}:-2,"
                        f"format=rgba,colorchannelmixer=aa={brand.watermark_opacity}[logo];"
                        f"[0:v][logo]overlay={margin}:{wm_y}:eof_action=repeat:format=auto,format=yuv420p[v]")
    partial = folder / "video.partial.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-i", clean, "-i", brand.watermark,
         "-filter_complex", watermark_filter, "-map", "[v]", "-map", "0:a:0", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "19", "-threads", "4", "-c:a", "copy", "-movflags", "+faststart", partial])
    if abs(duration(partial)-manifest["duration"]) > .15:
        raise ValueError("水印合成后视频时长发生变化")
    run(["ffmpeg", "-v", "error", "-xerror", "-i", partial, "-f", "null", "-"])
    final = folder / "video.mp4"
    partial.replace(final)
    poster = folder / "poster.jpg"
    run(["ffmpeg", "-v", "error", "-y", "-ss", "2", "-i", final, "-frames:v", "1", "-q:v", "2", poster])
    result = {**manifest, "video": str(final.resolve()), "poster": str(poster.resolve()),
              "base_manifest": str((clean.parent / "manifest.json").resolve()),
              "branding": {"account": brand.account, "watermark": str(brand.watermark), "sha256": logo_sha,
                           "width": wm_width, "x": margin, "y": wm_y, "opacity": brand.watermark_opacity},
              "qa": {**manifest.get("qa", {}), "branded_full_decode": "passed", "audio": "copied_unchanged"}}
    if brand.cover:
        details = make_cover(source, manifest, folder, series, brand)
        result.update(cover=details["file"], cover_details=details)
    write_json(saved, result)
    return result
