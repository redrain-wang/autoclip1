"""Long commentary must remain commentary after timing and factual repairs."""
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .budget import spoken_characters
from .util import digest, write_json
from .llm import review_json
from .selection import SELECTION_RULES, original_runs

NARRATIVE_POLICY = 'connected-commentary-2'
NARRATIVE_REVIEW_POLICY = 'selection-and-listening-5'
NARRATIVE_CRITERIA = {'continuity', 'analysis', 'non_repetition', 'naturalness', 'selection'}
NATURAL_ORAL_RULES = (
    '口播像一个熟悉剧情的人把事情讲给别人听，先说具体的人做了什么、卡在哪里，再解释关键交锋。'
    '不用论文或工作汇报的口气替人物贴标签；把“拆掉怀才不遇的叙事”“收回道义解释权”等抽象概括，'
    '改成谁拿什么理由说服谁、对方怎样回应、眼前谁承担后果。抽象词没有解释清楚时直接删去，不能只换近义词。'
    '每段不必固定按“复述动作、上升意义、总结一句”展开；普通请安、落座、走路不硬说掌握节奏或争夺主动权。'
    '旁白说完一个意思就往下讲，删除意思相同的段尾总结；“一个……一个……”“从……到……再到……”等整齐句式适量，不能连续套用。'
    '分析只放在真正影响人物选择或对话胜负的地方，指出具体依据，允许一句把问题讲透，不为了显得深刻反复解释。'
    '段落长短随内容变化；主要讲述可以连续展开，短转场保持简洁，不能为满足字数把已讲明的动作扩成空分析。'
    '次要人物的短转场不必强行附会主线；原声已交代的称呼和主从关系可以自然承接，不必重复介绍背景，更不能把“叫过去”补成尚未交代的办差目的。'
    '自然不等于堆网络梗、替角色喊话、编内心独白、乱用反问或故意口吃；仍用准确、平实的第三人称。'
)
DEPTH_RULES = (
    '剧情优先不是只复述动作或用两句过场话串原声。先按章节构成连贯讲述，再分配到镜头。'
    '主要冲突必须讲清处境、行动依据、可选择的做法与约束、眼前后果；不要求每个短转场分析。'
    '主要解说段通常90–170字，形成完整思路；需要换画面时相邻解说接着讲，不在每个镜头重新开头。'
    '关键原声用于展示决定、反驳或冲突，不连续保留大量问答再用一句话复述。'
    '分析从已展示的行为与明确说法推导，区分事实和解读：可以解释利益约束、话语作用和行动代价，'
    '不要求角色亲口说出旁白的每一步推理；但不得虚构动机、关系、结果或后集事实。'
    '避免空泛权谋金句、动作流水账和逐镜头报幕；深度来自可理解的推理链，不来自字数或长句本身。'
) + NATURAL_ORAL_RULES


def formal_long(cfg):
    return cfg.duration_profile == 'long' and (cfg.min_seconds or 0) >= 1080


def narration_minimum(slot, cfg, story_first):
    if formal_long(cfg):
        return max(5, round(slot['target_chars']*.75))
    return 5 if story_first else round(slot['target_chars']*.45)


def editorial_repair(issues, text):
    """Only a whole-script finding bound to this exact paragraph may condense it."""
    return bool(issues) and all(i.get('stage')=='narrative_review'
        and i.get('text_digest')==digest(text) for i in issues)


def require_paragraph_preserved(before, after, cfg, *, issues=()):
    """Local factual edits cannot silently turn developed commentary into a bridge.

    Timing compression has its own measured limits and final whole-script gate.
    This guard is for content edits only, and does not certify analytical quality.
    """
    if (formal_long(cfg) and not editorial_repair(issues,before)
            and spoken_characters(before)>=80 and spoken_characters(after)<80):
        raise ValueError('内容修复把主要解说段缩成了短过场：须保留有据的解释与衔接，'
                         '至少80个有效字；不能用复述或空话补足，改后仍须整稿质量核验')


def commentary_metrics(clip, timing=None):
    narration=[(i,b) for i,b in enumerate(clip.beats) if b.mode=='narration']
    chars=sum(spoken_characters(b.text) for _,b in narration)
    seconds=sum((timing['beats'][i]['output_seconds'] if timing else spoken_characters(b.text)/4.5) for i,b in narration)
    total=(sum(row['output_seconds'] for row in timing['beats']) if timing else
           seconds+sum(b.end-b.start for b in clip.beats if b.mode=='original'))
    return {'narration_chars':chars,'narration_seconds':round(seconds,3),
            'narration_ratio':round(seconds/total,4) if total else 0,
            'substantial_paragraphs':sum(spoken_characters(b.text)>=80 for _,b in narration),
            'narration_count':len(narration)}


def require_commentary(clip, cfg, timing=None):
    if timing is not None and 'beats' not in timing:
        if formal_long(cfg):raise ValueError('正式长片缺少逐段实测时长，不能核验解说占比')
        timing=None
    metrics=commentary_metrics(clip,timing)
    if formal_long(cfg) and (metrics['narration_chars']<1800 or metrics['narration_ratio']<.30
                            or metrics['substantial_paragraphs']<8):
        raise ValueError('长片解说退化为碎片过场，须重新安排章节解说与原声，不能靠扩写空话放行：'+str(metrics))
    return metrics


def commentary_balance_issues(clip, cfg, timing, lines, llm=None, root=None):
    """Return bounded expansion work after factual edits reduce whole-film depth.

    Keep the existing quantity thresholds. Prefer developed commentary with
    evidence and measured footage headroom; no original is converted or padded.
    Proposed text still needs local source verification and all final reviews.
    """
    if not formal_long(cfg) or not timing or 'beats' not in timing:return []
    metrics=commentary_metrics(clip,timing)
    missing_chars=max(0,1800-metrics['narration_chars'])
    missing_paragraphs=max(0,8-metrics['substantial_paragraphs'])
    total=sum(row['output_seconds'] for row in timing['beats'])
    missing_seconds=max(0,(.30*total-metrics['narration_seconds'])/.70) if metrics['narration_ratio']<.30 else 0
    missing_duration=max(0,(cfg.min_seconds or 0)-total)
    if not (missing_chars or missing_paragraphs or missing_seconds or missing_duration):return []
    remaining=max(missing_chars,math.ceil(max(missing_seconds,missing_duration)*4.5))
    lookup={s.id:s for s in lines}
    candidates=[]
    for index,beat in enumerate(clip.beats):
        if beat.mode!='narration':continue
        own=[lookup[i] for i in beat.evidence_ids if i in lookup and lookup[i].start<beat.end and lookup[i].end>beat.start]
        if len(own)<2:continue
        chars=spoken_characters(beat.text)
        row=timing['beats'][index]
        room=max(0,(beat.end-beat.start)*1.4-.35-row['output_seconds'])
        capacity=min(120,math.floor(room*4.5))
        if capacity<8:continue
        candidates.append((index,chars,capacity,len(own)))
    candidates.sort(key=lambda r:(not (missing_paragraphs and r[1]<80),r[1]<80,-r[2],-r[3]))
    rationales={}
    if candidates and llm is not None:
        from .editorial import audience_script
        offered={r[0]:r for r in candidates[:8]}
        choices=[{'beat':i,'current_chars':chars,'maximum_added_chars':capacity,
            'text':clip.beats[i].text,'editorial_note_not_fact':clip.beats[i].reason,
            'source_lines':[lookup[e].model_dump() for e in clip.beats[i].evidence_ids if e in lookup]}
            for i,chars,capacity,_ in offered.values()]
        prompt=('事实删改后全片的讲述分量不足。只选择1–3个值得补充有据解释的主要交锋段，不写稿、不改时间。'
            '选择尚未讲透的必要处境、行动约束、真实取舍或已发生后果；不能仅因有画面空间就挑普通待客、铺床、端水、走路、问候来凑字。'
            '优先把关键冲突说清楚；旧稿和内部编辑备注不是事实依据，ASR也可能有错字。选段理由只是后续编辑建议，不能当成新事实。'
            '每个beat必须来自offered，evidence_ids仅用该候选source_lines内的实际编号，不能引用后文。'
            '只返回JSON {"choices":[{"beat":整数编号,"reason":"还缺哪一点必要解释","evidence_ids":[实际编号]}]}。'
            +f'\n当前全片指标{json.dumps(metrics,ensure_ascii=False)}'
            +'\naudience='+json.dumps(audience_script(clip),ensure_ascii=False)
            +'\noffered='+json.dumps(choices,ensure_ascii=False))
        feedback=''
        for attempt in range(cfg.repair_attempts+1):
            raw=review_json(llm,'你是剧情解说编辑，只选择有真实讲述价值的展开位置，拒绝为下限凑字。',prompt+feedback)
            if root:write_json(root/'commentary-balance'/digest(clip.model_dump())/f'selection-{attempt}.json',raw)
            try:
                picked=raw.get('choices') if isinstance(raw,dict) else None
                if not isinstance(picked,list) or not 1<=len(picked)<=3:raise ValueError('须返回1–3个候选位置')
                ids=[]
                for choice in picked:
                    index=choice.get('beat')
                    if type(index) is not int or index not in offered or index in ids:raise ValueError('只能选择给定候选编号一次')
                    cited=choice.get('evidence_ids')
                    if (not isinstance(cited,list) or not cited or any(type(i) is not int for i in cited)
                            or not set(cited).issubset(clip.beats[index].evidence_ids)):raise ValueError('选段引用必须来自本候选')
                    reason=choice.get('reason')
                    if not isinstance(reason,str) or not reason.strip():raise ValueError('须说明还缺少的必要解释')
                    ids.append(index);rationales[index]=reason
                candidates=[offered[index] for index in ids]
                break
            except ValueError as exc:
                if attempt==cfg.repair_attempts:raise ValueError('讲述补充位置选择未通过：'+str(exc)) from exc
                feedback=f'\n第{attempt+1}轮位置选择无效：{exc}；只依据给定候选重选，不扩大事实范围。'
    issues=[]
    for index,chars,capacity,_ in candidates[:3]:
        increase=min(capacity,max(24,remaining,90-chars if missing_paragraphs and chars<80 else 0))
        if increase<=0:continue
        issues.append({'id':f'beat-{index}','stage':'commentary_balance','minimum_chars':chars+increase,
            'message':f'事实修订后全片解说尚差{missing_chars}个有效字、旁白约{missing_seconds:.1f}秒、{missing_paragraphs}个展开段；总时长尚差{missing_duration:.1f}秒。'
                f'本段已有{chars}个有效字，实测画面尚有空间；至少写到{chars+increase}个有效字。'
                '只补充当前原片证据支持、尚未讲清的必要处境、选择约束或因果；保留原有正确解释。'
                '不要扩写普通落座、走路等短转场，不重复原声，不编内心或后续结果，不用空话凑数。'
                '若本段证据不足以展开则不得编造；修改后仍须实配、逐段原片和整稿五项核验。'
                +'选段编辑建议（不是新事实）：'+rationales.get(index,'补充已有依据但尚未说清的必要解释')})
        remaining=max(0,remaining-increase)
        if chars<80<=chars+increase:missing_paragraphs=max(0,missing_paragraphs-1)
        if not remaining and not missing_paragraphs:break
    return issues


class NarrativeFinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    beat: int = Field(ge=0)
    quote: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    direction: str = Field(min_length=1)
    action: Literal['rewrite','drop','trim','narrate'] = 'rewrite'


class NarrativeCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    criterion: Literal['continuity','analysis','non_repetition','naturalness','selection']
    passed: StrictBool
    reason: str = Field(min_length=1)


class NarrativeReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    checks: list[NarrativeCheck]
    issues: list[NarrativeFinding]


def narrative_context(clip, lines):
    audience=[]
    source=[]
    for index,beat in enumerate(clip.beats):
        selected=[s for s in lines if s.start<beat.end and s.end>beat.start]
        audience.append({'beat':index,'mode':beat.mode,'source_start':beat.start,'source_end':beat.end,
            'text':beat.text if beat.mode=='narration' else ' '.join(s.text for s in selected)})
        if beat.mode=='narration':
            source.append({'beat':index,'dialogue':[{'id':s.id,'start':s.start,'end':s.end,'asr_text':s.text}
                for s in lines if s in selected or s.id in beat.evidence_ids]})
    return audience,source


SOURCE_TIMELINE_RULES = (
    'audience是观众实际听到的内容；source_context是旁白区间对应的原片对白，仅供核对事件顺序，'
    '不代表观众又听了一遍这些对白，不能把source_context本身当作旁白重复原声。'
    '旁白可以讲述其覆盖的原片事件，再接后面的原声；不是所有事件都必须先放原声才可解说。'
    '同类台词或命令可能多次出现，必须按原片时间分别识别，不能把第二次误当成唯一一次。'
    '不能凭记忆纠正原片顺序；方向只处理有依据的表达问题，不增加未提供的动机、人物关系或后文事实。'
)

LISTENING_RULES = (
    'audience只包含观众实际听到的文字：original.text是保留的原声，narration.text是配音。'
    '这份文字不包含全部视觉叙事；连续性检查针对影视解说，不能要求它脱离画面也成为完整广播剧。'
    '不能仅因原声转写没有报姓名、普通换场没有额外旁白，就判人物或场景断裂；先核对前后已交代的称呼与事件关系。'
    '真实的动作主体混淆、前后矛盾或理解主线所必需的信息缺失仍须修，不能为文本自足反复插入报幕和人物介绍。'
    '只按这份实际音轨检查重复，不能凭原剧记忆、画面上原有字幕或被配音覆盖的对白，声称观众已听过。'
    '旁白转述未保留原声的剧情不是重复播放；应检查讲述是否清楚、有没有必要分析，不要求每段都作分析。'
    '声称与原声重复时，必须在问题说明中指出audience里哪一段原声及其实际原句，不能拿旁白自己的素材依据充当原声。'
    '同类命令或相似话语可能多次发生，结合source_start/source_end区分，不凭记忆要求调换剧情次序；'
    '这里只编辑观众听感，事实和身份仍由独立原片核验负责。'
)


class EditorialPatchReview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    resolved: StrictBool
    reason: str = Field(min_length=1)
    remaining_quote: str


def verify_editorial_patch(clip, index, issues, lines, llm, root, *, retries=0):
    """A factually valid edit must also address its whole-script editorial finding."""
    audience,source=narrative_context(clip,lines)
    context=[r for r in audience if abs(r['beat']-index)<=3]
    prompt=(DEPTH_RULES+SOURCE_TIMELINE_RULES+
        '只核对本次target段是否解决给定整稿编辑问题，不重新认人、不宣布全片合格。'
        '若要求去重，换词缩写但仍逐句预告或复述相邻原声，仍未解决；保留必要一句提示可以。'
        '若要求衔接，必须实际交代切换关系；字数长短不能代替判断。'
        '编辑意见也可能误读原片顺序，须对照source_context；不能为迎合错误意见而重排真实事件。'
        '若问题前提已被source_context否定，而target按原片讲清了当前人物和事件，当前意见即可视为已解决；'
        '不要继续要求作者证明错误前提、提前交代后文人物关系或执行原修订方向。'
        '修订方向提到的人名、关系和因果也不是已核实事实；source_context不支持时，'
        '不能以“未照方向保留这些分析”为由要求作者补回。删去无据分析、只留必要的有据承接，'
        '仍可解决当前去重或衔接问题；全片分析分量另行检查，不强迫这一短段凑分析。'
        'resolved=false时remaining_quote必须逐字引用当前target仍有问题的原句；'
        'resolved=true时remaining_quote必须为空。只核当前修订，不复制已被删除的旧句。'
        '\n格式'+json.dumps(EditorialPatchReview.model_json_schema(),ensure_ascii=False)
        +'\ntarget='+json.dumps(audience[index],ensure_ascii=False)
        +'\neditorial_findings='+json.dumps(issues,ensure_ascii=False)
        +'\naudience='+json.dumps(context,ensure_ascii=False)
        +'\nsource_context='+json.dumps([r for r in source if r['beat']==index],ensure_ascii=False))
    feedback=''
    for attempt in range(retries+1):
        raw=review_json(llm,'你是局部整稿编辑，核对修订是否真正解决重复、衔接、分析或口播自然度问题。',prompt+feedback)
        try:
            result=EditorialPatchReview.model_validate(raw)
            if ((result.resolved and result.remaining_quote) or
                    (not result.resolved and (not result.remaining_quote or result.remaining_quote not in clip.beats[index].text))):
                raise ValueError('整稿修订核验须引用当前问题原句，不能沿用旧稿或虚构引用')
            break
        except ValueError as exc:
            write_json(root/f'editorial-protocol-{attempt}.json',{'error':str(exc),'raw':raw})
            if attempt==retries:raise
            feedback=f'\n第{attempt+1}次核验响应的格式或引用无效：{exc}。先修正核验响应，稿件不变。'
            feedback+='\n当前实际target全文：'+clip.beats[index].text+'\n上轮核验：'+json.dumps(raw,ensure_ascii=False)
            feedback+='\n真实编辑问题仍须resolved=false并逐字引用当前target；不能为了修格式宣布通过，也不要求作者重写已合规的候选。'
    write_json(root/'editorial-verification.json',{'policy':NARRATIVE_REVIEW_POLICY,
        'clip_digest':digest(clip.model_dump()),'beat':index,**result.model_dump()})
    if not result.resolved:
        raise ValueError('整稿修订仍未落实：'+result.reason+'；当前原句：'+result.remaining_quote)


def review_commentary(clip, lines, cfg, llm, root, timing=None):
    """Check the final listening experience, including all factual/timing repairs."""
    if not formal_long(cfg):
        return None
    rows,source=narrative_context(clip,lines)
    runs=original_runs(clip,timing)
    prompt=(DEPTH_RULES+SELECTION_RULES+LISTENING_RULES+'\n审核整片最终听感，不写稿，不重新认人，不替代原片事实核对。'
        '按continuity、analysis、non_repetition、naturalness、selection五个维度分别给结论，每项恰好一次。'
        'continuity检查主线是否连贯、相邻段是否真正推进，而非每两句重起话头。'
        'analysis检查主要冲突是否解释处境、行动依据、约束或代价，而非把动作复述包装成分析；'
        '允许简短转场，不要求每段凑齐所有分析要素，不强求角色未表露的内心或额外历史知识。'
        'non_repetition检查旁白之间、旁白与相邻原声之间是否长篇重复同一信息；必要提示与回顾可以保留。'
        'naturalness检查读出来是否像自然讲述：抽象术语代替具体解释、普通动作硬拔高、连续套用整齐句式、'
        '每段都补一句没有信息增量的总结，均须指出实际原句。不能因没有“字幕里说”等禁词就认定自然。'
        '这是表达检查，不判断作者是否使用AI，不给检测概率；不要求删掉所有分析，也不把必要的官名和事实术语当问题。'
        'selection检查原声是否值得保留及连续播放是否拖沓。original_runs按成片相邻原声累计秒数，'
        'review_focus=true的长段须重点判断每段是否持续推进，不能因单个镜头不足40秒就放过整段。'
        '短而无价值的过场也应指出；长而完整有力的交锋允许通过，不能仅因超过90秒判失败。'
        'restored_beats仅标记修稿后恢复原声，不是保留价值证明。没有可辨对白也不等于没有戏，不能凭转写缺失臆断。'
        '字数多、解说占比高不等于合格，空话填字、换词重复、逐镜头报幕仍需指出。'
        '只把必须修的问题列issues。旁白问题action=rewrite，逐字引用当前旁白；'
        '原声选材问题逐字引用audience中该段原声，action明确选drop、trim或narrate，说明删哪里、留什么及必要承接。'
        '每条原声意见只能编辑其beat；整组original_runs涉及多个片段时，须分别列出受影响beat，不能把整组删留要求挂到其中一段上。'
        'trim表示当前这一段内仍有值得保留的完整对白，须指出本段实际保留什么；需要保留的对白都在其他beat时，当前整段应删就用drop。'
        '不要让只含吃食、寒暄等待删内容的目标执行trim后仍留下这些内容，也不能要求用目标段外的对白或未发生的结果作本段转述。'
        '不能建议删除或改配音取代原声开场，开场只能在合法长度内trim；不要为时长或单纯插一句话建议改编原声。'
        'direction说明如何利用已展示内容改好，不直接给新稿或编造新事实。'
        '不能为补承接而假定未展示的地点变化、先后行动或结果；先纠正用词矛盾，必要衔接也必须有原片依据。'
        '主语或发言方不清楚时，要求依据原片澄清，不能凭当前含糊旁白猜定是哪一方，再强迫作者照猜测改写。'
        '不能仅凭“如果两人是同一个人”“官职似乎对不上”等猜测报连续性错误；'
        '前后出现不同官员、地名或相似场景，不等于同一人物或事件自相矛盾。'
        '只有实际听到的文字存在明确矛盾才列问题，待核实的人名官职事实交给独立原片核验。'
        '没有问题则issues为空；任一维度未通过，必须给出具体问题，不能只给泛泛评价。'
        '\n原声text为机器转写，仅用于理解节目结构；疑似同音字不在这里当作事实错误。'
        '\n格式'+json.dumps(NarrativeReview.model_json_schema(),ensure_ascii=False)
        +'\neditorial_goal='+json.dumps({'title':clip.title,'angle':clip.angle},ensure_ascii=False)
        +'\naudience='+json.dumps(rows,ensure_ascii=False)
        +'\noriginal_runs='+json.dumps(runs,ensure_ascii=False))
    folder=root/'narrative-review'/digest({'policy':NARRATIVE_POLICY,'review_policy':NARRATIVE_REVIEW_POLICY,
        'clip':clip.model_dump(),'rows':rows,'source':source})
    feedback=''
    for attempt in range(cfg.repair_attempts+1):
        print(f'整稿五项检查：第{attempt+1}/{cfg.repair_attempts+1}次核对',flush=True)
        raw=review_json(llm,'你是影视解说的整稿编辑，检查连贯性、分析质量和自然口播。',prompt+feedback)
        write_json(folder/f'attempt-{attempt}.json',raw)
        try:
            result=NarrativeReview.model_validate(raw)
            if (len(result.checks)!=len(NARRATIVE_CRITERIA) or {c.criterion for c in result.checks}
                    != NARRATIVE_CRITERIA):
                raise ValueError('整稿检查须精确覆盖五个维度，包括口播自然度和原声选材')
            if any(not c.passed for c in result.checks) and not result.issues:
                raise ValueError('未通过的整稿检查必须指出具体段落问题')
            invalid=[]
            for number,issue in enumerate(result.issues):
                if issue.beat>=len(rows):
                    invalid.append({'issue':number,'beat':issue.beat,'error':'段号不存在','allowed_beats':list(range(len(rows)))})
                    continue
                row=rows[issue.beat]
                allowed=['rewrite'] if row['mode']=='narration' else ['trim'] if issue.beat==0 else ['drop','trim','narrate']
                if issue.quote not in row['text'] or issue.action not in allowed:
                    invalid.append({'issue':number,'beat':issue.beat,'mode':row['mode'],
                        'error':'整稿问题必须引用当前实际音轨的原句与段号，并使用合法操作',
                        'submitted_quote':issue.quote,'actual_text':row['text'],
                        'submitted_action':issue.action,'allowed_actions':allowed})
            for index in {i.beat for i in result.issues}:
                if len({i.action for i in result.issues if i.beat==index})>1:
                    invalid.append({'beat':index,'error':'同一段落的选材操作不能相互冲突'})
            if invalid:raise ValueError('请同时修正所有整稿引用和操作问题：'+json.dumps(invalid,ensure_ascii=False))
            break
        except ValueError as exc:
            write_json(folder/f'protocol-{attempt}.json',{'error':str(exc),'raw':raw})
            if attempt==cfg.repair_attempts:raise
            feedback=f'\n第{attempt+1}轮格式或引用无效：'+str(exc)+'。重核实际稿件，不修改或放行存疑内容。'
            feedback+='\n上轮完整响应：'+json.dumps(raw,ensure_ascii=False)+'\n一次修正全部引用、段号与操作，保留仍然存在的编辑问题；不得为了修格式把问题清空。'
    report={'policy':NARRATIVE_POLICY,'review_policy':NARRATIVE_REVIEW_POLICY,'clip_digest':digest(clip.model_dump()),
            'passed':not result.issues and all(c.passed for c in result.checks),
            'original_runs':runs,**result.model_dump()}
    write_json(folder/'report.json',report)
    write_json(root/'narrative-review.json',report)
    return report


def require_commentary_review(report, clip):
    if (not isinstance(report,dict) or report.get('policy')!=NARRATIVE_POLICY
            or report.get('review_policy')!=NARRATIVE_REVIEW_POLICY
            or report.get('clip_digest')!=digest(clip.model_dump())
            or report.get('passed') is not True or report.get('issues')!=[]):
        raise ValueError('缺少当前完整稿件的连贯解说核定，不能交付')
    checks=NarrativeReview.model_validate({'checks':report.get('checks'),'issues':report['issues']}).checks
    if (len(checks)!=len(NARRATIVE_CRITERIA) or {c.criterion for c in checks}!=NARRATIVE_CRITERIA
            or not all(c.passed for c in checks)):
        raise ValueError('整稿连贯性、分析、重复、口播自然度与原声选材检查未完整通过')
