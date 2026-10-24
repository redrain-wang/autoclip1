"""Long commentary must remain commentary after timing and factual repairs."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .budget import spoken_characters
from .util import digest, write_json
from .llm import review_json

NARRATIVE_POLICY = 'connected-commentary-2'
NARRATIVE_REVIEW_POLICY = 'natural-oral-3'
NARRATIVE_CRITERIA = {'continuity', 'analysis', 'non_repetition', 'naturalness'}
NATURAL_ORAL_RULES = (
    '口播像一个熟悉剧情的人把事情讲给别人听，先说具体的人做了什么、卡在哪里，再解释关键交锋。'
    '不用论文或工作汇报的口气替人物贴标签；把“拆掉怀才不遇的叙事”“收回道义解释权”等抽象概括，'
    '改成谁拿什么理由说服谁、对方怎样回应、眼前谁承担后果。抽象词没有解释清楚时直接删去，不能只换近义词。'
    '每段不必固定按“复述动作、上升意义、总结一句”展开；普通请安、落座、走路不硬说掌握节奏或争夺主动权。'
    '旁白说完一个意思就往下讲，删除意思相同的段尾总结；“一个……一个……”“从……到……再到……”等整齐句式适量，不能连续套用。'
    '分析只放在真正影响人物选择或对话胜负的地方，指出具体依据，允许一句把问题讲透，不为了显得深刻反复解释。'
    '段落长短随内容变化；主要讲述可以连续展开，短转场保持简洁，不能为满足字数把已讲明的动作扩成空分析。'
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


class NarrativeFinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    beat: int = Field(ge=0)
    quote: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    direction: str = Field(min_length=1)


class NarrativeCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    criterion: Literal['continuity','analysis','non_repetition','naturalness']
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


class EditorialPatchReview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    resolved: StrictBool
    reason: str = Field(min_length=1)
    remaining_quote: str


def verify_editorial_patch(clip, index, issues, lines, llm, root):
    """A factually valid edit must also address its whole-script editorial finding."""
    audience,source=narrative_context(clip,lines)
    context=[r for r in audience if abs(r['beat']-index)<=3]
    prompt=(DEPTH_RULES+SOURCE_TIMELINE_RULES+
        '只核对本次target段是否解决给定整稿编辑问题，不重新认人、不宣布全片合格。'
        '若要求去重，换词缩写但仍逐句预告或复述相邻原声，仍未解决；保留必要一句提示可以。'
        '若要求衔接，必须实际交代切换关系；字数长短不能代替判断。'
        '编辑意见也可能误读原片顺序，须对照source_context；不能为迎合错误意见而重排真实事件。'
        'resolved=false时remaining_quote必须逐字引用当前target仍有问题的原句；'
        'resolved=true时remaining_quote必须为空。只核当前修订，不复制已被删除的旧句。'
        '\n格式'+json.dumps(EditorialPatchReview.model_json_schema(),ensure_ascii=False)
        +'\ntarget='+json.dumps(audience[index],ensure_ascii=False)
        +'\neditorial_findings='+json.dumps(issues,ensure_ascii=False)
        +'\naudience='+json.dumps(context,ensure_ascii=False)
        +'\nsource_context='+json.dumps([r for r in source if r['beat']==index],ensure_ascii=False))
    raw=review_json(llm,'你是局部整稿编辑，核对修订是否真正解决重复、衔接、分析或口播自然度问题。',prompt)
    result=EditorialPatchReview.model_validate(raw)
    if ((result.resolved and result.remaining_quote) or
            (not result.resolved and (not result.remaining_quote or result.remaining_quote not in clip.beats[index].text))):
        raise ValueError('整稿修订核验须引用当前问题原句，不能沿用旧稿或虚构引用')
    write_json(root/'editorial-verification.json',{'policy':NARRATIVE_REVIEW_POLICY,
        'clip_digest':digest(clip.model_dump()),'beat':index,**result.model_dump()})
    if not result.resolved:
        raise ValueError('整稿修订仍未落实：'+result.reason+'；当前原句：'+result.remaining_quote)


def review_commentary(clip, lines, cfg, llm, root):
    """Check the final listening experience, including all factual/timing repairs."""
    if not formal_long(cfg):
        return None
    rows,source=narrative_context(clip,lines)
    prompt=(DEPTH_RULES+SOURCE_TIMELINE_RULES+'\n审核整片最终听感，不写稿，不重新认人，不替代原片事实核对。'
        '按continuity、analysis、non_repetition、naturalness四个维度分别给结论，每项恰好一次。'
        'continuity检查主线是否连贯、相邻段是否真正推进，而非每两句重起话头。'
        'analysis检查主要冲突是否解释处境、行动依据、约束或代价，而非把动作复述包装成分析；'
        '允许简短转场，不要求每段凑齐所有分析要素，不强求角色未表露的内心或额外历史知识。'
        'non_repetition检查旁白之间、旁白与相邻原声之间是否长篇重复同一信息；必要提示与回顾可以保留。'
        'naturalness检查读出来是否像自然讲述：抽象术语代替具体解释、普通动作硬拔高、连续套用整齐句式、'
        '每段都补一句没有信息增量的总结，均须指出实际原句。不能因没有“字幕里说”等禁词就认定自然。'
        '这是表达检查，不判断作者是否使用AI，不给检测概率；不要求删掉所有分析，也不把必要的官名和事实术语当问题。'
        '字数多、解说占比高不等于合格，空话填字、换词重复、逐镜头报幕仍需指出。'
        '只把必须修的问题列issues，绑定实际narration段号并逐字引用该段quote；不能修改original。'
        'direction说明如何利用已展示内容改好，不直接给新稿或编造新事实。'
        '没有问题则issues为空；任一维度未通过，必须给出具体问题，不能只给泛泛评价。'
        '\n原声text为机器转写，仅用于理解节目结构；疑似同音字不在这里当作事实错误。'
        '\n格式'+json.dumps(NarrativeReview.model_json_schema(),ensure_ascii=False)
        +'\naudience='+json.dumps(rows,ensure_ascii=False)
        +'\nsource_context='+json.dumps(source,ensure_ascii=False))
    folder=root/'narrative-review'/digest({'policy':NARRATIVE_POLICY,'review_policy':NARRATIVE_REVIEW_POLICY,
        'clip':clip.model_dump(),'rows':rows,'source':source})
    feedback=''
    for attempt in range(cfg.repair_attempts+1):
        raw=review_json(llm,'你是影视解说的整稿编辑，检查连贯性、分析质量和自然口播。',prompt+feedback)
        write_json(folder/f'attempt-{attempt}.json',raw)
        try:
            result=NarrativeReview.model_validate(raw)
            if (len(result.checks)!=len(NARRATIVE_CRITERIA) or {c.criterion for c in result.checks}
                    != NARRATIVE_CRITERIA):
                raise ValueError('整稿检查须精确覆盖四个维度，包括口播自然度')
            if any(not c.passed for c in result.checks) and not result.issues:
                raise ValueError('未通过的整稿检查必须指出具体段落问题')
            for issue in result.issues:
                if (issue.beat>=len(clip.beats) or clip.beats[issue.beat].mode!='narration'
                        or issue.quote not in clip.beats[issue.beat].text):
                    raise ValueError('整稿问题必须引用当前旁白的实际原句与段号')
            break
        except ValueError as exc:
            if attempt==cfg.repair_attempts:raise
            feedback='\n上轮格式或引用无效：'+str(exc)+'。重核实际稿件，不修改或放行存疑内容。'
    report={'policy':NARRATIVE_POLICY,'review_policy':NARRATIVE_REVIEW_POLICY,'clip_digest':digest(clip.model_dump()),
            'passed':not result.issues and all(c.passed for c in result.checks),
            **result.model_dump()}
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
        raise ValueError('整稿连贯性、分析、重复与口播自然度检查未完整通过')
