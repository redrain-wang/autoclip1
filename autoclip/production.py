"""Standalone incremental production; published older batches are read-only inputs."""

import fcntl
import json
import shutil
from pathlib import Path

from pydantic import BaseModel, Field, model_validator
from .branding import BrandSettings, package_clip
from .config import Settings
from .automatic import FrozenAutomaticScript, assert_approval, audit_plan, review_voice_rows
from .models import Plan
from .short_voices import episode_evidence, ensure_current_library, retain_approved, spoken_plan_matches
from .director import analyze, compact, create_plan
from .ingest import save_transcript, transcribe
from .llm import LLM
from .pipeline import console
from .render import render_clip
from .report import build_report
from .repair import RepairNeeded, enrich_constraints, repair_context
from .research import background_context, prepare_research, research_context, seed_research
from .util import digest, duration, read_json, source_identity, write_json


class Story(BaseModel):
    title: str = Field(min_length=2, max_length=32)
    angle: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    evidence_ids: list[int] = Field(min_length=2)
    reason: str

    @model_validator(mode="after")
    def ordered(self):
        if not 120 <= self.end-self.start <= 1100:
            raise ValueError("新选题需要120–1100秒原片支撑完整故事")
        return self


class Selection(BaseModel):
    episode_summary: str
    stories: list[Story] = Field(max_length=6)


class AutomaticReviewRejected(ValueError):
    pass


def content_rejection(error: Exception):
    """Only known editorial/timing gates are normal candidate rejections."""
    from .diagnostics import classify_error
    if set(classify_error(str(error))) & {"service_failure","media_failure"}:
        return False
    known={"timeline_order","source_bounds","evidence","voice_alignment","semantic_review",
           "duration_short","duration_long","duration_unresolved","duplicate_footage"}
    if isinstance(error, ValueError) and set(classify_error(str(error))) <= known:
        return True
    return isinstance(error, (RepairNeeded,AutomaticReviewRejected)) or str(error).startswith(("剪辑计划未通过", "语义审片", "第1条视频三轮语义", "独立审核",
        "配音时长调整后的文案事实审核失败", "配音与画面时长无法匹配", "成片时长", "镜头超出", "新片重复"))


def overlap_fraction(a, b):
    overlap = sum(max(0, min(x[1], y[1])-max(x[0], y[0])) for x in a for y in b)
    return overlap / max(1, min(sum(y-x for x,y in a), sum(y-x for x,y in b)))


def check_footage(clip, story, existing):
    """Run again after local recutting, before any render or publication."""
    if any(b.start < story.start-.001 or b.end > story.end+.001 for b in clip.beats):
        raise ValueError("镜头超出本选题范围，必须只用给定范围内的素材")
    spans = [(b.start, b.end) for b in clip.beats]
    for previous in existing:
        old = [(b["start"], b["end"]) for b in previous["timeline"]]
        if overlap_fraction(spans, old) > .25:
            raise ValueError("新片重复使用旧片镜头超过25%，必须更换镜头或故事")


def load_history(history: Path, identity: dict, exclude: Path | None = None):
    clips, prior = [], None
    for path in sorted(history.rglob("source.json"), key=lambda p:len(p.parts)):
        if exclude and path.resolve().is_relative_to(exclude.resolve()):
            continue
        if read_json(path) == identity:
            prior = prior or path.parent
            results = path.parent / "results.json"
            if results.exists():
                clips.extend(read_json(results))
    return prior, clips


def seed_cache(prior: Path | None, root: Path):
    if prior is None or prior.resolve() == root.resolve():
        return
    # Copy immutable inputs/caches only. No old scripts, editorial overrides or
    # finished movies are prerequisites for producing a new story.
    for name in ("asr", "frames", "llm"):
        source = prior / name
        if source.is_dir():
            for path in source.rglob("*"):
                if path.is_file():
                    target = root / name / path.relative_to(source)
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target)


def completed_stories(root: Path):
    """Recover durable successes, including a crash between packaging and indexing.

    Keep completed artifacts independent of subsequent selection/prompt changes.
    A new configuration belongs in a new batch, never rewrites a completed film.
    """
    holds_path=root/"quality-holds.json"
    held=set(read_json(holds_path)) if holds_path.exists() else set()
    stories = {}
    for selection in sorted((root / "selections").glob("*.json")):
        for item in read_json(selection).get("stories", []):
            stories[digest(item)] = item
    candidates = []
    for path in (root / "completed.json", root / "results.json"):
        if path.exists():
            candidates.extend(read_json(path))
    for manifest in sorted((root / "stories").rglob("published/*/manifest.json")):
        clip = read_json(manifest)
        story = clip.get('story') or stories.get(manifest.parents[2].name)
        if story:
            clip["story"] = story
            candidates.append(clip)
    completed, seen = [], set()
    for clip in candidates:
        if clip.get("video") in held:
            continue
        if not clip.get("story"):
            continue
        key = digest(clip["story"])
        if key in seen:
            continue
        if clip.get("qa", {}).get("branded_full_decode") != "passed":
            continue
        if not all(clip.get(field) and Path(clip[field]).is_file()
                   for field in ("video", "cover", "poster")):
            continue
        seen.add(key)
        completed.append(clip)
    return completed


def select_stories(root, lines, maps, seconds, existing, cfg, llm, maximum):
    history = [{"title": c["title"], "angle": c["angle"],
                "shots": [[b["start"], b["end"]] for b in c["timeline"]]} for c in existing]
    signature = digest({"version": 5, "lines": [s.model_dump() for s in lines], "maps": maps,
                        "background": background_context(cfg.series),
                        "history": history, "maximum": maximum, "target": cfg.target_seconds})
    cache = root / "selections" / f"{signature}.json"
    if cache.exists():
        return Selection.model_validate(read_json(cache))
    prompt = f"""为《{cfg.series}》本集寻找尚未制作的独立长解说选题，最多{maximum}条，合适才选，可以0条。
每条目标{cfg.target_seconds}秒成片，最低{cfg.min_seconds or 150}秒，需足够情节与原声支撑。
优先选有完整冲突、升级、结果的故事，可合并同一故事的相邻场景；单独一两句话的碎片不要选。
reason先讲清这条要回答的问题、矛盾怎样推进、结尾实际走到哪里；没有反转不硬造反转，不为时长拼入无关支线。
不得重做历史成片的同一个故事或观点，不能仅换标题。新选题之间也应不同。
start/end是原片绝对秒数，每条范围120–1100秒，最长不能超过原片{seconds}秒。
分析摘要只是线索，人物/金额等必须核对原始字幕，ASR姓名不确定用身份指代。
reason解释新的看点、与旧片的区别以及为什么足以撑起长视频，不要声称已看过未提供的画面。
输出JSON schema：{json.dumps(Selection.model_json_schema(),ensure_ascii=False)}
已有成片：{json.dumps(history,ensure_ascii=False)}
候选分析：{json.dumps(maps,ensure_ascii=False)}
本集字幕：{compact(lines)}
{background_context(cfg.series)}"""
    repair = ""
    for attempt in range(3):
        raw = llm.json("你是影视解说选题编辑。根据证据挖掘完整新故事，不虚构，不强行凑数。", prompt+repair)
        selected = Selection(episode_summary=raw.get("episode_summary", ""), stories=[])
        rejected = []
        lookup = {s.id:s for s in lines}
        for item in raw.get("stories", []):
            try:
                story = Story.model_validate(item)
                if story.end>seconds or any(i not in lookup for i in story.evidence_ids):
                    raise ValueError("选题越界或引用不存在的字幕")
                # Candidate ranges are coarse; keep the full cited utterances at
                # their edges, but never stretch into a distant unrelated scene.
                first=min(lookup[i].start for i in story.evidence_ids)
                last=max(lookup[i].end for i in story.evidence_ids)
                if first<story.start-30 or last>story.end+30:
                    raise ValueError("选题证据超出故事边界30秒，需要重新选题")
                original_bounds=(story.start,story.end)
                story=Story.model_validate({**story.model_dump(),"start":min(story.start,first),"end":max(story.end,last)})
                if (story.start,story.end)!=original_bounds:
                    write_json(root/f"selection-boundary-{digest(item)}.json",{"title":story.title,
                               "before":original_bounds,"after":[story.start,story.end],"reason":"按真实字幕边界保留完整证据"})
                spans=[(story.start,story.end)]
                if any(overlap_fraction(spans,[(b.start,b.end)])>.3 for b in selected.stories):
                    raise ValueError("新选题之间范围重合过多")
                if any(overlap_fraction(spans,[(b["start"],b["end"]) for b in c["timeline"]])>.3 for c in existing):
                    raise ValueError("这个故事覆盖过多已出片内容，请更换")
                if len(selected.stories)<maximum:
                    selected.stories.append(story)
            except ValueError as ex:
                rejected.append({"title":item.get("title", ""),"reason":str(ex)[:800]})
        write_json(root/f"selection-rejected-{signature}-{attempt}.json",rejected)
        if selected.stories or not rejected:
            write_json(cache,selected.model_dump())
            return selected
        repair="\n所有候选均未通过，请换其他完整故事，不要继续推荐同一批；允许返回空列表。原因："+json.dumps(rejected,ensure_ascii=False)
    write_json(cache,selected.model_dump())
    return selected


def produce_episode(video: Path, cfg: Settings, brand: BrandSettings, history: Path, maximum=3, stage="all",
                    subtitle_layer: Path | None = None, retry_rejected_from: Path | None = None,
                    library: Path | None = None, voice_manifest: Path | None = None,
                    model_path: Path | None = None, voice_python: Path | None = None):
    if cfg.duration_profile == "long":
        raise ValueError("加长解说请使用write-outline → measure-plan → render-sample；常规自动选题尚未验证长稿")
    if stage != 'scout' and (library is None or not cfg.vision):
        raise ValueError('短片制作需要人工确认声纹库和画面审核；请使用--library指定本剧参考库')
    if library is not None:
        cfg = cfg.model_copy(update={'identity_gate':False,'identity_registry':None,'narrative_mode':'story_first'})
    identity=source_identity(video)
    root=cfg.output/(video.stem+"-"+digest(identity)[:8])
    if retry_rejected_from is not None:
        if root.resolve() == retry_rejected_from.resolve():
            raise ValueError("失败重试必须使用新批次目录，保留原失败记录")
        if read_json(retry_rejected_from/"source.json") != identity:
            raise ValueError("失败重试的素材与当前原片不一致")
    root.mkdir(parents=True,exist_ok=True)
    lock=(root/".lock").open("w")
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("本集已有生产进程，请等待完成后重试") from None
    llm=None
    def status(stage,**extra):
        write_json(root/"status.json",{"source":str(video),"stage":stage,**extra})
    try:
        prior,existing=load_history(history,identity,cfg.output)
        seed_cache(prior,root)
        write_json(root/"source.json",identity)
        write_json(root/"settings.json",cfg.model_dump(mode="json"))
        status("识别与理解")
        seconds=duration(video)
        lines=transcribe(video,root,cfg)
        if any(s.end>seconds+1 for s in lines):
            raise ValueError("字幕时间超出原片，请检查外挂字幕是否匹配此版本")
        save_transcript(lines,root)
        evidence_input = {"layer": None, "applied_ids": []}
        if subtitle_layer is not None:
            from .corrections import apply_layer
            lines, evidence_input = apply_layer(video, lines, subtitle_layer)
        # Raw transcript stays immutable evidence; only downstream prompts use
        # these effective lines, whose contents already form cache signatures.
        write_json(root/"transcript.effective.json", [s.model_dump() for s in lines])
        write_json(root/"transcript-input.json", evidence_input)
        bank = voices = None
        if library is not None:
            status('扫描整集语音并关联当前声纹库')
            bank,voices = episode_evidence(video,lines,library,root,cfg,voice_manifest,model_path,voice_python)
            if stage=='all':
                from .short_voices import pending_voice_followup
                pending=pending_voice_followup(video,lines,voices,root,cfg,library)
                if pending:
                    results=retain_approved(completed_stories(root),video,lines,voices,cfg,root)
                    write_json(root/'completed.json',results)
                    write_json(root/'results.json',results)
                    status('等待新增声纹确认',clips=len(results))
                    return {'source':str(video),'candidates':0,'clips':len(results),
                            'rejected':[],'failures':[],'voice_confirmation':pending,'status':'waiting_voice_confirmation'}
        llm=LLM(cfg,root)
        from .continuity import load_continuity, continuity_prompt
        previous = load_continuity(video, cfg, root)
        dossier = None
        if cfg.research_before_produce:
            status("整理人物与分集资料（自动初稿）")
            seed_research(history,identity,root)
            dossier = prepare_research(video,root,lines,seconds,cfg,llm,previous)
        maps=analyze(video,root,lines,seconds,cfg,llm,previous)
        if dossier:
            maps = [*maps, {"research_notes": research_context(dossier)}]
        if retry_rejected_from is None:
            selection=select_stories(root,lines,maps,seconds,existing,cfg,llm,maximum)
        else:
            from .retry import retry_selection
            selection=retry_selection(retry_rejected_from,root,identity,lines,seconds,existing,maximum)
            for skipped in read_json(root/"retry-selection.json")["skipped"]:
                console.print(f"重试选题跳过：{skipped['title']}：{skipped['reason']}",markup=False)
        write_json(root/"selection.json",selection.model_dump())
        if stage=="scout":
            status("新选题检查完成",candidates=len(selection.stories))
            return {"source":str(video),"candidates":len(selection.stories),"status":"scouted"}
        results,failures,rejected=completed_stories(root),[],[]
        if voices is not None:
            results = retain_approved(results,video,lines,voices,cfg,root)
        if stage=="all":
            write_json(root/"completed.json",results)
            write_json(root/"results.json",results)
        unsuccessful_roots=[]
        for index,story in enumerate(selection.stories):
            if any(digest(c["story"])==digest(story.model_dump()) for c in results):
                console.print(f"[green]{video.stem} 已完成，保留：{story.title}[/green]",highlight=False)
                continue
            if any(overlap_fraction([(story.start,story.end)],[(b["start"],b["end"]) for b in c["timeline"]])>.3 for c in results):
                rejected.append({"title":story.title,"error":"本批次已有覆盖此故事的成片，保留已完成版本"})
                continue
            console.print(f"[cyan]{video.stem} 新选题{index+1}：{story.title}[/cyan]",highlight=False)
            storyroot=root/"stories"/digest(story.model_dump())
            if voices is not None:
                storyroot = storyroot/'runs'/digest({'voices':voices,'cfg':cfg.model_dump(mode='json'),'story':story.model_dump()})
            storyroot.mkdir(parents=True,exist_ok=True)
            relevant=[s for s in lines if s.start>=story.start and s.end<=story.end]
            brief = None
            from .draft import resume_report
            repair=resume_report(storyroot,relevant,seconds,cfg,(story.start,story.end))
            if repair:
                console.print('复用结构较完整的失败草稿，修复未通过项；转写和整集分析使用缓存',markup=False)
            clip=None
            for attempt in range(cfg.repair_attempts+1):
                try:
                    if cfg.conflict_brief and brief is None:
                        from .conflict import prepare_conflict_brief, brief_context
                        brief = prepare_conflict_brief(video,storyroot,lines,story.start,story.end,cfg,llm,story.angle)
                    local=cfg.model_copy(update={"clips":1,"style":cfg.style+f"。本次唯一选题：{story.title}。角度：{story.angle}。只能剪原片{story.start}–{story.end}秒内的故事。"})
                    story_maps = [story.model_dump()]
                    if brief is not None:
                        story_maps.append({'conflict_brief':brief_context(brief)})
                    if previous.get('episodes'):
                        story_maps.append({'prior_episode_context': continuity_prompt(previous, compact(relevant)),
                                           'continuity_signature': previous['signature']})
                    if dossier:
                        story_maps.append({"research_notes": research_context(dossier,story.start,story.end)})
                    if voices is not None:
                        story_maps.append({'voice_policy':voices['policy'],'voice_candidates':review_voice_rows([
                            row for row in voices['lines'] if row['start']<story.end and row['end']>story.start])})
                    plan=create_plan(video,storyroot,relevant,seconds,story_maps,local,llm,[],repair=repair,bounds=(story.start,story.end),measured_review=voices is not None)
                    clip=plan.clips[0]
                    check_footage(clip, story, existing+results)
                    write_json(storyroot/"plan.json",plan.model_dump())
                    if stage=='plan':
                        report = audit_plan(video,plan,lines,voices,storyroot,local,llm)
                        if not report['passed']:
                            raise RepairNeeded({'stage':'voice_source_review','clip':clip.model_dump(),
                                'issues':[{'code':'semantic_review','message':json.dumps(report['issues'],ensure_ascii=False)}]})
                        assert_approval(report,video,plan,lines,voices)
                        break
                    from .series import checkpoint, check_delivery, complete_long_plan
                    write_json(storyroot/'source.json',identity)
                    write_json(storyroot/'transcript.json',[s.model_dump() for s in lines])
                    write_json(storyroot/'voice-evidence.json',voices)
                    def approve():
                        console.print(f'{story.title}：实测配音并逐段核对画面、事实和声纹',markup=False)
                        try:
                            measured,report = complete_long_plan(video,plan,lines,voices,storyroot,local,llm,
                                validate_clip=lambda c:check_footage(c,story,existing+results))
                        except ValueError as exc:
                            if str(exc).startswith('自动修复达到上限'):
                                raise AutomaticReviewRejected(str(exc)) from exc
                            raise
                        return {'plan':measured.model_dump(),'report':report},[
                            storyroot/'plan-approved.json',storyroot/'automatic-review.json',
                            *[Path(f['path']) for f in report['frames']]]
                    approval = checkpoint(storyroot,'voice-approval',{'draft':plan.model_dump(),'voices':voices,
                        'cfg':local.model_dump(mode='json'),'story':story.model_dump()},approve)
                    plan = Plan.model_validate(approval['plan'])
                    assert_approval(approval['report'],video,plan,lines,voices)
                    clip = plan.clips[0]
                    check_footage(clip, story, existing+results)
                    plan.clips[0]=clip
                    write_json(storyroot/"plan.json",plan.model_dump())
                    ensure_current_library(library,bank)
                    clean=render_clip(video,storyroot,clip,index,local,FrozenAutomaticScript(),lines)
                    if not spoken_plan_matches(plan,clean['timeline']):
                        raise ValueError('短片实际渲染内容与声纹核定稿不一致')
                    clean['qa']['semantic_review'] = 'automatic_source_review_not_human'
                    clean['automatic_review'] = str((storyroot/'automatic-review.json').resolve())
                    write_json(Path(clean['video']).parent/'manifest.json',clean)
                    finished=package_clip(video,storyroot,clean,index,cfg.series,brand)
                    finished=check_delivery(finished,local,storyroot)
                    holds=read_json(root/"quality-holds.json") if (root/"quality-holds.json").exists() else {}
                    if finished["video"] in holds:
                        raise RepairNeeded({"stage":"quality_hold","clip":clip.model_dump(),"issues":[{
                            "code":"semantic_review","message":holds[finished["video"]]["reason"]}]})
                    finished["story"]=story.model_dump()
                    finished['voice_review'] = {'plan':str((storyroot/'plan-approved.json').resolve()),
                        'approval':str((storyroot/'automatic-review.json').resolve()),
                        'settings_digest':digest(cfg.model_dump(mode='json')),'media':source_identity(Path(finished['video']))}
                    ensure_current_library(library,bank)
                    assert_approval(approval['report'],video,plan,lines,voices)
                    write_json(Path(finished['video']).parent/'manifest.json',finished)
                    results.append(finished)
                    write_json(root/"completed.json",results)
                    write_json(root/"results.json",results)
                    status(f"新片已完成 {len(results)}/{len(selection.stories)}")
                    build_report(cfg.output)
                    break
                except (ValueError,RuntimeError) as ex:
                    editorial=content_rejection(ex)
                    repair=enrich_constraints(repair_context(ex,clip,local),relevant,(story.start,story.end),local)
                    repair["repair_round"]=attempt+1
                    attempt_path=storyroot/f"attempt-{attempt}.json"
                    if attempt_path.exists():
                        old_attempt=read_json(attempt_path)
                        write_json(storyroot/'attempt-history'/f'{digest(old_attempt)}.json',old_attempt)
                    write_json(attempt_path,{"error":str(ex)[:2500],"repair":repair,"attempt":attempt+1})
                    if not editorial or attempt==cfg.repair_attempts or isinstance(ex,AutomaticReviewRejected):
                        destination=rejected if editorial else failures
                        destination.append({"title":story.title,"error":str(ex)[:2000],"issues":repair["issues"],"attempts":attempt+1})
                        unsuccessful_roots.append(storyroot)
                        console.print(f"[yellow]此选题未通过，继续其他选题：{story.title}[/yellow]")
                        break
                    console.print(f"[yellow]{story.title}：按具体问题修复，第{attempt+1}/{cfg.repair_attempts}轮[/yellow]",highlight=False)
        write_json(root/"failures.json",failures)
        write_json(root/"rejected.json",rejected)
        if stage=="all":
            write_json(root/"results.json",results)
        voice_followup=None
        if stage=='all' and voices is not None and unsuccessful_roots:
            from .short_voices import prepare_voice_followup
            try:
                voice_followup=prepare_voice_followup(video,lines,voices,root,cfg,library,unsuccessful_roots,model_path,voice_python)
            except (ValueError,RuntimeError,OSError) as exc:
                write_json(root/'voice-followup-error.json',{'error':str(exc)[:1500]})
        elif not unsuccessful_roots and (root/'voice-followup.json').exists():
            pointer=read_json(root/'voice-followup.json')
            write_json(root/'voice-followup.json',{**pointer,'status':'resolved'})
        no_delivery = stage=='all' and not results
        status("等待新增声纹确认" if voice_followup else ("未产出成片" if no_delivery else ("部分完成" if failures or rejected else ("脚本完成" if stage=="plan" else "已完成"))),clips=len(results),failures=len(failures),rejected=len(rejected))
        return {"source":str(video),"candidates":len(selection.stories),"clips":len(results),"failures":failures,"rejected":rejected,
                "voice_confirmation":voice_followup,
                "status":"waiting_voice_confirmation" if voice_followup else ("no_delivery" if no_delivery else ("partial" if failures or rejected else "completed"))}
    except Exception as ex:
        status("失败（可断点重试）",error=str(ex)[:2000])
        raise
    finally:
        if llm:
            write_json(root/"usage.json",llm.usage())
            llm.client.close()
        fcntl.flock(lock,fcntl.LOCK_UN)
        lock.close()
