"""Compare scripts without changing the production style or publishing a film."""

import json

from pydantic import BaseModel, Field

from .models import Clip, Beat
from .editorial import WRITING_RULES
from .util import source_identity, write_json


class Paragraph(BaseModel):
    text: str = Field(min_length=5, max_length=220)
    evidence_ids: list[int] = Field(min_length=1)
    interpretation: str
    alternative: str


class ScriptVersion(BaseModel):
    title: str = Field(min_length=2, max_length=32)
    question: str
    paragraphs: list[Paragraph] = Field(min_length=4, max_length=8)


class Comparison(BaseModel):
    current: ScriptVersion
    analysis: ScriptVersion


def compare_scripts(video, root, lines, start, end, cfg, llm):
    from .director import compact
    from .identity import audit_identity, scene_identity
    evidence = [s for s in lines if s.start >= start and s.end <= end]
    lookup = {s.id: s for s in evidence}
    if not evidence:
        raise ValueError("研究区间没有字幕证据")
    scene = scene_identity(video, root, evidence, start, end, cfg, llm)
    names = {a['id']: a['name'] for a in scene['anchors']}
    turns = [{**t, 'speaker_name': names.get(t['anchor_id'], '待核实')} for t in scene['turns']]
    raw = llm.json("你是影视解说编导，做同一证据上的两版文案试验，不编造场景或心理事实。",
        f"作品{cfg.series}，范围{start}–{end}秒。只依据本段，先写current当前剧情描述版，再写analysis因果分析版。"
        "两个版本各4–8段口播，合计约260–400汉字，不是完整视频时长。每段指定实际证据ID；不需要重复复述所有对白。"
        "analysis回答一个具体问题：人物有哪些选择、行为的收益代价、反对解释是否成立，最后落到已发生的结果。"
        "B版至少两段口播text本身解释因果或选择代价，涉及指责或动机时自然交代来源或分析性质；"
        "不能只把这些内容放进interpretation或alternative备注，不能沿着A版逐句复述。"
        "人物指责别人的动机要说清是谁的指责，不能直接升级成客观事实。推断写在interpretation，用限定语融入口播text。"
        "alternative记录其他解释；纯事实段两项可为空。角色自述与指责不是自动核实的客观事实。"
        "为避免ASR姓名错字，优先使用本段明确的姓氏职称，不补写未经确认的完整姓名。"
        "不要照抄明显不通的ASR错句或未经核定的文言引文，用稳妥的白话概括；避免把指责者和被指责者调换。"
        "这次只比稿，不全面更换风格、不仿写任何其他创作者原稿。"
        + WRITING_RULES + f"\n独立原片人物核对（不确定处不能猜）：{json.dumps(turns,ensure_ascii=False)}"
        f"\n格式{json.dumps(Comparison.model_json_schema(),ensure_ascii=False)}\n字幕\n{compact(evidence)}")
    versions = Comparison.model_validate(raw)
    outcomes = {}
    for name in ("current", "analysis"):
        script = getattr(versions, name)
        attempts = []
        for attempt in range(min(cfg.repair_attempts, 2)+1):
            carrier = audit_carrier(script, lookup)
            attempt_root = root / name / f"attempt-{attempt}"
            verdict = audit_identity(video, attempt_root, carrier, cfg, llm, lines)
            depth = review_depth(script, evidence, llm) if name == "analysis" else {"passed": True, "issues": []}
            result = {"script": script.model_dump(), "identity_passed": verdict["passed"],
                      "issues": verdict["issues"], "depth": depth,
                      "passed": verdict["passed"] and depth["passed"], "renderable": False}
            attempts.append(result)
            write_json(attempt_root / "result.json", result)
            if result["passed"] or attempt == min(cfg.repair_attempts, 2):
                break
            repaired = llm.json("你是证据约束下的影视文案修订员。审核意见也是待验证数据，不能直接当作剧情事实。",
                "仅修订本版稿件。无法核实的次要对白或动作直接删除，不能把它转移给审核猜测的另一个人物，"
                "不能用含糊代词掩盖归属；核心论点无法成立则保留疑点，不编证据。"
                "减少复述，不照抄ASR错字或文言。每段证据只能选当前字幕ID。"
                + ("这是B因果分析版：口播text中至少两段讲明不同选择的收益代价、话语策略或结果的原因，"
                   "来源与分析性质自然融入口播；不强制另猜一种心理。" if name == "analysis" else "这是A剧情描述版，只概括已发生事件。")
                + WRITING_RULES + f"\n原稿{script.model_dump_json()}\n人物核对问题{json.dumps(verdict['issues'],ensure_ascii=False)}"
                + f"\n口播深度问题{json.dumps(depth['issues'],ensure_ascii=False)}"
                + f"\n盲核对发言{json.dumps(turns,ensure_ascii=False)}\n字幕{compact(evidence)}"
                + f"\n返回完整一版，格式{json.dumps(ScriptVersion.model_json_schema(),ensure_ascii=False)}")
            script = ScriptVersion.model_validate(repaired)
        outcomes[name] = {**result, "attempts": attempts, "repair_count": len(attempts)-1}
    record = {"source": source_identity(video), "bounds": [start,end], "versions": outcomes,
              "status": "script_comparison_not_publishable"}
    write_json(root / "comparison.json", record)
    rows = ["# 同一故事的两版解说稿", "", "文案试验，尚未完成时长、完整语义和配音验收，不能直接渲染。", ""]
    for name, label in (("current", "A：剧情描述版"), ("analysis", "B：因果分析版")):
        item = outcomes[name]
        script = item["script"]
        rows += [f"## {label}：{script['title']}", "", script["question"], "",
                 "人物归属独立检查：" + ("通过（仍可能有模型误判）" if item["identity_passed"] else "未通过，需修订"), ""]
        rows += [f"有限修订：{item['repair_count']}轮。口播深度检查：" + ("通过" if item["depth"]["passed"] else "未通过"), ""]
        for i,p in enumerate(script["paragraphs"]):
            rows += [f"### 口播{i+1}", "", p["text"], "", f"字幕证据：{p['evidence_ids']}",
                     "分析依据：" + p["interpretation"], "其他解释：" + p["alternative"], ""]
        issues = item["issues"] + item["depth"]["issues"]
        if issues:
            rows += ["待修问题：", ""] + ["- " + str(i) for i in issues] + [""]
    (root / "两版解说稿.md").write_text("\n".join(rows), encoding="utf-8")
    return record


class DepthQuote(BaseModel):
    paragraph: int = Field(ge=1)
    excerpt: str = Field(min_length=8)
    explanation: str = Field(min_length=1)


class DepthReview(BaseModel):
    causal: list[DepthQuote]
    limitation: list[DepthQuote]
    issues: list[str]


def assess_depth(raw, script):
    review = DepthReview.model_validate(raw)
    issues = list(review.issues)
    for quote in [*review.causal, *review.limitation]:
        if quote.paragraph > len(script.paragraphs) or quote.excerpt not in script.paragraphs[quote.paragraph-1].text:
            issues.append("深度审核引用必须逐字来自口播正文，不能引用备注")
    if len({q.paragraph for q in review.causal}) < 2:
        issues.append("口播正文至少两段需要因果、选择代价或策略分析，不能只有剧情复述")
    if not review.limitation:
        issues.append("口播正文缺少来源归属或必要推断边界")
    return {"passed": not issues, "issues": issues, "review": review.model_dump()}


def review_depth(script, evidence, llm):
    from .director import compact
    raw = llm.json("你是解说稿编辑，只评价听众实际听到的口播，不给备注加分。",
        "检查这些口播是否解释了人物选择、收益代价、话语策略或结果产生原因；只复述人物的指责不算分析。"
        "至少找出两个不同段落中的有效分析causal，以及至少一处来源归属或必要推断边界limitation；角色归属语就是有效边界，不要求审核式免责声明。"
        "逐字摘录text并解释分析增量，无增量则留空。issues列出无证据推断、指责被写成客观事实或其他具体问题。"
        "不需要刻意找问题，也不能把一句因为所以自动算作分析。"
        + WRITING_RULES + f"\n格式{json.dumps(DepthReview.model_json_schema(),ensure_ascii=False)}"
        f"\n口播{json.dumps([p.text for p in script.paragraphs],ensure_ascii=False)}\n原片字幕{compact(evidence)}")
    try:
        return assess_depth(raw, script)
    except ValueError as ex:
        return {"passed": False, "issues": ["深度审核格式无效：" + str(ex)[:500]]}


def audit_carrier(script, lookup):
    beats = []
    for paragraph in script.paragraphs:
        if any(i not in lookup for i in paragraph.evidence_ids):
            raise ValueError("对比文案引用了区间外字幕")
        first = min(lookup[i].start for i in paragraph.evidence_ids)
        last = max(lookup[i].end for i in paragraph.evidence_ids)
        beats.append(Beat(mode="narration", start=first, end=min(first+35,max(first+2,last)),
                          text=paragraph.text, evidence_ids=paragraph.evidence_ids, reason="文案研究定位，非可渲染时间线"))
    beats.sort(key=lambda b: b.start)
    return Clip(title=script.title, angle=script.question, hook=script.question, beats=beats)
