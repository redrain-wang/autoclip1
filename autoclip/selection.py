"""Editorial selection of original audio, separate from factual approval."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import Beat, Plan, validate_plan
from .util import digest, write_json

SELECTION_RULES = (
    '按本片主线选择素材，不因为原剧有这一场就保留。原声应提供新信息、关键决定、真正的冲突或人物转折；'
    '必要铺垫可以保留，但洗澡、换衣、叫人、寒暄等生活过场若不推进当前主线，优先删除，必要时只用简短承接。'
    '上述场景不是关键词黑名单；若确有关键情节仍可保留，须说明具体作用，不能凭场景名称判断价值。'
    '只有一句有用，就只留那句及必要回应，不能拿一个名字或一句信息给整段闲聊背书。'
    '原声优先保留值得亲耳听的关键台词与交锋；普通报情况、交代安排若能简洁讲清，优先转述。'
    '原声真实、对白完整、旁白修稿失败，都不等于值得保留。恢复原声后仍须重新判断选材与整段节奏。'
    '按成片播放顺序合看连续原声，原片时间有跳跃也不能重置这段的累计时长。'
    '连续原声达到90秒是重点复核提示，不是长度禁令；完整而持续推进的交锋可以保留，'
    '混入重复问答或低价值过场则删除、缩短或转述，不为打断原声硬插空话。'
    '不能为凑18–22分钟保留低价值片段，也不能删掉必要问句、起因或回应，拼成虚假的因果。'
)


def original_runs(clip, timing=None):
    """Adjacent output beats form a run even across gaps in source time."""
    runs=[]
    for index,beat in enumerate(clip.beats):
        if beat.mode!='original':continue
        seconds=timing['beats'][index]['output_seconds'] if timing else beat.end-beat.start
        if not runs or runs[-1]['beats'][-1]!=index-1:
            runs.append({'beats':[],'seconds':0.,'restored_beats':[]})
        run=runs[-1];run['beats'].append(index);run['seconds']+=seconds
        if beat.reason.startswith('局部口播修稿未通过'):
            run['restored_beats'].append(index)
    return [{**r,'seconds':round(r['seconds'],3),'review_focus':r['seconds']>=90} for r in runs]


class OriginalEdit(BaseModel):
    model_config=ConfigDict(extra='forbid')
    beat: int = Field(ge=0)
    action: Literal['trim','narrate']
    start: float
    end: float
    text: str = ''
    evidence_ids: list[int] = Field(default_factory=list)


class OriginalEdits(BaseModel):
    model_config=ConfigDict(extra='forbid')
    changes: list[OriginalEdit]


def repair_original_selection(plan, issues, lines, root, cfg, llm, attempt, source_seconds):
    """Apply only explicitly reviewed original edits, then require full re-review."""
    from .editorial import WRITING_RULES
    from .narrative import NARRATIVE_REVIEW_POLICY, require_commentary
    clip=plan.clips[0];targets={}
    for issue in issues:
        index=int(issue['id'].split('-')[1]);beat=clip.beats[index]
        if (issue.get('stage')!='selection_review' or issue.get('review_policy')!=NARRATIVE_REVIEW_POLICY
                or issue.get('clip_digest')!=digest(clip.model_dump())
                or issue.get('beat_digest')!=digest(beat.model_dump()) or beat.mode!='original'
                or issue.get('action') not in {'drop','trim','narrate'}):
            raise ValueError('选材意见未绑定当前原声与整稿，不能修改')
        if index in targets and targets[index]['action']!=issue['action']:
            raise ValueError('同一原声的选材意见相互冲突')
        targets[index]=issue
    if not targets:raise ValueError('缺少原声选材修改目标')
    if 0 in targets and targets[0]['action']!='trim':
        raise ValueError('不能删除或改配音取代原声开场；须另选完整开场')
    folder=root/'selection-repairs'/str(attempt)
    editable={i:issue for i,issue in targets.items() if issue['action']!='drop'}
    prompt=(SELECTION_RULES+WRITING_RULES+
        '按已绑定的编辑意见修改原声，只返回editable中各项一次，不能换action，不能改其他镜头。'
        'trim只缩短当前区间、text为空；narrate用准确简洁的第三人称转述，不替角色裸说台词。'
        'start/end不得超出原区间，最多40秒；trim保留完整对白，开场仍须满足原开场长度要求。'
        'narrate的evidence_ids仅用当前原声已有编号；不能补剧情、猜人物或借未来对白。'
        '\n格式'+json.dumps(OriginalEdits.model_json_schema(),ensure_ascii=False)
        +'\neditable='+json.dumps([{'beat':i,'original':clip.beats[i].model_dump(),'finding':issue}
            for i,issue in editable.items()],ensure_ascii=False)
        +'\n字幕='+json.dumps([s.model_dump() for s in lines if any(s.id in clip.beats[i].evidence_ids for i in editable)],ensure_ascii=False))
    feedback=''
    for retry in range(cfg.repair_attempts+1):
        raw=llm.json('你是原声选材编辑，落实已给出的删减或转述意见，不宣布成片合格。',prompt+feedback) if editable else {'changes':[]}
        try:
            changes=OriginalEdits.model_validate(raw).changes
            if len(changes)!=len(editable) or {c.beat for c in changes}!=set(editable):
                raise ValueError('仅可修改editable中的每个原声一次')
            replacements={}
            for change in changes:
                before=clip.beats[change.beat]
                if (change.action!=editable[change.beat]['action'] or change.start<before.start
                        or change.end>before.end or not 1<=change.end-change.start<=40):
                    raise ValueError('选材修改越过原区间、改了操作或时长不合法')
                if change.action=='trim':
                    if change.text:raise ValueError('缩短原声不能新增口播')
                    if change.start<=before.start+.001 and change.end>=before.end-.001:
                        raise ValueError('trim必须实际缩短原声')
                    if any(s.start+.001<cut<s.end-.001 for s in lines for cut in (change.start,change.end)):
                        raise ValueError('缩短原声不能切断完整对白')
                    ids=[s.id for s in lines if change.start-.001<=s.start and s.end<=change.end+.001]
                    if not ids:raise ValueError('缩短原声须保留完整对白')
                    if not set(change.evidence_ids).issubset(ids):
                        raise ValueError('缩短原声引用超出保留区间')
                    replacement=Beat(mode='original',start=change.start,end=change.end,evidence_ids=ids,
                        reason='按整稿选材意见缩短；仍须完整测时和复核')
                else:
                    if not change.text.strip() or not change.evidence_ids or not set(change.evidence_ids).issubset(before.evidence_ids):
                        raise ValueError('原声转述须有正文，引用只能来自本段')
                    replacement=Beat(mode='narration',start=change.start,end=change.end,text=change.text,
                        evidence_ids=change.evidence_ids,reason='按整稿选材意见转述；仍须完整测时和事实核对')
                if replacement.model_dump(exclude={'reason'})==before.model_dump(exclude={'reason'}):
                    raise ValueError('选材修改未实际改变内容或区间')
                replacements[change.beat]=replacement
            revised=plan.model_copy(deep=True)
            revised.clips[0].beats=[replacements.get(i,b) for i,b in enumerate(clip.beats)
                if i not in targets or targets[i]['action']!='drop']
            revised=Plan.model_validate(revised.model_dump())
            require_commentary(revised.clips[0],cfg)
            validate_plan(revised,lines,source_seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,
                cfg.opening,check_duration=False)
            write_json(folder/'result.json',{'before_digest':digest(plan.model_dump()),'issues':issues,
                'raw':raw,'plan':revised.model_dump(),'status':'requires_full_measurement_and_review'})
            return revised
        except ValueError as exc:
            write_json(folder/f'rejected-{retry}.json',{'raw':raw,'error':str(exc)})
            if not editable or retry==cfg.repair_attempts:raise
            feedback='\n上次修改无效：'+str(exc)+'；返回全部editable项，不能切断对白、扩大范围或增加其他镜头。'
