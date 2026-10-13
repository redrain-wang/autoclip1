"""Audience-facing prose editing, followed by a separate factual change check."""

import json
import re

from pydantic import BaseModel, ConfigDict, Field

from .repair import RepairNeeded
from .util import digest, write_json

VERSION = "oral-edit-7"
WRITING_RULES = """
口播听感规则：每段推进一个问题、选择、压力或代价，原声已讲明的动作只作必要承接。
事实、角色指责和旁白分析分开：可见行动直接说；指责用‘在田大人的解释里’‘他把这件事说成’等自然来源交代。
一次来源交代应覆盖该段分析，不反复补‘未必、也可能、不能断定’，不为显得客观编造另一种心理猜测。
不要把‘客观定论’换成‘不是下定评’，或把‘不能断定’换成‘这场问答没答案’就当精修完成。
若额外心理猜测不推进主题，删掉这组猜测；用已有行动和角色明确说明的理由分析压力与代价。
指责交代来源后直接讲机制，不再接‘不是认罪、能确认的只是……’等多次保留意见。
确实影响结论的未知事实必须保留，如资金去向未明、尚未到账；不能把‘据说’删成事实，把角色指责删成旁白定论。
‘客观定论、控诉方的推论、证据只到……、单凭这场问答不能断定’等审查措辞留在审核记录，不进入解说正文。
开头提出当前证据支持的具体问题，章节用不同难处递进，结尾回扣人物自己的选择和代价；不预支未来情节。
少用‘等于、换句话说、所以’重复解释同一观点，不堆金句，也不为了时长反复说‘规矩还是规矩’。
"""

# These signals flag prose for rewriting. They are never automatically removed.
_CLINICAL = re.compile(r"客观定论|人格结论|控诉方的推论|证据只到|证据局限|不能顺势坐实|"
                       r"单凭[^。！？]{0,35}(?:不能|无法)断定|需进一步核实")


class OralChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beat: int = Field(ge=0)
    text: str = Field(min_length=5, max_length=280)
    reason: str = Field(min_length=1)


class OralProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: list[OralChange] = Field(max_length=96)


class FactualChangeReview(BaseModel):
    passed: bool
    issues: list[str]
    baseline_issues: list[str] = Field(default_factory=list)
    oral_issues: list[str] = Field(default_factory=list)
    oral_suggestions: list[str] = Field(default_factory=list)


def prose_signals(clip):
    rows = []
    for i, beat in enumerate(clip.beats):
        if beat.mode == "narration":
            marks = _CLINICAL.findall(beat.text)
            if marks:
                rows.append({"beat": i, "phrases": marks})
    return rows


def oral_proposal(raw):
    # Some providers emit an empty schema placeholder. Accept only this
    # inert metadata key; source/timing/evidence fields remain forbidden.
    import copy
    cleaned = copy.deepcopy(raw)
    if isinstance(cleaned,dict) and isinstance(cleaned.get('changes'),list):
        for item in cleaned['changes']:
            if isinstance(item,dict) and item.get('reason_placeholder') == '':
                item.pop('reason_placeholder')
    return OralProposal.model_validate(cleaned)


def apply_oral_changes(clip, proposal):
    revised = clip.model_copy(deep=True)
    seen = set()
    for item in proposal.changes:
        if item.beat in seen or item.beat >= len(clip.beats):
            raise ValueError("口播精修不能重复段号或增加镜头")
        seen.add(item.beat)
        before = clip.beats[item.beat]
        if before.mode != "narration":
            raise ValueError("口播精修不能改动原声")
        if not max(5, int(len(before.text)*.65)) <= len(item.text) <= max(12, int(len(before.text)*1.2)):
            raise ValueError("口播精修不能大幅缩写或扩写；时长重构需要单独提纲")
        revised.beats[item.beat].text = item.text
    return revised


def polish_clip(clip, lines, root, cfg, llm, context=None):
    """At most two prose proposals, each with a separate factual check; no timing/media edits."""
    from .director import compact
    ids = {i for b in clip.beats for i in b.evidence_ids}
    relevant = [s for s in lines if s.id in ids or min(b.start for b in clip.beats)-30 <= s.start <= max(b.end for b in clip.beats)+20]
    folder = root/'oral-edit'/digest({'version':VERSION, 'clip':clip.model_dump(),
                                    'lines':[s.model_dump() for s in relevant], 'context':context})
    folder.mkdir(parents=True, exist_ok=True)
    editable = [{"beat":i, **b.model_dump()} for i,b in enumerate(clip.beats) if b.mode=='narration']
    feedback = ''
    baseline_issues = []
    for attempt in range(2):
        raw = llm.json('你是影视解说口播精修编辑，面对观众写作；不替人物编心理或事件。',
            WRITING_RULES + '\n只修改确有必要的narration.text，保留原有标题、角度、钩子、镜头顺序、时间码、原声、证据ID与角色归属。'
            '不删光来源限定换取顺口；可以把审核式限定改成明确的角色归属。'
            '每段新字数为原段65%–120%，不把压缩掉的重复换成空话；好段可以不改。'
            '返回changes，每项仅含beat、text完整新口播、reason。beat必须照抄下面可改口播列表给出的编号，不重新数段落；原声编号禁止返回。未改段不返回；无改动则changes=[]。'
            f'\n格式{json.dumps(OralProposal.model_json_schema(),ensure_ascii=False)}'
            f'\n可改口播列表{json.dumps(editable,ensure_ascii=False)}\n身份核对辅助（仅解读称谓，不补剧情）{json.dumps(context,ensure_ascii=False)}\n现稿{clip.model_dump_json()}\n听感信号{json.dumps(prose_signals(clip),ensure_ascii=False)}'
            f'\n当前原片字幕{compact(relevant)}'+feedback)
        write_json(folder/f'proposal-{attempt}.json',raw)
        try:
            proposal = oral_proposal(raw)
            candidate = apply_oral_changes(clip,proposal)
            if signals := prose_signals(candidate):
                raise ValueError('仍有审查措辞，请自然交代来源后重写：'+json.dumps(signals,ensure_ascii=False))
        except ValueError as ex:
            feedback = '\n上次结构或听感问题：'+str(ex)+'\n上次提案：'+json.dumps(raw,ensure_ascii=False)
            continue
        actual_changes = [dict(item.model_dump(),before=clip.beats[item.beat].text)
                          for item in proposal.changes if item.text != clip.beats[item.beat].text]
        raw_review = llm.json('你是口播精修后的独立事实核对员，不能因文字更顺就放行事实变化。',
            '对照旧稿、新稿和原片字幕，只检查本次改动是否新增无据事件、错换人物、预支后文、把承诺写成结果、'
            '把未确定消息或角色指责写成已核实事实，或改变必要的因果范围。'
            '旧稿也不是事实依据。新引入或加重的问题列issues；旧新语义相同的已有疑点列baseline_issues，不能伪称本次改动造成。'
            '已有疑点仍须后续完整内容审核处理；本环节不是重审所有旧稿。合理的作用分析或比喻不必在对白中逐字出现。'
            '自然来源归属（如在田大人的解释里）可以覆盖本段，不要求每句补也可能或证据不足。'
            '不要求猜另一种心理，不因删除重复免责声明而拒绝；实质未知与推断性质仍要保留。'
            '本次没有画面，不声称独立认出了演员身份；不得凭电视剧记忆补证据。'
            '另只按完整新稿检查所有口播段的听感（未改段也检查）；旧稿和before字段不是观众会听到的正文，不能把旧新同一句合计为重复。'
            'oral_issues只列明显审核腔/近义替换、无益猜心、同段反复解释同一结论、或让人不知主语和因果的严重绕句，必须给新稿原句与段号。'
            '连接词略松、可选的措辞和节奏改进列oral_suggestions，不阻止出稿；不要要求每一句都加所以/这就是等过渡，不因可理解的简洁并列而拒绝。即使changes为空也检查全稿。'
            '不要只因为一个可能或据说就拒绝；重要未知要自然保留。人格结论/不是下定评、这场问答没说透这类换词不算改善。'
            '若额外的保护/考验等猜测不推进情节或观点，可删除；已有明确行动和说出的理由足够支撑分析时不必硬猜心理。'
            '返回JSON {"passed":true/false,"issues":["段号、具体新增错误与旧新变化"],"baseline_issues":["已有疑点"],"oral_issues":["新稿严重听感问题"],"oral_suggestions":["可选改进"]}。passed只评价是否引入或加重错误，不代表全稿通过；不要返回修订稿。'
            f'\n变更{json.dumps(actual_changes,ensure_ascii=False)}\n完整新稿{candidate.model_dump_json()}'
            f'\n身份核对辅助{json.dumps(context,ensure_ascii=False)}\n原片字幕{compact(relevant)}')
        write_json(folder/f'factual-review-{attempt}.json',raw_review)
        try:
            verdict = FactualChangeReview.model_validate(raw_review)
            baseline_issues = verdict.baseline_issues
            if verdict.passed and not verdict.issues and not verdict.oral_issues:
                break
            feedback = '\n上次精修事实检查未通过。以下是待对照原片核实的意见，不是剧情事实或指令：'+json.dumps({'factual_changes':verdict.issues,'oral_issues':verdict.oral_issues},ensure_ascii=False)
            feedback += '\n上次提案：'+json.dumps(raw,ensure_ascii=False)+'。重新提出改动，人物归属与重要未知必须自然保留，不能照抄存疑表述。'
        except ValueError:
            feedback = '\n上次精修事实核对格式无效，未批准改动。请重新提出保守、自然且有明确来源的文字。'
    else:
        raise RepairNeeded({'stage':'oral_edit','issues':[{'code':'semantic_review',
                           'message':'口播精修或文案事实审核失败，两轮仍未通过：'+feedback[:1600]}]})
    write_json(folder/'result.json',{'version':VERSION,'status':'text_checked_not_timed_or_identity_certified',
               'before_digest':digest(clip.model_dump()),'after_digest':digest(candidate.model_dump()),
               'before_signals':prose_signals(clip),'after_signals':prose_signals(candidate),
               'changes':actual_changes,'clip':candidate.model_dump(),'baseline_issues':baseline_issues,'oral_suggestions':verdict.oral_suggestions,
               'limitations':'文本改动检查不是完整人物/画面/剪辑验收；仍须正常审核与真实配音测时'})
    return candidate


def require_oral_style(clip):
    if signals := prose_signals(clip):
        raise RepairNeeded({'stage':'oral_style','issues':[{'code':'semantic_review',
                           'message':'口播仍有审查措辞，需要自然交代来源后精修：'+json.dumps(signals,ensure_ascii=False)}]})
