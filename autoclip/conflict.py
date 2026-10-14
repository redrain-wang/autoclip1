"""A practical editorial brief: one question, its development, and its actual ending."""

import json
from typing import Literal

from pydantic import BaseModel, Field

from .util import digest, read_json, source_identity, timestamp, write_json

VERSION = 'conflict-2'


class ConflictStep(BaseModel):
    role: Literal['起因', '阻力', '选择', '转折', '结果']
    description: str
    evidence_ids: list[int] = Field(min_length=1)
    commentary_job: str


class ConflictBrief(BaseModel):
    question: str
    why_it_matters: str
    steps: list[ConflictStep]
    ending: str
    unresolved: list[str] = Field(default_factory=list)
    opening_evidence_ids: list[int] = Field(default_factory=list)
    opening_reason: str = ''
    omit: list[str] = Field(default_factory=list)


def prepare_conflict_brief(video, root, lines, start, end, cfg, llm, question='', notes=''):
    from .director import compact
    selected = [s for s in lines if s.start < end and s.end > start]
    if not selected:
        raise ValueError('故事范围内没有可整理的字幕')
    lookup = {s.id:s for s in selected}
    inputs = {'version':VERSION,'source':source_identity(video),'start':start,'end':end,
              'lines':[s.model_dump() for s in selected],'question':question,'notes':notes,
              'model':cfg.llm_model,'endpoint':cfg.llm_base_url}
    path = root/'conflict-briefs'/f'{digest(inputs)}.json'
    if path.exists():
        raw = read_json(path.with_suffix('.raw.json'))
    else:
        raw = llm.json('你是影视解说选题编辑。先依据本集原片把一个完整冲突说清楚，不编造剧情。',
            f'《{cfg.series}》当前选段{start}–{end}秒，编辑主问题参考：{question}。'
            '整理一份简短的写稿提纲，不是口播全文：核心问题是什么，观众为什么在意；'
            '有哪些有因果关联的起因、阻力、选择、转折或结果；结尾已经走到哪里。'
            'steps按实际发生顺序，类型按需要使用，不要求每种都有；没有转折不要硬编，结局未定就明确本段实际结果和未决事项。'
            '主问题和看点也不能提前宣告未发生的结果：暂缓行刑、继续查问不写成已经翻案昭雪。'
            'description写台词能支持的事件；commentary_job写此处旁白要解释的选择、压力、收益或代价，不再复述原声。'
            '解说任务只提出值得解释的问题或有依据的解释，不把“正常程序绝不可能”“只能这样做”等推测写成既定规则。'
            '不要为了凑集数、固定分钟数或金句拼进无关场景，不要求把整集所有情节都塞进同一条。'
            'opening_evidence_ids选当前范围内最能让观众立即理解冲突的连续高光对白，原声约20秒供剪辑参考，可以来自后半段；不硬凑时长。'
            'omit写哪些重复解释或无关支线可以舍弃；不写程序要求或审稿式免责声明。'
            '人物称谓不明确时只描述原片可确认的关系，不凭儿臣、官服、ASR错名认人；角色指责和已经落实的事实要分开。'
            '疑似转写错字的人名、刑罚和官职不照抄扩写，保留清楚的称谓或省去不影响主问题的细节。'
            '提纲是选题建议，仍需现有内容核对。证据只能用当前字幕ID，不借其他集或未来剧情。'
            f'\n当前原片核对备注（只辅助明确的称谓归属，不扩展成新情节）：{notes}'
            f'\n格式{json.dumps(ConflictBrief.model_json_schema(),ensure_ascii=False)}\n本段字幕{compact(selected)}')
        write_json(path.with_suffix('.raw.json'),raw)
    data = ConflictBrief.model_validate(raw)
    warnings, steps = [], []
    for step in data.steps:
        if any(i not in lookup for i in step.evidence_ids):
            warnings.append('提纲一项引用了本段以外的字幕，未传给写稿：'+step.description)
            continue
        evidence = sorted((lookup[i] for i in step.evidence_ids),key=lambda s:s.start)
        steps.append({**step.model_dump(),'start':evidence[0].start,'end':max(s.end for s in evidence)})
    steps.sort(key=lambda s:s['start'])
    opening = sorted((lookup[i] for i in data.opening_evidence_ids if i in lookup),key=lambda s:s.start)
    if len(opening) != len(data.opening_evidence_ids):
        warnings.append('开场引用含区间外字幕，未保留该开场建议')
        opening = []
    if opening:
        continuous = [s.id for s in selected if opening[0].start <= s.start <= opening[-1].start]
        if [s.id for s in opening] != continuous:
            warnings.append('开场建议拼接了不连续对白，未作为连续原声开场传给写稿')
            opening = []
    result = {'inputs':inputs,'status':'editorial_suggestion_not_reviewed_facts',
              **data.model_dump(),'steps':steps,'warnings':warnings,
              'opening_reason':data.opening_reason if opening else '',
              'opening_evidence_ids':[s.id for s in opening],
              'opening_source_range': [min(s.start for s in opening),max(s.end for s in opening)] if opening else None}
    write_json(path,result)
    write_json(root/'conflict-brief.json',result)
    rows=['# 完整冲突提纲','',f"主问题：{result['question']}",'',f"看点：{result['why_it_matters']}",'',
          '提纲用于安排解说，不是人物或剧情已核实证明。','']
    for step in result['steps']:
        rows += [f"- {step['role']} · {timestamp(step['start'])}–{timestamp(step['end'])}：{step['description']}",
                 f"  解说任务：{step['commentary_job']}",'']
    rows += [f"结尾落点：{result['ending']}",'',f"尚未解决：{'；'.join(result['unresolved']) or '未列出'}",'',
             f"开场建议：{result['opening_reason'] or '未提供可用的连续原声建议'}",f"可舍弃：{'；'.join(result['omit']) or '未列出'}"]
    rows += ['', *[f'整理提示：{w}' for w in result['warnings']]]
    (root/'完整冲突提纲.md').write_text('\n'.join(rows),encoding='utf-8')
    return result


def brief_context(brief):
    """Only the compact editorial suggestion enters drafting, not a new approval gate."""
    return {k:v for k,v in brief.items() if k not in {'inputs','warnings'}}
