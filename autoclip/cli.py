import json
import re
import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from .config import Settings
from .pipeline import process
from .report import build_report
from .util import digest, read_json, source_identity, write_json

app = typer.Typer(help="自动影视解说：本地识别 → 画面理解 → 选题写稿 → 配音剪辑", pretty_exceptions_enable=False)
console = Console()
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm"}


@app.command()
def produce(source: Annotated[Path | None, typer.Argument(help="素材文件/目录；省略则读取本地.env")] = None,
            config: Path = Path("configs.production.toml"), max_new: int = 3,
            stage: str = "all", output: Path | None = None):
    """独立生成更长的新片，自动选题、审查、配音、包装和导出，保留旧成片。"""
    import os
    from dotenv import load_dotenv
    from .branding import BrandSettings, font_path
    from .production import produce_episode
    from .deliver import export_finished
    if stage not in {"all", "scout", "plan"} or not 1 <= max_new <= 6:
        raise typer.BadParameter("stage=all/scout/plan，max-new=1–6")
    base=config.resolve().parent
    load_dotenv(base/".env")
    cfg=Settings.load(config)
    cfg.output=(output or base/cfg.output).resolve()
    history=base/"output"
    if cfg.output==history or cfg.output in history.parents:
        raise typer.BadParameter("新片必须使用独立目录，不能覆盖旧output目录")
    if source is None:
        source=Path(os.getenv("AUTOCLIP_SOURCE_DIR", "videos/雍正王朝"))
        if not source.is_absolute():
            source=base/source
    source=source.resolve()
    if not source.exists():
        raise typer.BadParameter("素材路径不存在，请在.env设置AUTOCLIP_SOURCE_DIR或传入素材路径")
    brand=BrandSettings.load(base/"branding.toml")
    if not all(shutil.which(x) for x in ("ffmpeg","ffprobe")):
        raise typer.BadParameter("请安装FFmpeg和ffprobe后重试")
    cfg.api_key()
    if brand.enabled:
        from .branding import logo_bounds
        for logo in brand.watermarks or [brand.watermark]:
            logo_bounds(logo)
    if brand.enabled and brand.cover:
        font_path(brand)
    paths=[source] if source.is_file() else sorted((p for p in source.rglob("*") if p.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda p:[int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)",str(p))])
    if not paths:
        raise typer.BadParameter("素材目录没有视频")
    cfg.output.mkdir(parents=True,exist_ok=True)
    outcomes=[]
    for video in paths:
        console.rule(f"增量生产 {video.name}")
        try:
            outcomes.append(produce_episode(video,cfg,brand,history,max_new,stage))
        except Exception as ex:
            outcomes.append({"source":str(video),"status":"failed","error":str(ex)[:2000]})
            console.print(f"{video.name} 失败：{str(ex)[:1500]}",markup=False)
        write_json(cfg.output/"batch.json",outcomes)
        if stage=="all":
            latest=outcomes[-1]
            console.print(f"{video.name}：成片 {latest.get('clips',0)} 条，候选淘汰 {len(latest.get('rejected',[]))} 条，"
                          f"运行故障 {len(latest.get('failures',[])) + (latest.get('status')=='failed')} 项",markup=False)
            if not latest.get("clips"):
                for issue in latest.get("rejected",[]) + latest.get("failures",[]):
                    console.print(f"  {issue['title']}：{issue['error'].splitlines()[0][:240]}",markup=False)
    if stage=="all" and any(row.get("clips",0)>0 or row.get("status")=="completed" for row in outcomes):
        export_finished(cfg.output)
    console.print(f"新片浏览页：{build_report(cfg.output)}")
    if stage=="all" and not any(row.get("clips",0) for row in outcomes):
        console.print("本批未生成成片；详细原因已保存到 batch.json。处理结束不代表候选已通过。",markup=False)
    if any(row["status"] in {"failed","partial"} for row in outcomes):
        raise typer.Exit(1)


@app.command()
def doctor(config: Path = Path("configs.yongzheng.toml")):
    """检查本地工具和配置，不输出密钥，也不发起收费请求。"""
    cfg = Settings.load(config)
    checks = {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}
    try:
        cfg.api_key()
        checks["model_key"] = True
    except ValueError:
        checks["model_key"] = False
    import importlib.util
    checks["asr_module"] = importlib.util.find_spec("mlx_whisper" if cfg.asr_backend == "mlx" else "faster_whisper") is not None
    console.print(json.dumps({"checks": checks, "model": cfg.llm_model, "api": cfg.llm_base_url,
                              "asr": cfg.asr_model, "tts": cfg.tts}, ensure_ascii=False, indent=2))
    if not all(checks.values()):
        raise typer.Exit(1)


@app.command()
def batch(source: Annotated[Path, typer.Argument(help="电影文件或电视剧文件夹")],
          config: Path = Path("configs.yongzheng.toml"), clips: int | None = None,
          stage: str = "all", limit: int | None = None, branding: Path = Path("branding.toml")):
    """按集顺序批量生产，失败隔离，重复运行自动复用缓存。"""
    if stage not in {"all", "ingest", "analyze", "plan"}:
        raise typer.BadParameter("stage 必须是 all/ingest/analyze/plan")
    cfg = Settings.load(config)
    if clips is not None:
        cfg = Settings.model_validate({**cfg.model_dump(), "clips": clips})
    if limit is not None and limit < 1:
        raise typer.BadParameter("limit 至少为1")
    paths = [source] if source.is_file() else sorted((p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
              key=lambda p: [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", str(p))])
    if limit:
        paths = paths[:limit]
    if not paths:
        raise typer.BadParameter("没有找到视频文件")
    cfg.output.mkdir(parents=True, exist_ok=True)
    summaries, outcomes = [], []
    for video in paths:
        console.rule(f"处理 {video.name}")
        try:
            summary = process(video.resolve(), cfg, stage, summaries[-8:], branding)
            summaries.append(summary)
            outcomes.append({"source": str(video), "status": "completed"})
        except Exception as ex:
            console.print(f"[red]{video.name} 失败：{str(ex)[:1000]}[/red]", markup=False)
            outcomes.append({"source": str(video), "status": "failed", "error": str(ex)[:2000]})
            # A render/revision failure must not drop a previously approved
            # episode summary and invalidate every subsequent episode's cache.
            root = cfg.output / (video.stem + "-" + digest(source_identity(video))[:8])
            approved = root / "plan.json"
            if approved.exists():
                summaries.append({"episode": video.stem, "summary": read_json(approved)["episode_summary"]})
    write_json(cfg.output / "batch.json", outcomes)
    console.print(f"成片浏览页：{build_report(cfg.output)}")
    if any(row["status"] == "failed" for row in outcomes):
        raise typer.Exit(1)


@app.command()
def export(output: Path = Path("output")):
    """将已完成成片及最终解说稿整理到 output/成片。"""
    from .deliver import export_finished
    console.print(str(export_finished(output)))


@app.command()
def gallery(output: Path = Path("output"), port: int = 8765, open_browser: bool = False):
    """在本机提供成片浏览页。只公开 output 文件夹。"""
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    page = build_report(output)
    if open_browser:
        import urllib.request
        import webbrowser
        try:
            local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with local.open(f"http://127.0.0.1:{port}/index.html", timeout=2) as response:
                if response.read() == page.read_bytes():
                    webbrowser.open(f"http://127.0.0.1:{port}")
                    console.print("已打开现有成片浏览页。")
                    return
        except OSError:
            pass
    console.print(f"成片浏览页：http://127.0.0.1:{port}（Ctrl+C 关闭）")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(output.resolve()))
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}")
        server.serve_forever()


if __name__ == "__main__":
    app()
