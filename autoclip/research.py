"""Source-bound study notes. Model notes are leads, never verified identities."""

import json
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from .util import digest, read_json, source_identity, timestamp, write_json

VERSION = "research-1"


def seed_research(history, identity, root):
    """Reuse only notes for the same material; input signatures check the rest."""
    for marker in history.rglob("source.json"):
        if marker.parent.resolve() == root.resolve() or read_json(marker) != identity:
            continue
        for item in (marker.parent / "research").glob("*.json"):
            target = root / "research" / item.name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)


def profile(series: str) -> dict:
    # Exact match prevents leaking this drama's roles into unrelated films.
    if series != "雍正王朝":
        return {"series": series, "characters": [], "rules": ["没有已核对的人物背景表；不得凭模型记忆填写身份。"]}
    return read_json(Path(__file__).parent / "knowledge" / "yongzheng.json")


def background_context(series: str) -> str:
    return "\n人物背景（只作消歧，不是当前说话人证明）：" + json.dumps(profile(series), ensure_ascii=False)


class StudyEvent(BaseModel):
    event: str = Field(min_length=1, max_length=900)
    participants: list[str] = Field(max_length=12)
    evidence_ids: list[int] = Field(min_length=1, max_length=24)
    identity_basis: str = Field(min_length=1, max_length=500)
    interpretation: str = Field(max_length=700)
    alternatives: list[str] = Field(max_length=8)
    uncertainties: list[str] = Field(min_length=1, max_length=12)


class StudyWindow(BaseModel):
    events: list[StudyEvent] = Field(max_length=15)
    omitted_reason: str


def ground_window(raw: dict, lines) -> list[dict]:
    data = StudyWindow.model_validate(raw)
    if not data.events and not data.omitted_reason.strip():
        raise ValueError("空研究区间必须说明原因")
    lookup = {s.id: s for s in lines}
    events = []
    for index,event in enumerate(data.events):
        unknown=sorted({i for i in event.evidence_ids if i not in lookup})
        if unknown:
            raise ValueError(f"研究事件{index}引用了当前区间以外或不存在的字幕{unknown}；"
                             f"本区间可用编号为{sorted(lookup)}")
        evidence = [lookup[i] for i in sorted(set(event.evidence_ids), key=lambda i: lookup[i].start)]
        row = event.model_dump()
        row.update(start=min(s.start for s in evidence), end=max(s.end for s in evidence),
                   # Never let a model's confidence promote a name to verified.
                   identity_status="unverified", status="model_notes",
                   evidence=[s.model_dump() for s in evidence])
        events.append(row)
    return sorted(events, key=lambda e: e["start"])


def prepare_research(video, root, lines, seconds, cfg, llm, previous=None):
    from .director import compact
    from .continuity import load_continuity
    previous = previous if previous is not None else load_continuity(video, cfg, root)
    context = profile(cfg.series)
    inputs = {"version": VERSION, "source": source_identity(video), "series_profile": context,
              "transcript_hash": digest([s.model_dump() for s in lines]),
              "model": cfg.llm_model, "endpoint": cfg.llm_base_url}
    # Facts are extracted from this episode first. Prior context enters selection
    # and interpretation later, so old events cannot contaminate this ledger.
    signature = digest(inputs)
    cache = root / "research" / f"{signature}.json"
    if cache.exists():
        result = read_json(cache)
    else:
        windows = []
        for start in range(0, int(seconds)+1, 300):
            end = min(seconds, start+300)
            evidence = [s for s in lines if start <= s.start < end]
            if not evidence:
                continue
            prompt = (f"为《{cfg.series}》建立分集研究资料，本区间{start}–{end}秒。"
                "按剧情顺序拆为尽可能详细的场景事件，通常4–10项，遇片头片尾可为空并说明。"
                "event写谁提出什么、谁实际做什么、谁回应、结果如何；只写字幕能支持的情节，不能补造动作。"
                "participants填写暂定身份或准确泛称；identity_basis解释身份归属是否只是推断，儿臣不能识别具体皇子。"
                "interpretation只写带限定的因果分析；alternatives列其他可能解释；uncertainties必须保留身份/ASR/跨段证据局限。"
                "不要把对白提到的人当作说话人。未提供画面，不能声称看到了谁。"
                f"\n格式：{json.dumps(StudyWindow.model_json_schema(), ensure_ascii=False)}"
                + background_context(cfg.series) + "\n本区间ASR：\n" + compact(evidence))
            error = ""
            for attempt in range(cfg.repair_attempts+1):
                raw = llm.json("你是影视资料整理员。只处理提供的数据，逐项保留不确定性，不能把自己的推断标为已核实。", prompt+error)
                try:
                    events = ground_window(raw, evidence)
                    break
                except ValueError as ex:
                    write_json(root/'research-attempts'/signature/f'{start}-{attempt}.json',
                               {'response':raw,'error':str(ex),'status':'rejected'})
                    if attempt==cfg.repair_attempts:
                        raise
                    error = (f"\n资料修复第{attempt+1}/{cfg.repair_attempts}轮。上次输出校验失败，请重核后完整返回：" + str(ex)[:1200]
                             + "。每个事件evidence_ids须为1–24条当前区间真实编号，只选直接支持该事件的关键对白。"
                             "事件过大时拆成多个事件；不能连续枚举整个区间、伪造编号或用文字声称已修复。"
                             "仍须遵守events最多15项及各字段长度约束。"
                             + "\n上次未通过的数据：" + json.dumps(raw,ensure_ascii=False))
            windows.append({"start": start, "end": end, "events": events, "omitted_reason": raw["omitted_reason"]})
        result = {**inputs, "signature": signature, "status": "model_notes_not_fact_checked", "windows": windows}
        write_json(cache, result)
    write_json(root / "research.json", result)
    write_markdown(root / "剧情资料.md", video.stem, result)
    return result


def research_context(dossier, start=0, end=float("inf")):
    if not dossier:
        return {}
    return {"status": "待核对研究线索，不能作为事实或身份已核实证明", "signature": dossier["signature"],
            "events": [{k: v for k, v in event.items() if k != "evidence"}
                       for w in dossier["windows"] for event in w["events"]
                       if event["start"] < end and event["end"] > start]}


def write_markdown(path, episode, dossier):
    rows = [f"# 《{dossier['series_profile']['series']}》{episode}：带时间码的剧情资料", "",
            "**自动研究初稿，未通过人物画面核对。事件和推理均可能有误；证据链接有效不等于内容正确。**", "",
            f"原片：{dossier['source']['path']}", f"资料指纹：{dossier['signature']}", "",
            "人物资料与原片说话人分开：核心角色表仅帮助消歧。字幕中的儿臣、皇阿玛、臣等不能确定说话人。", ""]
    for w in dossier["windows"]:
        rows += [f"## {timestamp(w['start'])}–{timestamp(w['end'])}", ""]
        if not w["events"]:
            rows += [w["omitted_reason"], ""]
        for e in w["events"]:
            rows += [f"### {timestamp(e['start'])}–{timestamp(e['end'])}", "", e["event"], "",
                     "暂定人物：" + "、".join(e["participants"]), "身份依据（待核）：" + e["identity_basis"], "",
                     "分析假设：" + e["interpretation"], "其他解释：" + "；".join(e["alternatives"]),
                     "疑点：" + "；".join(e["uncertainties"]), "", "ASR索引（不是核实后的台词）：", ""]
            rows += [f"- [{s['id']}] {timestamp(s['start'])}–{timestamp(s['end'])} {s['text']}" for s in e["evidence"]]
            rows += [""]
    path.write_text("\n".join(rows), encoding="utf-8")
