"""Source-bound automated review and local repair, separate from human approval."""
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .identity import units_for_clip, file_hash
from .ingest import contact_sheet
from .models import Plan, Beat, validate_plan
from .util import digest, read_json, source_identity, write_json
from .voices import match_batch

VERSION = 'automatic-review-1'


def link_voice_evidence(video, lines, library, manifest):
    """Rematch against the current human library; never trust old suggested names."""
    source = source_identity(video)
    source_ids = {s['id'] for s in manifest['sources'] if s['source'] == source}
    if not source_ids:
        raise ValueError('声纹候选没有绑定当前原片版本')
    for person in library['characters']:
        for sample in person['samples']:
            if sample.get('status') != 'human_confirmed':
                raise ValueError('参考库只能包含人工确认声纹')
            if source_identity(Path(sample['source']['path'])) != sample['source']:
                raise ValueError('声纹参考原片已改变')
    selected = {**manifest, 'samples':[s for s in manifest['samples'] if s['source_id'] in source_ids]}
    matched = match_batch(library, selected)
    rows = []
    for line in lines:
        candidates = [s for s in matched['samples'] if min(s['end'],line.end)-max(s['start'],line.start) >= .3]
        names = {s['name'] for s in candidates if s.get('name')}
        rows.append({'line_id':line.id, 'start':line.start, 'end':line.end,
                     'status':'conflict' if len(names)>1 else ('candidate' if names else 'unknown'),
                     'samples':candidates})
    return {'version':VERSION, 'source':source, 'transcript_digest':digest([s.model_dump() for s in lines]),
            'library_digest':digest(library), 'manifest_digest':digest(manifest), 'lines':rows,
            'policy':'声音相似仅为候选；不得单独认定角色或把说话人当成行为者。机器结论不回写人工库。'}


class UnitDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    verdict: Literal['supported','contradicted','unknown']
    reason: str = Field(min_length=1)
    evidence_ids: list[int] = Field(min_length=1)
    frame_ids: list[int] = Field(min_length=1)
    identity_basis: Literal['no_identity_claim','voice_and_context','explicit_address_response','insufficient']
    subjects: list[str] = Field(default_factory=list)


class ReviewBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    passed: StrictBool
    units: list[UnitDecision]
    issues: list[str]


def assess_batch(raw, units, lines, frame_count, voice_rows):
    parsed = ReviewBatch.model_validate(raw)
    expected = {u['id']:u for u in units}
    if len(parsed.units) != len(expected) or {u.id for u in parsed.units} != set(expected):
        raise ValueError('自动审核未精确覆盖全部待核单位')
    lookup = {s.id:s for s in lines}
    voices = {r['line_id']:r for r in voice_rows}
    issues = [{'id':u.id,'message':u.reason} for u in parsed.units if u.verdict != 'supported']
    for unit in parsed.units:
        if not set(unit.evidence_ids).issubset(lookup) or any(i<0 or i>=frame_count for i in unit.frame_ids):
            raise ValueError('自动审核引用了未提供的证据或画面')
        target = expected[unit.id]
        if 'end' in target and any(lookup[i].start >= target['end'] and i not in target.get('shown_evidence',[]) for i in unit.evidence_ids):
            issues.append({'id':unit.id,'message':'审核依据预支了本镜头之后的剧情'})
        if unit.identity_basis == 'insufficient':
            issues.append({'id':unit.id,'message':'人物依据不足，不能放行'})
        if unit.identity_basis == 'explicit_address_response' and len(set(unit.evidence_ids))<2:
            issues.append({'id':unit.id,'message':'明确称呼与回应需至少两条不同证据'})
        if unit.identity_basis == 'voice_and_context':
            associated = [voices.get(i, {}) for i in unit.evidence_ids]
            candidate_names = {s['name'] for r in associated for s in r.get('samples',[]) if s.get('name')}
            if not any(r.get('status') == 'candidate' for r in associated) or any(r.get('status') == 'conflict' for r in associated):
                issues.append({'id':unit.id,'message':'声音候选缺失或冲突，须用明确称呼与回应核实或删除不确定归属'})
            if not unit.subjects or not set(unit.subjects).issubset(candidate_names):
                issues.append({'id':unit.id,'message':'声音依据未支持本条主张中的人物，不能借其他人的候选放行'})
    if parsed.issues or not parsed.passed:
        issues.extend({'id':u['id'],'message':'; '.join(parsed.issues) or '审核未通过'} for u in units)
    return issues


def audit_plan(video, plan, lines, voices, root, cfg, llm):
    if not cfg.vision:
        raise ValueError('自动长片审核需要画面，不能静默跳过')
    if voices['source'] != source_identity(video) or voices['transcript_digest'] != digest([s.model_dump() for s in lines]):
        raise ValueError('人物证据与当前素材或字幕不一致')
    clip = plan.clips[0]
    units = units_for_clip(clip)
    if cfg.opening == 'original_teaser':
        for unit in units:
            if unit['id'] == 'beat-1':
                unit['shown_evidence'] = clip.beats[0].evidence_ids
    # Original footage is audited too: an edit may otherwise invent a response.
    units += [{'id':f'original-{i}', 'text':b.reason, 'start':b.start, 'end':b.end,
               'evidence_ids':b.evidence_ids} for i,b in enumerate(clip.beats) if b.mode == 'original']
    key = digest({'v':VERSION,'plan':plan.model_dump(),'voices':voices,'model':cfg.llm_model,'endpoint':cfg.llm_base_url})
    folder = root/'automatic-review'/key
    all_issues, batches, frames = [], [], []
    for offset in range(0,len(units),6):
        group = units[offset:offset+6]
        ids = {i for u in group for i in u.get('evidence_ids',[]) + u.get('shown_evidence',[])}
        # Metadata is checked against the actual selected footage, not an invented summary.
        if any('start' not in u for u in group):
            ids.update(i for b in clip.beats for i in b.evidence_ids)
        selected = [s for s in lines if s.id in ids or any('start' in u and u['start']-8<=s.start<u['end']+8 for u in group)]
        intervals = [(u['start'],u['end']) for u in group if 'start' in u]
        if not intervals:
            intervals = [(b.start,b.end) for b in clip.beats[:6]]
        times = sorted({round(t,3) for a,b in intervals for t in (a+.1,(a+b)/2,b-.1)})
        sheet = contact_sheet(video,folder,times,f'batch-{offset}')
        evidence = [r for r in voices['lines'] if r['line_id'] in {s.id for s in selected}]
        prompt = ('逐项核对文本事实、人物行为归属、画面相关性、连续原声切点、场景承接和重复解释。'
                  '不能因同一模型写稿就默认正确。声纹是候选；画面人物可能是听者。'
                  '声纹有冲突时只能以明确叫名与连续问答核实，否则unknown；不能凭剧名记忆认人。'
                  '身份依据分为无身份主张、声纹加语境、明确称呼回应、依据不足。泛称仍可能有身份主张。'
                  'subjects列出涉及身份归属的实际人物名；声纹加语境必须列出与当前候选一致的名字。'
                  '不得把承诺写成结果，不能预支当前镜头后文；shown_evidence是已播放高光，首段桥接可引用。分析须与事实分开。'
                  'original单位核对切点是否破坏完整问答或制造因果，不把reason当作观众口播。'
                  'title/angle/hook根据全片选用证据核查。保留必要剧情与自然转场，不要求每段都有观点。'
                  '图片编号按times顺序从0开始。证据必须来自给定字幕；每个单位至少引用一条字幕及一幅画面。'
                  '任何未知或错误均passed=false，issues给出可操作原因；不要直接修稿。'
                  f'\n格式{json.dumps(ReviewBatch.model_json_schema(),ensure_ascii=False)}'
                  f'\nunits={json.dumps(group,ensure_ascii=False)}\ntimes={times}'
                  f'\n字幕={json.dumps([s.model_dump() for s in selected],ensure_ascii=False)}'
                  f'\n声音候选={json.dumps(evidence,ensure_ascii=False)}')
        raw = llm.json('你是独立的影视内容与人物证据审核员，不读取人工答案。',prompt,[sheet])
        try:
            issues = assess_batch(raw,group,selected,len(times),evidence)
        except ValueError as exc:
            issues = [{'id':u['id'],'message':str(exc)} for u in group]
        record = {'units':group,'times':times,'raw':raw,'issues':issues}
        write_json(folder/f'batch-{offset}.json',record)
        batches.append(str((folder/f'batch-{offset}.json').resolve()))
        frames.append({'path':str(sheet.resolve()),'sha256':file_hash(sheet)})
        all_issues.extend(issues)
    report = {'mode':'automatic_source_review','version':VERSION,'source':source_identity(video),
              'plan_digest':digest(plan.model_dump()),'transcript_digest':digest([s.model_dump() for s in lines]),
              'voice_digest':digest(voices),'passed':not all_issues,'issues':all_issues,'batches':batches,'frames':frames,
              'limitations':['机器审核可能误判；声纹候选不等于人工确定身份。']}
    write_json(folder/'report.json',report)
    write_json(root/'automatic-review.json',report)
    return report


def repair_plan(plan, issues, lines, voices, root, cfg, llm, attempt):
    """Patch only named units; unchanged shots are byte-for-byte preserved."""
    allowed = {i['id'] for i in issues}
    prompt = ('只修列出的单位，保留其他镜头和原声；证据不足就删除该断言或改为能被当前证据支持的说法。'
              '不能用泛称掩盖错误归属。不要新增情节，不为长度凑字。'
              '返回changes数组，每项id及text；beat-N可附start/end/evidence_ids用于修复对应镜头，'
              'original-N只能附start/end/evidence_ids，不能虚构对白。title/angle/hook只能改text。'
              '不能增加、删除或换序镜头。时间不得越过相邻镜头；保留整句原声。'
              f'\n问题{json.dumps(issues,ensure_ascii=False)}\n稿件{plan.model_dump_json()}'
              f'\n字幕{json.dumps([s.model_dump() for s in lines],ensure_ascii=False)}'
              f'\n人物候选{json.dumps(voices,ensure_ascii=False)}')
    raw = llm.json('你是局部修稿编辑，只依据提供证据修复明确问题。',prompt)
    write_json(root/f'automatic-repair-{attempt}.json',raw)
    revised = plan.model_copy(deep=True)
    changes = raw.get('changes') if isinstance(raw,dict) else None
    if not isinstance(changes,list) or not changes:
        raise ValueError('自动局部修订没有有效改动')
    seen = set()
    for row in changes:
        if not isinstance(row,dict) or row.get('id') not in allowed or row['id'] in seen:
            raise ValueError('局部修订只能修改问题中列明的单位一次')
        uid = row['id']; seen.add(uid)
        if uid in {'title','angle','hook'}:
            if set(row) != {'id','text'} or not isinstance(row['text'],str):
                raise ValueError('标题修订字段无效')
            setattr(revised.clips[0],uid,row['text'])
        else:
            if set(row)-{'id','text','start','end','evidence_ids'}:
                raise ValueError('镜头修订含未知字段')
            index = int(uid.split('-')[1])
            original = revised.clips[0].beats[index]
            if uid.startswith('original-') and 'text' in row:
                raise ValueError('不能改写原声')
            revised.clips[0].beats[index] = Beat.model_validate({**original.model_dump(),**{k:v for k,v in row.items() if k!='id'}})
    revised = Plan.model_validate(revised.model_dump())
    if revised == plan:
        raise ValueError('自动修订重复原稿，停止无效重试')
    return revised


class FrozenAutomaticScript:
    frozen_script = True
    def json(self,*args,**kwargs):
        raise ValueError('自动核定稿渲染中禁止改写；需重新测时和审核')


def assert_approval(report, video, plan, lines, voices):
    if (report.get('mode') != 'automatic_source_review' or report.get('passed') is not True or report.get('issues') != []
            or report.get('source') != source_identity(video) or report.get('plan_digest') != digest(plan.model_dump())
            or report.get('transcript_digest') != digest([s.model_dump() for s in lines])
            or report.get('voice_digest') != digest(voices) or not report.get('frames')):
        raise ValueError('自动审核记录与当前稿件、素材或声纹版本不一致')
    if any(file_hash(Path(f['path'])) != f['sha256'] for f in report['frames']):
        raise ValueError('自动审核画面已改变')
