"""Long commentary must remain commentary after timing and factual repairs."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .budget import spoken_characters
from .util import digest, write_json
from .llm import review_json

NARRATIVE_POLICY = 'connected-commentary-1'
DEPTH_RULES = (
    '剧情优先不是只复述动作或用两句过场话串原声。先按章节构成连贯讲述，再分配到镜头。'
    '主要冲突必须讲清处境、行动依据、可选择的做法与约束、眼前后果；不要求每个短转场分析。'
    '主要解说段通常90–170字，形成完整思路；需要换画面时相邻解说接着讲，不在每个镜头重新开头。'
    '关键原声用于展示决定、反驳或冲突，不连续保留大量问答再用一句话复述。'
    '分析从已展示的行为与明确说法推导，区分事实和解读：可以解释利益约束、话语作用和行动代价，'
    '不要求角色亲口说出旁白的每一步推理；但不得虚构动机、关系、结果或后集事实。'
    '避免空泛权谋金句、动作流水账和逐镜头报幕；深度来自可理解的推理链，不来自字数或长句本身。'
)


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
    criterion: Literal['continuity','analysis','non_repetition']
    passed: StrictBool
    reason: str = Field(min_length=1)


class NarrativeReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    checks: list[NarrativeCheck]
    issues: list[NarrativeFinding]


def review_commentary(clip, lines, cfg, llm, root):
    """Check the final listening experience, including all factual/timing repairs."""
    if not formal_long(cfg):
        return None
    rows=[]
    for index,beat in enumerate(clip.beats):
        text=(beat.text if beat.mode=='narration' else
              ' '.join(s.text for s in lines if s.start<beat.end and s.end>beat.start))
        rows.append({'beat':index,'mode':beat.mode,'text':text})
    prompt=(DEPTH_RULES+'\n审核整片最终听感，不写稿，不重新认人，不替代原片事实核对。'
        '按continuity、analysis、non_repetition三个维度分别给结论，每项恰好一次。'
        'continuity检查主线是否连贯、相邻段是否真正推进，而非每两句重起话头。'
        'analysis检查主要冲突是否解释处境、行动依据、约束或代价，而非把动作复述包装成分析；'
        '允许简短转场，不要求每段凑齐所有分析要素，不强求角色未表露的内心或额外历史知识。'
        'non_repetition检查旁白之间、旁白与相邻原声之间是否长篇重复同一信息；必要提示与回顾可以保留。'
        '字数多、解说占比高不等于合格，空话填字、换词重复、逐镜头报幕仍需指出。'
        '只把必须修的问题列issues，绑定实际narration段号并逐字引用该段quote；不能修改original。'
        'direction说明如何利用已展示内容改好，不直接给新稿或编造新事实。'
        '没有问题则issues为空；任一维度未通过，必须给出具体问题，不能只给泛泛评价。'
        '\n原声text为机器转写，仅用于理解节目结构；疑似同音字不在这里当作事实错误。'
        '\n格式'+json.dumps(NarrativeReview.model_json_schema(),ensure_ascii=False)
        +'\n观众实际听到的顺序='+json.dumps(rows,ensure_ascii=False))
    folder=root/'narrative-review'/digest({'policy':NARRATIVE_POLICY,'clip':clip.model_dump(),'rows':rows})
    feedback=''
    for attempt in range(cfg.repair_attempts+1):
        raw=review_json(llm,'你是影视深度解说的整稿编辑，检查连贯性与分析质量。',prompt+feedback)
        write_json(folder/f'attempt-{attempt}.json',raw)
        try:
            result=NarrativeReview.model_validate(raw)
            if (len(result.checks)!=3 or {c.criterion for c in result.checks}
                    != {'continuity','analysis','non_repetition'}):
                raise ValueError('整稿检查须精确覆盖三个维度')
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
    report={'policy':NARRATIVE_POLICY,'clip_digest':digest(clip.model_dump()),
            'passed':not result.issues and all(c.passed for c in result.checks),
            **result.model_dump()}
    write_json(folder/'report.json',report)
    write_json(root/'narrative-review.json',report)
    return report


def require_commentary_review(report, clip):
    if (not isinstance(report,dict) or report.get('policy')!=NARRATIVE_POLICY
            or report.get('clip_digest')!=digest(clip.model_dump())
            or report.get('passed') is not True or report.get('issues')!=[]):
        raise ValueError('缺少当前完整稿件的连贯解说核定，不能交付')
    checks=NarrativeReview.model_validate({'checks':report.get('checks'),'issues':report['issues']}).checks
    if len(checks)!=3 or {c.criterion for c in checks}!={'continuity','analysis','non_repetition'} or not all(c.passed for c in checks):
        raise ValueError('整稿连贯性、分析与重复检查未完整通过')
