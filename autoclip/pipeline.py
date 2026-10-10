import fcntl
import time
from pathlib import Path

from rich.console import Console

from .config import Settings
from .branding import BrandSettings, package_clip
from .director import analyze, create_plan
from .ingest import save_transcript, transcribe
from .llm import LLM
from .render import render_clip
from .report import build_report
from .util import digest, duration, source_identity, write_json

console = Console()


def process(video: Path, cfg: Settings, stage="all", previous=None, brand_config=Path("branding.toml")):
    identity = source_identity(video)
    root = cfg.output / (video.stem + "-" + digest(identity)[:8])
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / ".lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError(f"{video.name} 正被另一个任务处理")
    started = time.time()
    def status(name, **extra):
        write_json(root / "status.json", {"source": str(video), "stage": name, "started": started,
                   "updated": time.time(), **extra})
        build_report(cfg.output)
    llm = None
    try:
        brand = BrandSettings.load(brand_config) if stage == "all" else None
        write_json(root / "source.json", identity)
        write_json(root / "settings.json", cfg.model_dump(mode="json"))
        seconds = duration(video)
        status("识别对白")
        lines = transcribe(video, root, cfg)
        save_transcript(lines, root)
        if any(s.end > seconds + 1 for s in lines):
            raise ValueError("字幕时间超出原片长度，请检查外挂字幕是否匹配")
        if stage == "ingest":
            status("对白识别完成")
            return {"episode": video.stem}
        llm = LLM(cfg, root)
        status("理解剧情与画面")
        maps = analyze(video, root, lines, seconds, cfg, llm)
        if stage == "analyze":
            status("剧情分析完成")
            return {"episode": video.stem}
        status("编写脚本与核查")
        plan = create_plan(video, root, lines, seconds, maps, cfg, llm, previous or [])
        write_json(root / "plan.json", plan.model_dump())
        summary = {"episode": video.stem, "summary": plan.episode_summary}
        if stage == "plan":
            status("脚本完成")
            return summary
        status("配音与剪辑")
        results = []
        if not plan.clips:
            write_json(root / "results.json", results)
        for i, clip in enumerate(plan.clips):
            clean = render_clip(video, root, clip, i, cfg, llm, lines)
            results.append(package_clip(video, root, clean, i, cfg.series, brand))
            write_json(root / "results.json", results)
            status(f"已完成 {i+1}/{len(plan.clips)} 条")
        status("已完成", elapsed_seconds=round(time.time()-started,1), clips=len(results))
        return summary
    except Exception as ex:
        status("失败（可断点重试）", error=str(ex)[:2000])
        raise
    finally:
        if llm:
            write_json(root / "usage.json", llm.usage())
            llm.client.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
