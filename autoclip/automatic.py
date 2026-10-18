"""Source-bound automated review and local repair, separate from human approval."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .identity import units_for_clip, file_hash
from .ingest import contact_sheet
from .models import Plan, Beat, validate_plan, evidence_matches
from .util import digest, read_json, source_identity, write_json
from .voices import match_batch

VERSION = 'automatic-review-5'


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
            'coverage':{'transcript_lines':len(rows),'audio_windows':len(matched['samples']),
                        'lines_with_audio':sum(bool(r['samples']) for r in rows),
                        'candidate_lines':sum(r['status']=='candidate' for r in rows),
                        'conflict_lines':sum(r['status']=='conflict' for r in rows)},
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


class UnitIssue(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    message: str = Field(min_length=1)


class ReviewBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    passed: StrictBool
    units: list[UnitDecision]
    issues: list[UnitIssue]


def assess_batch(raw, units, lines, frame_count, voice_rows):
    parsed = ReviewBatch.model_validate(raw)
    expected = {u['id']:u for u in units}
    if len(parsed.units) != len(expected) or {u.id for u in parsed.units} != set(expected):
        raise ValueError('自动审核未精确覆盖全部待核单位')
    lookup = {s.id:s for s in lines}
    voices = {r['line_id']:r for r in voice_rows}
    issues = [{'id':u.id,'message':u.reason} for u in parsed.units if u.verdict != 'supported']
    for issue in parsed.issues:
        if issue.id not in expected:
            raise ValueError('自动审核问题指向了本批次以外的单位')
        issues.append(issue.model_dump())
    for unit in parsed.units:
        if not set(unit.evidence_ids).issubset(lookup) or any(i<0 or i>=frame_count for i in unit.frame_ids):
            raise ValueError('自动审核引用了未提供的证据或画面')
        target = expected[unit.id]
        if 'end' in target and any(lookup[i].start >= target['end'] and i not in target.get('shown_evidence',[]) for i in unit.evidence_ids):
            if unit.verdict == 'supported':
                raise ValueError(f'{unit.id} 声称有据但引用未来对白；请重核引用与结论，不能据此直接改稿')
            issues.append({'id':unit.id,'message':'审核依据预支了本镜头之后的剧情'})
        if unit.identity_basis == 'insufficient':
            issues.append({'id':unit.id,'message':'人物依据不足，不能放行'})
        if unit.identity_basis == 'no_identity_claim' and unit.subjects:
            issues.append({'id':unit.id,'message':'列出了人物归属却声明没有身份主张，须明确依据'})
        if unit.identity_basis == 'explicit_address_response' and len(set(unit.evidence_ids))<2:
            issues.append({'id':unit.id,'message':'明确称呼与回应需至少两条不同证据'})
        if unit.identity_basis == 'voice_and_context':
            associated = [voices.get(i, {}) for i in unit.evidence_ids]
            candidate_names = {s['name'] for r in associated for s in r.get('samples',[]) if s.get('name')}
            if not any(r.get('status') == 'candidate' for r in associated) or any(r.get('status') == 'conflict' for r in associated):
                issues.append({'id':unit.id,'message':'声音候选缺失或冲突，须用明确称呼与回应核实或删除不确定归属'})
            if not unit.subjects or not set(unit.subjects).issubset(candidate_names):
                issues.append({'id':unit.id,'message':'声音依据未支持本条主张中的人物，不能借其他人的候选放行'})
    if not parsed.passed and not issues:
        raise ValueError('自动审核声称未通过但未指出任何具体单位问题')
    return issues


def audit_plan(video, plan, lines, voices, root, cfg, llm):
    if not cfg.vision:
        raise ValueError('自动长片审核需要画面，不能静默跳过')
    if voices['source'] != source_identity(video) or voices['transcript_digest'] != digest([s.model_dump() for s in lines]):
        raise ValueError('人物证据与当前素材或字幕不一致')
    clip = plan.clips[0]
    units = units_for_clip(clip)
    # Original footage is audited too: an edit may otherwise invent a response.
    units += [{'id':f'original-{i}', 'mode':'original', 'text':'连续原声：核对对白切点、问答顺序与场景承接', 'start':b.start, 'end':b.end,
               'evidence_ids':b.evidence_ids} for i,b in enumerate(clip.beats) if b.mode == 'original']
    lookup = {s.id:s for s in lines}
    for unit in units:
        if unit['id'].startswith(('beat-','original-')):
            index = int(unit['id'].split('-')[1])
            prior = {eid for b in clip.beats[:index] for eid in b.evidence_ids}
            unit['shown_evidence'] = sorted(eid for eid in prior if lookup[eid].start<unit['end']
                or (cfg.opening=='original_teaser' and index==1 and eid in clip.beats[0].evidence_ids))
    key = digest({'v':VERSION,'plan':plan.model_dump(),'voices':voices,'model':cfg.llm_model,'endpoint':cfg.llm_base_url})
    folder = root/'automatic-review'/key
    all_issues, batches, frames = [], [], []
    def review_group(offset):
        group = units[offset:offset+3]
        ids = {i for u in group for i in u.get('evidence_ids',[]) + u.get('shown_evidence',[])}
        # Metadata is checked against the actual selected footage, not an invented summary.
        if any('start' not in u for u in group):
            ids.update(i for b in clip.beats for i in b.evidence_ids)
        selected = [s for s in lines if s.id in ids or any('start' in u and u['start']-8<=s.start<u['end']+8 for u in group)]
        intervals = [(u['start'],u['end']) for u in group if 'start' in u]
        if intervals:
            times = sorted({round(t,3) for a,b in intervals for t in (a+.1,(a+b)/2,b-.1)})
        else:
            # Metadata spans the selected story. Keep the sheet bounded while
            # sampling its beginning, middle and end rather than only the intro.
            count = min(8,len(clip.beats))
            selected_beats = [clip.beats[round(i*(len(clip.beats)-1)/max(1,count-1))] for i in range(count)]
            times = sorted({round((b.start+b.end)/2,3) for b in selected_beats})
        sheet = contact_sheet(video,folder/f'batch-{offset}-images',times,f'batch-{offset}')
        evidence = [r for r in voices['lines'] if r['line_id'] in {s.id for s in selected}]
        prompt = ('逐项核对文本事实、人物行为归属、画面相关性、连续原声切点、场景承接和重复解释。'
                  '不能因同一模型写稿就默认正确。声纹是候选；画面人物可能是听者。'
                  '声纹有冲突时只能以明确叫名与连续问答核实，否则unknown；不能凭剧名记忆认人。'
                  '身份依据分为无身份主张、声纹加语境、明确称呼回应、依据不足。泛称仍可能有身份主张。'
                  'subjects列出涉及身份归属的实际人物名；声纹加语境必须列出与当前候选一致的名字。'
                  '不得把承诺写成结果，不能预支当前镜头后文；shown_evidence是此单位之前观众已看到的对白，可支持承接与回顾。分析须与事实分开。'
                  'original单位只有原片本身的连续声音与画面，没有新增旁白或人物标签；只核对切点是否破坏完整对白、伪造问答因果。'
                  '原声本身没有新增身份主张时identity_basis=no_identity_claim、subjects=[]。'
                  'ASR可能有同音或繁简错字，应区别转写疑点与实质事实错误；他/她同音，不能据ASR字形断言原声所指性别错误。'
                  '旁白和标题中的人物归属仍必须有依据；用清楚的关系或行动描述人物可以，不能用含糊泛称掩盖错误行为归属。'
                  'title/angle/hook根据全片选用证据核查。保留必要剧情与自然转场，不要求每段都有观点。'
                  '只审核本批units；不要求其他批次的标题或镜头出现在这里。每个问题都写具体id，不把单段问题复制给所有单位。'
                  '时间区间包含start、不包含end；end恰好等于下一条字幕start并未剪入该字幕，不得据此误判截断或预支。'
                  '图片编号按times顺序从0开始。证据必须来自给定字幕；每个单位至少引用一条字幕及一幅画面。'
                  '任何未知或错误均passed=false，issues每项只含id和message，指向本批的具体单位；不要直接修稿。'
                  f'\n格式{json.dumps(ReviewBatch.model_json_schema(),ensure_ascii=False)}'
                  f'\nunits={json.dumps(group,ensure_ascii=False)}\ntimes={times}'
                  f'\n字幕={json.dumps([s.model_dump() for s in selected],ensure_ascii=False)}'
                  f'\n声音候选={json.dumps(evidence,ensure_ascii=False)}')
        feedback = ''
        for retry in range(cfg.repair_attempts+1):
            raw = llm.json('你是独立的影视内容与人物证据审核员，不读取人工答案。',prompt+feedback,[sheet])
            try:
                issues = assess_batch(raw,group,selected,len(times),evidence)
                break
            except ValueError as exc:
                write_json(folder/f'batch-{offset}-protocol-{retry}.json',{'raw':raw,'error':str(exc)})
                if retry == cfg.repair_attempts:
                    raise ValueError(f'自动审核批次{offset}格式或引用校验失败，未改写稿件：{str(exc)[:1200]}') from exc
                feedback = '\n上次审核响应未通过格式/引用检查：'+str(exc)[:1800]+'\n原响应：'+json.dumps(raw,ensure_ascii=False)+'\n请重审同一批units，未知仍须标unknown，不得为了修复格式把存疑内容放行。'
        record = {'units':group,'times':times,'raw':raw,'issues':issues}
        write_json(folder/f'batch-{offset}.json',record)
        return issues,str((folder/f'batch-{offset}.json').resolve()),{'path':str(sheet.resolve()),'sha256':file_hash(sheet)}
    # Batches are independent; keep results in source order and share LLM's
    # locked request allowance. No partial batch constitutes an approval.
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            for issues,batch,frame in pool.map(review_group,range(0,len(units),3)):
                all_issues.extend(issues)
                batches.append(batch)
                frames.append(frame)
        except BaseException:
            pool.shutdown(wait=True,cancel_futures=True)
            raise
    report = {'mode':'automatic_source_review','version':VERSION,'source':source_identity(video),
              'plan_digest':digest(plan.model_dump()),'transcript_digest':digest([s.model_dump() for s in lines]),
              'voice_digest':digest(voices),'passed':not all_issues,'issues':all_issues,'batches':batches,'frames':frames,
              'limitations':['机器审核可能误判；声纹候选不等于人工确定身份。']}
    write_json(folder/'report.json',report)
    write_json(root/'automatic-review.json',report)
    return report


def repair_plan(plan, issues, lines, voices, root, cfg, llm, attempt, *, source_seconds=None):
    """Repair one failed unit at a time, keeping validated edits and shot IDs."""
    revised = plan.model_copy(deep=True)
    accepted = []
    lookup = {s.id:s for s in lines}
    for uid in dict.fromkeys(issue['id'] for issue in issues):
        clip = revised.clips[0]
        targets = {u['id']:u for u in units_for_clip(clip)}
        targets.update({f'original-{i}':{'id':f'original-{i}',**b.model_dump()} for i,b in enumerate(clip.beats) if b.mode=='original'})
        if uid not in targets:
            raise ValueError('局部修订收到不存在的单位')
        target = targets[uid]
        if uid in {'title','angle','hook'}:
            eligible = {i for b in clip.beats for i in b.evidence_ids}
            fields = '当前只允许id、text两个字段；title须2–32字，angle/hook必须是当前选用片段能支持的内容。'
        else:
            index = int(uid.split('-')[1]);beat = clip.beats[index]
            shown = {i for b in clip.beats[:index] for i in b.evidence_ids
                     if i in lookup and lookup[i].start<b.end and lookup[i].end>b.start}
            teaser = set(clip.beats[0].evidence_ids) if cfg.opening=='original_teaser' and index==1 else set()
            eligible = {s.id for s in lines if (s.start<beat.end and evidence_matches(s,beat,shown)) or s.id in teaser}
            fields = ('当前只允许id/start/end/evidence_ids/reason；text字段禁止出现，即使是空字符串也不能返回。reason仅是内部剪辑说明。'
                      if uid.startswith('original-') else '当前只允许id/text/start/end/evidence_ids；text必须非空，不能改mode或reason。')
        prompt = ('只修当前target一个单位，返回changes且仅含这一项，id必须原样照抄；其他段落与原声保持。'
              '不要按字幕顺序重数段落，不要把后场旁白搬来替代当前段。'
              '只使用allowed_evidence_ids中列出的真实对白；原稿和内部reason都不是事实依据。'
              '证据不足就删除该断言或改为能被当前证据支持的说法。'
              '不能用泛称掩盖错误归属。不要新增情节，不为长度凑字。'
              '返回changes数组，当前项遵守以下字段约束：'+fields+
              '不能增加、删除或换序镜头，不能改变mode。beat-N的text必须是非空完整口播，不能用空字符串删除不确定断言；改成当前证据能支持的简洁表述。'
              '时间不得越过相邻镜头，原片区间1–40秒，不能把原声开场移到后面的旁白位置；保留整句原声。'
              f'\ntarget={json.dumps(target,ensure_ascii=False)}\nallowed_evidence_ids={sorted(eligible)}'
              f'\n问题{json.dumps([i for i in issues if i["id"]==uid],ensure_ascii=False)}\n完整稿件（仅供衔接）{revised.model_dump_json()}'
              f'\n可用字幕{json.dumps([s.model_dump() for s in lines if s.id in eligible],ensure_ascii=False)}'
              f'\n人物候选{json.dumps([r for r in voices.get("lines",[]) if r["line_id"] in eligible],ensure_ascii=False)}')
        feedback = ''
        for retry in range(cfg.repair_attempts+1):
            raw = llm.json('你是局部修稿编辑，只依据提供证据修复当前一个单位。',prompt+feedback)
            record = root/'automatic-repairs'/str(attempt)/uid/f'attempt-{retry}.json'
            try:
                changes = raw.get('changes') if isinstance(raw,dict) else None
                if not isinstance(changes,list) or len(changes)!=1 or not isinstance(changes[0],dict):
                    raise ValueError('单段修订必须恰好返回当前target的一项changes')
                if changes[0].get('id')!=uid:
                    raise ValueError(f'单段修订id必须是{uid}，不能换段')
                cited = changes[0].get('evidence_ids',[])
                if not isinstance(cited,list) or any(type(i) is not int for i in cited):
                    raise ValueError('修订evidence_ids必须是字幕整数编号列表')
                if not set(cited).issubset(eligible):
                    raise ValueError(f'当前单位引用了未允许证据；只能使用{sorted(eligible)}')
                candidate = apply_plan_changes(revised,raw,{uid})
                if source_seconds is not None:
                    validate_plan(candidate,lines,source_seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening)
                    for index,b in enumerate(candidate.clips[0].beats):
                        teaser = set(candidate.clips[0].beats[0].evidence_ids) if cfg.opening=='original_teaser' and index==1 else set()
                        if any(lookup[i].start>=b.end and i not in teaser for i in b.evidence_ids):
                            raise ValueError(f'镜头{index}修订引用了尚未播放的未来对白')
                        if b.mode=='original' and any(s.start+.2<cut<s.end-.2 for s in lines for cut in (b.start,b.end)):
                            raise ValueError(f'镜头{index}修订后的原声切点落在字幕句中')
                revised = candidate
                accepted.extend(changes)
                write_json(record,{'raw':raw,'status':'valid_patch'})
                break
            except ValueError as exc:
                write_json(record,{'raw':raw,'error':str(exc)})
                if retry == cfg.repair_attempts:
                    raise
                feedback = '\n上轮局部修订未通过程序检查：'+str(exc)[:1800]+'\n上轮修订：'+json.dumps(raw,ensure_ascii=False)+'\n只返回当前target的合法修改，不能换段、清空口播、变更mode或突破原片时间线。'
    write_json(root/f'automatic-repair-{attempt}.json',{'changes':accepted})
    return revised


def apply_plan_changes(plan, raw, allowed):
    """Apply one patch transaction; no partial or out-of-scope edits survive."""
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
            if set(row)-{'id','text','start','end','evidence_ids','reason'}:
                raise ValueError('镜头修订含未知字段')
            index = int(uid.split('-')[1])
            original = revised.clips[0].beats[index]
            if (uid.startswith('original-')) != (original.mode=='original'):
                raise ValueError('局部修订单位类型与镜头模式不一致')
            if uid.startswith('original-') and 'text' in row:
                raise ValueError('不能改写原声')
            if uid.startswith('beat-') and 'reason' in row:
                raise ValueError('旁白修订不能额外改写内部剪辑说明')
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
    if (report.get('version')!=VERSION or report.get('mode') != 'automatic_source_review' or report.get('passed') is not True or report.get('issues') != []
            or report.get('source') != source_identity(video) or report.get('plan_digest') != digest(plan.model_dump())
            or report.get('transcript_digest') != digest([s.model_dump() for s in lines])
            or report.get('voice_digest') != digest(voices) or not report.get('frames')):
        raise ValueError('自动审核记录与当前稿件、素材或声纹版本不一致')
    if any(file_hash(Path(f['path'])) != f['sha256'] for f in report['frames']):
        raise ValueError('自动审核画面已改变')
