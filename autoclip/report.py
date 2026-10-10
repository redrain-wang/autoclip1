import html
from pathlib import Path
from urllib.parse import quote

from .util import read_json


def build_report(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    cards = []
    for root in sorted(output.iterdir()):
        if not root.is_dir() or not (root / "status.json").exists():
            continue
        status = read_json(root / "status.json")
        name = html.escape(status.get("source", root.name))
        stage = html.escape(status.get("stage", "等待"))
        error = html.escape(status.get("error", ""))
        clips = read_json(root / "results.json") if (root / "results.json").exists() else []
        block = f'<section><div class="episode"><h2>{name}</h2><span>{stage}</span></div>'
        if error:
            block += f'<p class="error">{error}</p>'
        if status.get("stage") == "已完成" and status.get("clips") == 0:
            block += '<p>未发现符合当前标准的完整选题，本集未生成视频。</p>'
        block += '<div class="grid">'
        for clip in clips:
            relative = Path(clip["video"]).relative_to(output.resolve())
            link = quote(str(relative))
            preview = clip.get("cover") or clip.get("poster")
            poster = quote(str(Path(preview).relative_to(output.resolve()))) if preview else ""
            cover_link = (f'<a class="download" href="{quote(str(Path(clip["cover"]).relative_to(output.resolve())))}" download>下载封面</a>'
                          if clip.get("cover") else "")
            script = "\n\n".join(f'[{b["start"]:.1f}–{b["end"]:.1f}s] '+ ('【原声】' if b['mode']=='original' else b['text']) for b in clip['timeline'])
            block += f'''<article><video controls preload="metadata" poster="{poster}" src="{link}"></video><div class="body"><div class="meta">{clip['duration']:.0f} 秒 · {clip['width']} × {clip['height']}</div><h3>{html.escape(clip['title'])}</h3><p>{html.escape(clip['angle'])}</p><a class="download" href="{link}" download>下载成片</a> {cover_link}<details><summary>查看解说稿与原片时间码</summary><pre>{html.escape(script)}</pre></details></div></article>'''
        block += '</div></section>'
        cards.append(block)
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>剧作工坊 · 自动解说成片</title><style>
*{box-sizing:border-box}body{margin:0;background:#0b1118;color:#ecf0f5;font:16px/1.65 system-ui,-apple-system,sans-serif}main{max-width:1200px;margin:auto;padding:48px 28px}header{padding-bottom:36px;border-bottom:1px solid #26313f}.eyebrow{color:#dabb81;letter-spacing:4px;font-size:12px}h1{font-size:40px;letter-spacing:-1px;margin:12px 0}header p{color:#a0adbc;max-width:720px}.episode{display:flex;align-items:center;justify-content:space-between}.episode span,.meta{font-size:12px;color:#dabb81}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px}article{overflow:hidden;background:#141e2b;border:1px solid #293647;border-radius:14px}video{width:100%;max-height:500px;background:#05070a}.body{padding:22px}h3{font-size:22px;margin:8px 0}p{color:#b7c1cd}.download{display:inline-block;background:#dabb81;color:#161b24;padding:8px 18px;border-radius:8px;text-decoration:none}details{margin-top:18px;color:#aeb9c8}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.8 system-ui;max-height:380px;overflow:auto}.error{color:#ffaeab}footer{margin-top:48px;color:#7f90a4;font-size:13px}section{margin-top:30px}@media(max-width:600px){main{padding:24px 16px}h1{font-size:30px}}
</style><main><header><div class="eyebrow">AUTOCLIP / 剧作工坊</div><h1>让精彩剧情，有话可说。</h1><p>从整集理解，到观点解说，再到原声与画面的节奏编排。这里汇集本地生产的成片，以及每一段解说对应的原片证据。</p></header>'''
    document += "".join(cards) or '<p>还没有完成的任务。运行批量生产命令后刷新此页。</p>'
    document += '<footer>自动事实核查与技术质检已纳入流程；抽样画面和语音识别仍可能遗漏细节。最终效果以实际观看为准。</footer></main></html>'
    path = output / "index.html"
    path.write_text(document, encoding="utf-8")
    return path.resolve()
