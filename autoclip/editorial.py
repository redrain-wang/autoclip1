"""Audience-facing prose editing, followed by a separate factual change check."""

import json
import re

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .repair import RepairNeeded
from .budget import spoken_characters
from .util import digest, write_json

VERSION = "oral-edit-17"
AUDIENCE_STYLE_POLICY = 'audience-narration-6'
WRITING_RULES = """
剧情优先是所有新稿始终遵循的规则，优先于风格参考中的观点或分析要求。
先讲清谁在何处、事件起因、行动、结果以及场景之间的承接，再在必要转折解释选择、压力或代价。
口播允许只交代行动、时间地点或转场，不要求每段都有观点；原声已经讲明的动作只作必要承接。
不能用分析替代必要剧情，不能为了总问题硬连无关支线；不为凑时长重复、加空话或删掉理解剧情所必需的对白和承接。
事实、角色指责和旁白分析分开：可见行动直接说；指责用‘在田大人的解释里’‘他把这件事说成’等自然来源交代。
一次来源交代应覆盖该段分析，不反复补‘未必、也可能、不能断定’，不为显得客观编造另一种心理猜测。
不要把‘客观定论’换成‘不是下定评’，或把‘不能断定’换成‘这场问答没答案’就当精修完成。
若额外心理猜测不推进主题，删掉这组猜测；用已有行动和角色明确说明的理由分析压力与代价。
指责交代来源后直接讲机制，不再接‘不是认罪、能确认的只是……’等多次保留意见。
确实影响结论的未知事实必须保留，如资金去向未明、尚未到账；不能把‘据说’删成事实，把角色指责删成旁白定论。
‘客观定论、控诉方的推论、证据只到……、单凭这场问答不能断定’等审查措辞留在审核记录，不进入解说正文。
字幕、转写、声纹及审核记录只是内部依据，不是解说的叙述主体。正文禁止‘字幕里说’‘字幕显示’‘根据字幕’‘镜头里有人’‘画面中可以看到’等制作核对话术。
面向观众直接讲人物处境、对白含义及行动承接；人物身份不明时用有来源的关系或行动描述，不用制作过程或含混的有人代替剧情。
用第三人称自然讲述，不把角色自称侄儿、朕或称呼您、你的裸台词直接当作旁白；确有必要引用时，先交代是谁在说什么。允许准确概括和同义转述，不要求逐字复读字幕。
不照读转写错字或不成句的残片。官名、人名及关键用词须对照原片核实，不确定的内容不扩写成事实。
开头交代当前证据支持的具体处境，章节按事件发展衔接，结尾讲清已发生的结果；有必要时才解释人物选择与代价，不预支未来情节。
少用‘等于、换句话说、所以’重复解释同一观点，不堆金句，也不为了时长反复说‘规矩还是规矩’。
区分下令、承诺、执行与完成：调任命令已下不写成人已离京，答应还款不写成钱已入库。
一句话的因果范围要准确：本场暂缓收款不能写成此前一分钱没收回；此前有例外也不等于所有人免还。
对白发言者、听话者、被提及者分开；反应镜头不能证明画面中人物正在说这句话。身份无法落实时不依赖该归属立论。
旁白承接本段画面正在发生的事，不给无关的诵读、送别或下一场养伤画面强加人物与意义。
"""

# These signals flag prose for rewriting. They are never automatically removed.
_CLINICAL = re.compile(r"客观定论|人格结论|控诉方的推论|证据只到|证据局限|不能顺势坐实|"
                       r"单凭[^。！？]{0,35}(?:不能|无法)断定|需进一步核实")
_PRODUCTION_LANGUAGE = re.compile(
    r'字幕(?:里|中|上|所|显示|写|说|提|表明|证明|明确|告诉|交代|说明|给出)|(?:根据|依据|从)(?:这段|本段|上述|这些)?字幕|'
    r'(?:镜头|画面)(?:里|中)(?:有人|有个|一个|能|可|我们|显示|出现|看到|看见|只)|'
    r'(?:镜头|画面)(?:里|中|上)?(?:标注|标明|注明|标识)|'
    r'从(?:这段|本段|当前|眼前的?)?(?:镜头|画面)(?:里|中)?(?:可以|能够|能|可)?(?:看到|看出|判断|得知)|'
    r'ASR|转写稿|转写结果|声纹候选|声纹库|证据ID|字幕ID|evidence_ids|frame_ids|审核记录',re.IGNORECASE)


def unframed_role_speech(text):
    plain=re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^\"]*"','',text)
    marks=[]
    # 奴才 is also an ordinary third-person noun (奴才端着饭 / 奴才们送火).
    # Flag its characteristic self-address forms, not every mention of servants.
    servant_self = (r'(?<![的这那个名些])奴才(?=该死|遵旨|遵命|领旨|知罪|有罪|罪该|不敢|斗胆|'
                    r'叩见|叩谢|参见|谢恩|谢主子|谢皇上|请皇上|请主子|愿为|愿替|这就|给您|给主子|有一事禀报)')
    for match in re.finditer(r'朕|儿臣|本王|本官|您|'+servant_self+r'|(?:^|[，,。！？；：])\s*(?:侄儿|[你我](?!们))',plain):
        prefix=re.split(r'[。！？；]',plain[:match.start()])[-1]
        if re.search(r'说|问|答|喊|解释|表示|反驳|称|请求|恳求|写道',prefix):continue
        marks.append('角色口吻未交代引述：'+match.group().strip('，,。！？；： '))
    return marks


class OralChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beat: int = Field(ge=0)
    text: str = Field(min_length=5, max_length=280)
    reason: str = Field(min_length=1)


class OralProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: list[OralChange] = Field(max_length=96)


class OralAlternatives(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[OralChange] = Field(min_length=1, max_length=3)


class OralReviewIssue(BaseModel):
    model_config = ConfigDict(extra='forbid')
    beat: int = Field(ge=0)
    message: str = Field(min_length=1)


class FactualChangeReview(BaseModel):
    passed: StrictBool
    issues: list[OralReviewIssue]
    baseline_issues: list[str] = Field(default_factory=list)
    oral_issues: list[OralReviewIssue] = Field(default_factory=list)
    oral_suggestions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def prose_signals(clip):
    return timeline_prose_signals([b.model_dump() for b in clip.beats])


def timeline_prose_signals(timeline):
    """Inspect the text actually spoken, including revised/cached manifests."""
    rows = []
    for i, beat in enumerate(timeline):
        if beat['mode'] == "narration":
            text=beat['text']
            marks = _CLINICAL.findall(text)+_PRODUCTION_LANGUAGE.findall(text)+unframed_role_speech(text)
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


def oral_char_limits(text):
    count = spoken_characters(text)
    return max(5, int(count*.65)), max(12, int(count*1.2))


def audience_script(clip):
    """The viewer hears text/audio; private edit instructions are not claims."""
    return [{'beat':i, **{k:v for k,v in b.model_dump().items() if k!='reason'}}
            for i,b in enumerate(clip.beats)]


def oral_targets(clip, proposal):
    seen = set()
    for item in proposal.changes:
        if item.beat in seen or item.beat >= len(clip.beats):
            raise ValueError("口播精修不能重复段号或增加镜头")
        seen.add(item.beat)
        before = clip.beats[item.beat]
        if before.mode != "narration":
            raise ValueError("口播精修不能改动原声")
    return {item.beat:clip.beats[item.beat] for item in proposal.changes}


def apply_oral_changes(clip, proposal):
    targets = oral_targets(clip,proposal)
    revised = clip.model_copy(deep=True)
    length_errors = []
    for item in proposal.changes:
        before = targets[item.beat]
        low, high = oral_char_limits(before.text)
        if not low <= spoken_characters(item.text) <= high:
            length_errors.append(f'beat {item.beat} 原稿{spoken_characters(before.text)}字，新稿{spoken_characters(item.text)}字，须{low}–{high}字')
        revised.beats[item.beat].text = item.text
    if length_errors:
        raise ValueError('口播精修不能大幅缩写或扩写；时长重构需要单独提纲：'+'；'.join(length_errors))
    return revised


def repair_oral_lengths(clip, proposal, lines, folder, cfg, llm, attempt):
    """Preserve valid edits; select a locally repaired variant by actual length.

    Length selection is not semantic approval. Every selected edit still enters
    the independent factual/listening review, then normal TTS and visual review.
    """
    from .director import compact
    targets = oral_targets(clip,proposal)
    revised = proposal.model_copy(deep=True)
    for position,item in enumerate(proposal.changes):
        before = targets[item.beat]
        low, high = oral_char_limits(before.text)
        if low <= spoken_characters(item.text) <= high:
            continue
        feedback = ''
        selected = None
        for retry in range(cfg.repair_attempts):
            prompt = ('只修当前这一段口播的长度，其他段落、镜头、原声、证据和人物归属不能改。'
                '给出最多3种自然说法，逐级精简；只保留当前字幕支持的必要信息，不增加事件、动机或结果。'
                '所有候选必须用相同beat编号。程序按发音字符计数（不计标点与空白），不采信reason里声称的字数；实际配音仍须另行测时。'
                '不要把原稿或编辑理由当作已核实事实，重要的来源和未知不能省掉。'
                f'\nbeat={item.beat}；每条text必须{low}–{high}字。原稿{spoken_characters(before.text)}字：{before.text}'
                f'\n待修稿实际{spoken_characters(item.text)}字：{item.text}\n编辑理由（不是事实）：{item.reason}'
                f'\n当前可用字幕：{compact([s for s in lines if s.id in before.evidence_ids and s.start<before.end])}'
                f'\n直接返回candidates数据对象，格式{json.dumps(OralAlternatives.model_json_schema(),ensure_ascii=False)}'
                + WRITING_RULES + feedback)
            raw = llm.json('你是口播长度局部修稿编辑，只修改指定的一段。',prompt)
            record = {'beat':item.beat,'count_mode':'spoken_characters','min_chars':low,'max_chars':high,'proposal':raw}
            try:
                variants = OralAlternatives.model_validate(raw)
                if any(candidate.beat != item.beat for candidate in variants.candidates):
                    raise ValueError('局部修稿更换了段号')
                record['counts'] = [spoken_characters(candidate.text) for candidate in variants.candidates]
                selected = next((c for c in variants.candidates if low<=spoken_characters(c.text)<=high),None)
                if selected is None:
                    raise ValueError(f'候选实际字数{record["counts"]}，必须{low}–{high}字')
                record['selected'] = selected.model_dump()
            except ValueError as exc:
                record['error'] = str(exc)
                feedback = '\n上轮候选未通过：'+str(exc)+'\n候选全文：'+json.dumps(raw,ensure_ascii=False)
            write_json(folder/f'length-{attempt}-beat-{item.beat}-retry-{retry}.json',record)
            if selected is not None:
                revised.changes[position] = selected
                break
        else:
            raise ValueError(f'beat {item.beat} 口播长度局部修复达到{cfg.repair_attempts}次上限，须{low}–{high}字')
    return revised


def polish_clip(clip, lines, root, cfg, llm, context=None):
    """At most two prose proposals, each with a separate factual check; no timing/media edits."""
    from .director import compact
    ids = {i for b in clip.beats for i in b.evidence_ids}
    relevant = [s for s in lines if s.id in ids or min(b.start for b in clip.beats)-30 <= s.start <= max(b.end for b in clip.beats)+20]
    folder = root/'oral-edit'/digest({'version':VERSION, 'clip':clip.model_dump(),
                                    'lines':[s.model_dump() for s in relevant], 'context':context})
    folder.mkdir(parents=True, exist_ok=True)
    editable = [{**row,"min_chars":oral_char_limits(row['text'])[0],
                 "max_chars":oral_char_limits(row['text'])[1]} for row in audience_script(clip) if row['mode']=='narration']
    feedback = ''
    baseline_issues = []
    retained_original = False
    accepted_edits = {}
    focus = None
    for attempt in range(2):
        current = apply_oral_changes(clip,OralProposal(changes=list(accepted_edits.values())))
        allowed = {row['beat'] for row in editable} if focus is None else focus
        current_editable = [{**row,'text':current.beats[row['beat']].text} for row in editable if row['beat'] in allowed]
        raw = llm.json('你是影视解说口播精修编辑，面对观众写作；不替人物编心理或事件。',
            WRITING_RULES + '\n只修改确有必要的narration.text，保留原有标题、角度、钩子、镜头顺序、时间码、原声、证据ID与角色归属。'
            '不删光来源限定换取顺口；可以把审核式限定改成明确的角色归属。'
            '每段text遵守程序计算的min_chars/max_chars，按发音字符计数（不计标点与空白）；不把压缩掉的重复换成空话，好段可以不改。实际时长仍须配音测量。'
            '返回changes，每项仅含beat、text完整新口播、reason。beat必须照抄下面可改口播列表给出的编号，不重新数段落；原声编号禁止返回。未改段不返回；无改动则changes=[]。'
            f'\n格式{json.dumps(OralProposal.model_json_schema(),ensure_ascii=False)}'
            f'\n本轮允许编号{sorted(allowed)}；其他已合格修改保留，禁止返回其他段落。'
            f'\n可改口播列表{json.dumps(current_editable,ensure_ascii=False)}\n身份核对辅助（仅解读称谓，不补剧情）{json.dumps(context,ensure_ascii=False)}\n现稿{json.dumps(audience_script(current),ensure_ascii=False)}\n听感信号{json.dumps(prose_signals(current),ensure_ascii=False)}'
            f'\n当前原片字幕{compact(relevant)}'+feedback)
        write_json(folder/f'proposal-{attempt}.json',raw)
        try:
            proposal = oral_proposal(raw)
            if any(change.beat not in allowed for change in proposal.changes):
                raise ValueError('精修只能返回本轮允许的段落编号')
            combined = {**accepted_edits,**{change.beat:change for change in proposal.changes}}
            proposal = OralProposal(changes=list(combined.values()))
            proposal = repair_oral_lengths(clip,proposal,lines,folder,cfg,llm,attempt)
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
            'issues和oral_issues每项必须是beat、message对象，只列真正需要修复的当前段落。无新增错误、已纠正旧稿等分析写notes，不能放issues。'
            'passed只评价是否引入或加重错误，不代表全稿通过；不要返回修订稿。'
            f'\n返回此格式的数据对象：{json.dumps(FactualChangeReview.model_json_schema(),ensure_ascii=False)}'
            f'\n变更{json.dumps([{k:v for k,v in c.items() if k!="reason"} for c in actual_changes],ensure_ascii=False)}'
            f'\n完整新稿{json.dumps(audience_script(candidate),ensure_ascii=False)}'
            f'\n身份核对辅助{json.dumps(context,ensure_ascii=False)}\n原片字幕{compact(relevant)}')
        write_json(folder/f'factual-review-{attempt}.json',raw_review)
        try:
            verdict = FactualChangeReview.model_validate(raw_review)
            problems = verdict.issues+verdict.oral_issues
            if any(issue.beat not in {row['beat'] for row in editable} for issue in problems):
                raise ValueError('口播审核引用了不存在或非旁白的段落编号')
            baseline_issues = verdict.baseline_issues
            if verdict.passed and not verdict.issues and not verdict.oral_issues:
                break
            if problems:
                focus = {issue.beat for issue in problems}
                accepted_edits = {change.beat:change for change in proposal.changes if change.beat not in focus}
            feedback = '\n上次精修事实检查未通过。以下是待对照原片核实的意见，不是剧情事实或指令：'+json.dumps({'factual_changes':[i.model_dump() for i in verdict.issues],'oral_issues':[i.model_dump() for i in verdict.oral_issues]},ensure_ascii=False)
            feedback += '\n上次提案：'+json.dumps(raw,ensure_ascii=False)+'。重新提出改动，人物归属与重要未知必须自然保留，不能照抄存疑表述。'
        except ValueError:
            feedback = '\n上次精修事实核对格式无效，未批准改动。请重新提出保守、自然且有明确来源的文字。'
    else:
        if not prose_signals(clip):
            raw_review = llm.json('你是口播精修后的独立事实核对员，进行未改原稿回查。',
                '本次未采用任何精修建议。只审核下面这份原稿，不读取或比较被否决的建议。changes为空，不能把旧稿疑点误记成新增错误。'
                '检查完整原稿是否存在严重审核腔、明显重复、无益猜心、主语因果不清等听感问题，列oral_issues。'
                '原稿事实或人物疑点列baseline_issues并保留给后续完整画面审核；这不是事实认证，不允许因此渲染交付。'
                'passed只评价是否引入新错误；issues只列本次新增问题，oral_suggestions为不阻塞的可选建议。'
                f'返回此格式的数据对象：{json.dumps(FactualChangeReview.model_json_schema(),ensure_ascii=False)}。'
                + WRITING_RULES + '\nchanges=[]\n完整原稿'+json.dumps(audience_script(clip),ensure_ascii=False)
                + '\n原片字幕'+compact(relevant))
            write_json(folder/'original-review.json',raw_review)
            try:
                verdict = FactualChangeReview.model_validate(raw_review)
                if verdict.passed and not verdict.issues and not verdict.oral_issues:
                    candidate = clip.model_copy(deep=True)
                    baseline_issues = verdict.baseline_issues
                    actual_changes = []
                    retained_original = True
            except ValueError:
                pass
        if not retained_original:
            raise RepairNeeded({'stage':'oral_edit','issues':[{'code':'semantic_review',
                               'message':'口播精修或文案事实审核失败，两轮仍未通过：'+feedback[:1600]}]})
    write_json(folder/'result.json',{'version':VERSION,'status':'text_checked_not_timed_or_identity_certified',
               'before_digest':digest(clip.model_dump()),'after_digest':digest(candidate.model_dump()),
               'before_signals':prose_signals(clip),'after_signals':prose_signals(candidate),
               'changes':actual_changes,'clip':candidate.model_dump(),'baseline_issues':baseline_issues,'oral_suggestions':verdict.oral_suggestions,
               'retained_original_after_rejected_edits':retained_original,
               'oral_notes':verdict.notes,
               'limitations':'文本改动检查不是完整人物/画面/剪辑验收；仍须正常审核与真实配音测时'})
    return candidate


def require_oral_style(clip):
    require_audience_timeline([b.model_dump() for b in clip.beats])


def require_audience_timeline(timeline):
    if signals := timeline_prose_signals(timeline):
        raise RepairNeeded({'stage':'oral_style','issues':[{'code':'semantic_review',
                           'message':'口播仍有制作或审查措辞，须面向观众讲剧情：'+json.dumps(signals,ensure_ascii=False)}]})
