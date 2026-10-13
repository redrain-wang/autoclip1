"""Independent subject/action audit; unresolved identity blocks new publication."""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .research import profile
from .util import digest, read_json, source_identity, write_json

VERSION = "identity-6"


def reference_board(anchors, images, root):
    from PIL import Image, ImageDraw
    views = [(i, a, p) for i,(a,p) in enumerate(zip(anchors, images))]
    views += [(i, ref, Path(ref['resolved_image'])) for i,a in enumerate(anchors) for ref in a.get('references', [])]
    views.sort(key=lambda row: row[0])
    canvas = Image.new("RGB", (800, 240*((len(views)+3)//4)), "#111111")
    draw = ImageDraw.Draw(canvas)
    for i, (reference_id, a, p) in enumerate(views):
        with Image.open(p) as original:
            crop = a.get("crop")
            if crop:
                if len(crop) != 4 or not 0 <= crop[0] < crop[2] <= original.width or not 0 <= crop[1] < crop[3] <= original.height:
                    raise ValueError("人物参考裁剪范围无效")
                original = original.crop(crop)
            original.thumbnail((195, 210))
            x, y = (i%4)*200, (i//4)*240
            canvas.paste(original, (x, y+25))
            draw.text((x+5, y+5), f"R{reference_id}", fill="white")
    target = root / "reference-board.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, quality=92)
    return target


def scene_identity(video, root, lines, start, end, cfg, llm, times=None):
    """Reusable source observations are independent of any draft or interpretation."""
    from .speakers import recognize_scene
    return recognize_scene(video, root, lines, start, end, cfg, llm, times)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_anchors(path, series):
    if path is None:
        return [], []
    path = Path(path)
    if not path.exists():
        raise ValueError("配置的人物锚点文件不存在；请恢复本地registry.json或设置AUTOCLIP_IDENTITY_REGISTRY，不能静默跳过")
    data = read_json(path)
    if data.get("series") != series:
        raise ValueError("人物锚点作品与当前作品不一致")
    records, images, seen = [], [], set()
    sources = {}
    for row in data.get("anchors", []):
        if row.get("status") != "reviewed":
            continue
        if not row.get("id") or row["id"] in seen or not row.get("basis") or not row.get("reviewer"):
            raise ValueError("人物锚点缺少唯一编号或身份核对依据")
        seen.add(row["id"])
        source = row["source"]
        video = Path(source["path"])
        if str(video) not in sources:
            sources[str(video)] = source_identity(video)
        if sources[str(video)] != source:
            raise ValueError("人物锚点原片版本已改变，须重新核对")
        image = (path.parent / row["image"]).resolve()
        if file_hash(image) != row["image_sha256"]:
            raise ValueError("人物锚点图片已改变，须重新核对")
        if not row.get("name") or not row.get("aliases"):
            raise ValueError("人物锚点缺少姓名或别名")
        additional = []
        for ref in row.get('references', []):
            if ref.get('status') != 'reviewed':
                continue
            if not ref.get('basis') or not ref.get('reviewer'):
                raise ValueError('补充人物参考缺少核对依据')
            ref_video = Path(ref['source']['path'])
            if str(ref_video) not in sources:
                sources[str(ref_video)] = source_identity(ref_video)
            if sources[str(ref_video)] != ref['source']:
                raise ValueError('补充人物参考原片版本已改变')
            ref_image = (path.parent/ref['image']).resolve()
            if file_hash(ref_image) != ref['image_sha256']:
                raise ValueError('补充人物参考图片已改变')
            additional.append({**ref, 'resolved_image':str(ref_image)})
        record = {**row, "image_number": len(images)+1}
        if additional:
            record['references'] = additional
        else:
            record.pop('references',None)
        records.append(record)
        images.append(image)
    return records, images


def units_for_clip(clip):
    units = [{"id": field, "text": getattr(clip, field)} for field in ("title", "angle", "hook")]
    units += [{"id": f"beat-{i}", "text": beat.text, "start": beat.start, "end": beat.end,
               "evidence_ids": beat.evidence_ids} for i, beat in enumerate(clip.beats) if beat.mode == "narration"]
    return units


class IdentityClaim(BaseModel):
    excerpt: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    action: str = Field(min_length=1)
    observed_subject: str = Field(min_length=1)
    role: Literal["speaker", "actor", "analysis", "mentioned", "addressee"] = "actor"
    verdict: Literal["supported", "contradicted", "unresolved"]
    basis: Literal["anchor_and_context", "explicit_address_response", "insufficient"]
    anchor_ids: list[str]
    evidence_ids: list[int]
    frame_ids: list[int]
    reason: str = Field(min_length=1)


class IdentityUnit(BaseModel):
    id: str
    claims: list[IdentityClaim]
    no_claim_reason: str = ""


class IdentityReview(BaseModel):
    units: list[IdentityUnit]


def assess_review(raw, units, lines, anchors, frame_times, series, scene=None):
    review = IdentityReview.model_validate(raw)
    expected = {u["id"]: u for u in units}
    received = [u.id for u in review.units]
    if len(received) != len(set(received)) or set(received) != set(expected):
        raise ValueError("人物核对必须逐项覆盖标题、角度、钩子和全部口播，不能漏项或重复")
    lookup = {s.id: s for s in lines}
    anchor_lookup = {a["id"]: a for a in anchors}
    aliases = {}
    for row in [*profile(series).get("characters", []), *anchors]:
        for alias in [row["name"], *row["aliases"]]:
            if alias in aliases and aliases[alias] != row["name"]:
                raise ValueError("人物表存在互相冲突的别名")
            aliases[alias] = row["name"]
    issues = []
    def canonical(name):
        match = re.fullmatch(r"(.+?)[（(]R(\d+)[）)]", name)
        if match:
            candidate, index = match.group(1), int(match.group(2))
            if index < len(anchors) and aliases.get(candidate, candidate) == anchors[index]["name"]:
                name = candidate
        return aliases.get(name, name)
    for unit in review.units:
        text = expected[unit.id]["text"]
        if not unit.claims:
            if not unit.no_claim_reason.strip() or any(a in text for a in aliases) or any(a in text for a in ("他", "她", "这位", "对方")):
                issues.append({"unit": unit.id, "message": "含人物称谓/代词或未解释，无主张结果不能放行"})
            continue
        for claim in unit.claims:
            problems = []
            if claim.excerpt not in text:
                problems.append("摘录不属于当前文案")
            if claim.verdict != "supported":
                problems.append(f"人物归属{claim.verdict}：{claim.reason}")
            subject = canonical(claim.subject)
            observed = canonical(claim.observed_subject)
            if subject != observed:
                problems.append(f"文案主语{claim.subject}与证据主语{claim.observed_subject}不一致")
            if not claim.evidence_ids or any(i not in lookup for i in claim.evidence_ids):
                problems.append("缺少有效原片字幕证据")
            if not claim.frame_ids or any(f < 0 or f >= len(frame_times) for f in claim.frame_ids):
                problems.append("未引用实际提供的场景帧")
            if claim.basis == "anchor_and_context":
                if not claim.anchor_ids or any(i not in anchor_lookup for i in claim.anchor_ids):
                    problems.append("未引用已核对的画面锚点")
                elif not any(anchor_lookup[i]["name"] == observed for i in claim.anchor_ids):
                    problems.append("锚点不是所声称的证据主语")
                elif scene is not None:
                    observed_ids = [i for i in claim.anchor_ids if anchor_lookup[i]["name"] == observed]
                    matching = [t for t in scene["turns"] if t["anchor_id"] in observed_ids
                                and set(t["evidence_ids"]) & set(claim.evidence_ids)]
                    texts = " ".join(lookup[i].text for i in claim.evidence_ids if i in lookup)
                    active_action = any(word in claim.action for word in
                                        ("主动", "提出", "举荐", "请辞", "收下", "交出", "指责", "宣称", "要求", "命令"))
                    # Some reviewers label a named recipient as actor. Only a
                    # narrow, passive address action can normalize that role;
                    # active actions still require the subject's own turn.
                    passive_address = bool(re.fullmatch(r"被(?:点名|询问|征求)(?:询问|征求)?(?:意见|意思)?", claim.action))
                    named = ((claim.role in {"mentioned", "addressee"} or passive_address) and not active_action and
                             any(a in texts for a, name in aliases.items() if name == observed))
                    if not matching and not named:
                        problems.append("独立场景核对不支持这些台词属于该主语，不能仅按文案猜测身份")
            elif claim.basis == "explicit_address_response":
                texts = " ".join(lookup[i].text for i in claim.evidence_ids if i in lookup)
                names = [a for a, name in aliases.items() if name == observed] or [observed]
                if len(set(claim.evidence_ids)) < 2 or not any(name in texts for name in names):
                    problems.append("没有点名及回应的证据，不能以儿臣等非唯一称呼认人")
                if scene is not None and anchors and claim.role not in {'mentioned','addressee'}:
                    observed_ids = {a['id'] for a in anchors if a['name'] == observed}
                    if not any(t['anchor_id'] in observed_ids and set(t['evidence_ids']) & set(claim.evidence_ids)
                               for t in scene['turns']):
                        problems.append('点名不能绕过独立发言核对：被问者不等于发出问话的人')
            else:
                problems.append("身份依据不足")
            if problems:
                issues.append({"unit": unit.id, "excerpt": claim.excerpt, "subject": claim.subject,
                               "observed_subject": claim.observed_subject, "message": "；".join(problems)})
    return {"passed": not issues, "issues": issues, "review": review.model_dump()}


def audit_identity(video, root, clip, cfg, llm, lines):
    from .director import compact
    anchors, reference_images = load_anchors(cfg.identity_registry, cfg.series)
    units = units_for_clip(clip)
    first, last = min(b.start for b in clip.beats), max(b.end for b in clip.beats)
    cited = {i for b in clip.beats for i in b.evidence_ids}
    relevant = [s for s in lines if s.id in cited or first-30 <= s.start <= last+20]
    # Observe utterances independently of the draft's edit points.
    scene = scene_identity(video, root, relevant, min(s.start for s in relevant), max(s.end for s in relevant), cfg,llm)
    times = scene["times"]
    sheets = [Path(p) for p in scene["sheets"]]
    reference_images = [Path(p) for p in scene["reference"]]
    inputs = {"version": VERSION, "source": source_identity(video), "clip": clip.model_dump(),
              "lines": [s.model_dump() for s in relevant], "anchors": anchors,
              "frames": [{"path": str(p.resolve()), "sha256": file_hash(p)} for p in sheets],
              "scene_identity": scene["signature"],
              "profile": profile(cfg.series).get("characters", []), "model": cfg.llm_model, "endpoint": cfg.llm_base_url}
    key = digest(inputs)
    path = root / "identity" / f"{key}.json"
    if path.exists():
        raw = read_json(path)["raw"]
    else:
        # The reviewer sees identity references, not the development correction
        # narrative: otherwise the known failure becomes a supplied answer.
        public_anchors = [{**{k: a[k] for k in ("id", "name", "aliases")}, "reference_label": f"R{i}"}
                          for i,a in enumerate(anchors)]
        prompt = ("这是独立人物/行为归属审核。不要因稿件写得肯定而相信其主语，也不要重写稿件。"
            "逐项覆盖所有输入unit，每个unit提取全部依赖人物身份的主张（含代词、标题提问、人物动机分析），分别核对。"
            "区分说话人、听话人、被提及者。画面可能是反应镜头；结合前后点名、回应与多帧动作，单张同期字幕不是说话人证据。"
            "必须报告文案subject与证据observed_subject，尽量用锚点标准姓名；不明则unresolved。泛称同样须核实行为对象，不能靠泛称绕过。"
            "role必须区分speaker实际发言者、actor行为者、analysis分析对象、mentioned被提及者、addressee被问者；不要要求被提及者本人说出了他人的台词。"
            "有已核对锚点时用anchor_and_context，引用anchor_ids、原片evidence_ids与场景frame_ids（F后数字）。"
            "没有锚点可用明确点名及连续回应explicit_address_response，至少两条证据；儿臣、臣等不足以定名。"
            "不要声称听过未提供音频。脸被遮挡、只有背影、对应关系不明时用insufficient。"
            "摘录excerpt必须逐字来自该unit；没有任何身份相关主张才允许claims为空并说明原因。"
            "合理分析不必逐字出现在对白中，但被分析者必须是正确的人；角色指责别人的动机不等于动机已成为明示事实。"
            f"\n前{len(reference_images)}张图片是带R编号的人物脸部参考图，其后为带F编号与原片秒数的场景帧。"
            f"\n人物参考：{json.dumps(public_anchors,ensure_ascii=False)}"
            f"\n不看文案先行核对的发言记录（有局限，矛盾时不得放行）：{json.dumps(scene['turns'],ensure_ascii=False)}"
            f"\n格式：{json.dumps(IdentityReview.model_json_schema(),ensure_ascii=False)}"
            f"\n待核单位：{json.dumps(units,ensure_ascii=False)}\n原片字幕：{compact(relevant)}")
        # Deliberately omit the known error and earlier reviewer decisions.
        raw = llm.json("你是独立影视人物归属核对员，只根据当前提供的原片证据和已核对参考图判断。", prompt, reference_images+sheets)
    try:
        verdict = assess_review(raw, units, relevant, anchors, times, cfg.series,scene)
    except ValueError as ex:
        verdict = {"passed": False, "issues": [{"message": str(ex)[:2000]}]}
    record = {"inputs": inputs, "raw": raw, **verdict}
    write_json(path, record)
    write_json(root / "identity-latest.json", {"report": str(path.resolve()), **verdict})
    return record


def enforce_identity(video, root, clip, cfg, llm, lines):
    if not cfg.identity_gate:
        return
    from .repair import RepairNeeded
    record = audit_identity(video, root, clip, cfg, llm, lines)
    if record["passed"] is not True:
        raise RepairNeeded({"stage": "identity_review", "clip": clip.model_dump(), "issues": [
            {"code": "semantic_review", "message": "人物归属独立核对：" + json.dumps(i,ensure_ascii=False)} for i in record["issues"]]})
