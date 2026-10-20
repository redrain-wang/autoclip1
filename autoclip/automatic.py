"""Source-bound automated review and local repair, separate from human approval."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .identity import units_for_clip, file_hash
from .editorial import audience_script, prose_signals, require_oral_style, WRITING_RULES
from .ingest import contact_sheet, subtitle_evidence_sheet
from .models import Plan, Beat, validate_plan, evidence_matches
from .repair import RepairNeeded
from .util import digest, read_json, source_identity, write_json
from .voices import match_batch

VERSION = 'automatic-review-9'
METADATA_CAPTION_POLICY = 'metadata-captions-2'


def validate_voice_library(library, series):
    if not library.get('characters'):
        raise ValueError('缺少人工确认声纹库，不能进行人物核对后交付')
    if library.get('series') != series:
        raise ValueError('人工声纹库剧名与项目不一致')
    for person in library['characters']:
        if not person.get('samples'):
            raise ValueError('人工声纹参考没有确认样本')
        for sample in person['samples']:
            if sample.get('status') != 'human_confirmed' or source_identity(Path(sample['source']['path'])) != sample['source']:
                raise ValueError('人工声纹参考无效或原片已改变')


def link_voice_evidence(video, lines, library, manifest):
    """Rematch against the current human library; never trust old suggested names."""
    source = source_identity(video)
    source_ids = {s['id'] for s in manifest['sources'] if s['source'] == source}
    if not source_ids:
        raise ValueError('声纹候选没有绑定当前原片版本')
    validate_voice_library(library,manifest['series'])
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


class UnitReviews(BaseModel):
    model_config = ConfigDict(extra='forbid')
    units: list[UnitDecision]
    issues: list[UnitIssue]
    notes: list[UnitIssue] = Field(default_factory=list)


class ReviewBatch(UnitReviews):
    passed: StrictBool


class OriginalDecision(UnitDecision):
    identity_basis: Literal['no_identity_claim']
    subjects: list[str] = Field(default_factory=list,max_length=0)


class OriginalReviewBatch(UnitReviews):
    units: list[OriginalDecision]


class CaptionReading(BaseModel):
    model_config=ConfigDict(extra='forbid')
    line_id: int
    text: str


class CaptionReadings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    readings: list[CaptionReading] = Field(min_length=1,max_length=6)


def read_caption_observations(llm, sheet, line_ids):
    """Read only source pixels, without exposing ASR or the candidate narration."""
    raw=llm.json('你是原片字幕辨读员，只读图，不审稿、不识别人物、不补对白。',
        '按图上caption line标签，逐条抄录原片实际可见的字幕。标签ID和时间不是原片字幕。'
        '每条有两张原片图，若字幕不同，按先后用 / 分隔；无字或看不清返回空字符串。'
        '不凭剧情记忆、发音或常识补字。仅返回JSON readings数组，每项只有line_id和text，'
        '必须恰好覆盖这些line_id：'+json.dumps(line_ids),[sheet])
    parsed=CaptionReadings.model_validate(raw)
    if len(parsed.readings)!=len(line_ids) or {r.line_id for r in parsed.readings}!=set(line_ids):
        raise ValueError('原片字幕辨读未精确覆盖给定位置')
    return parsed.model_dump()['readings']


def review_transcript_rows(lines):
    return [{'id':s.id,'start':s.start,'end':s.end,'asr_text':s.text,'confidence':s.confidence} for s in lines]


def review_voice_rows(rows):
    """Keep actual accepted names and uncertainty; ranks below the gate aren't identities."""
    return [{**{k:v for k,v in row.items() if k!='samples'},
             'samples':[{k:v for k,v in sample.items() if k!='candidates'} for sample in row.get('samples',[])]}
            for row in rows]


def reference_names(name):
    """Allow the human label before a trailing parenthesis, never invent aliases."""
    names={name} if name else set()
    if name and (match:=re.fullmatch(r'([^（）()]+)[（(][^（）()]+[）)]',name)):
        names.add(match[1].strip())
    return names


def caption_lines_for_issues(issues, lines, fallback_ids):
    """Prioritize the actual subtitle IDs disputed by the reviewer, not just endpoints."""
    lookup = {s.id:s for s in lines}
    ids = []
    for issue in issues:
        for first,last in re.findall(r'(?:字幕|line|ID)\s*(\d+)(?:\s*[-–/、]\s*(\d+))?',issue['message']):
            a,b = int(first),int(last or first)
            ids.extend(range(min(a,b),max(a,b)+1) if abs(b-a)<=6 else [a,b])
    ids.extend(fallback_ids)
    chosen = list(dict.fromkeys(i for i in ids if i in lookup))[:6]
    if not chosen and lines:
        chosen = [lines[i].id for i in sorted({0,len(lines)//2,len(lines)-1})]
    return [lookup[i] for i in chosen]


def metadata_caption_lines(units, lines):
    """Pick literal source phrases from metadata, including plot-changing ASR words."""
    def grams(text):
        chunks=re.findall(r'[\u4e00-\u9fffA-Za-z0-9]+',text)
        return {chunk[i:i+n] for chunk in chunks for n in range(2,6) for i in range(len(chunk)-n+1)}
    terms=grams('，'.join(u['text'] for u in units))
    rows=[(s,grams(s.text)&terms) for s in lines]
    frequencies={term:sum(term in words for _,words in rows) for term in terms}
    def score(row):
        s,words=row
        return sum(len(t)**2/max(1,frequencies[t]) for t in words)/max(1,len(s.text)**.5)
    ranked=sorted(rows,key=lambda row:(-score(row),row[0].id))[:6]
    return [s for s,_ in ranked]


def required_note_repairs(notes, units):
    """A required source correction cannot be hidden under a supported verdict."""
    issues=[]
    for note in notes:
        message=note.message
        affected=set()
        # Scope instructions to their clause: an ASR observation followed by
        # "the narration is correct; do not change it" is not a script repair.
        for clause in re.split(r'[，,。；;\n]',message):
            if (re.search(r'ASR|转写',clause,re.IGNORECASE)
                    and not re.search(r'旁白|口播|正文|稿件|标题|角度|钩子',clause)):
                continue
            directives=re.sub(r'(?:无[需须]|不(?:必|需(?:要)?|用|应|须))[^，,。；;]{0,30}?'
                r'(?:修正|改为|改成|更正|修改|修订)|已(?:经)?(?:修正|更正|修改)','',clause)
            instruction=re.search(r'(?:应|必须|需要|须)[^，,。；;]{0,50}(?:修正|改为|改成|更正|修改)',directives)
            if not instruction:continue
            quoted=re.findall(r'[“「"]([^”」"]{2,80})[”」"]',directives[:instruction.start()])
            targets=[u['id'] for u in units if any(word in u['text'] for word in quoted)]
            affected.update(targets or [note.id])
        issues.extend({'id':u['id'],'message':message} for u in units if u['id'] in affected)
        if affected-set(u['id'] for u in units):
            issues.extend({'id':uid,'message':message} for uid in sorted(affected-set(u['id'] for u in units)))
    return issues


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
    for issue in required_note_repairs(parsed.notes,units):
        if issue['id'] not in expected:raise ValueError('审核备注要求修订本批次以外的单位')
        issues.append(issue)
    for unit in parsed.units:
        if not set(unit.evidence_ids).issubset(lookup):
            raise ValueError(f'{unit.id} 引用了未提供的字幕编号：{sorted(set(unit.evidence_ids)-set(lookup))}')
        if any(i<0 or i>=frame_count for i in unit.frame_ids):
            raise ValueError(f'{unit.id} 引用了未提供的主画面编号；frame_ids只能为{list(range(frame_count))}，'
                '字幕放大图不另编号，不能把其格子追加到主图编号后。请按主图times重核对应关系。')
        target = expected[unit.id]
        if target.get('mode')=='original' and (unit.identity_basis!='no_identity_claim' or unit.subjects):
            raise ValueError(f'{unit.id} 是没有新增身份主张的原声；只核对剪辑，不能因说话人姓名未知改写原声')
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
            candidate_names = {name for r in associated for s in r.get('samples',[])
                               for name in reference_names(s.get('name'))}
            if not any(r.get('status') == 'candidate' for r in associated) or any(r.get('status') == 'conflict' for r in associated):
                issues.append({'id':unit.id,'message':'声音候选缺失或冲突，须用明确称呼与回应核实或删除不确定归属'})
            if not unit.subjects or not set(unit.subjects).issubset(candidate_names):
                issues.append({'id':unit.id,'message':'声音依据未支持本条主张中的人物，不能借其他人的候选放行'})
    if not parsed.passed and not issues:
        raise ValueError('自动审核声称未通过但未指出任何具体单位问题')
    return issues


def review_citation_limits(units, lines, voice_rows):
    """Explain the existing per-unit gates without inventing identity evidence."""
    result=[]
    for unit in units:
        allowed={s.id for s in lines if 'end' not in unit or s.start<unit['end']
                 or s.id in unit.get('shown_evidence',[])}
        result.append({'id':unit['id'],'end_exclusive':unit.get('end'),
            'allowed_evidence_ids':sorted(allowed),
            'future_evidence_ids':[s.id for s in lines if s.id not in allowed],
            'voice_candidates':[] if unit.get('mode')=='original' else [
                {'line_id':r['line_id'],'names':sorted({name for s in r.get('samples',[])
                    for name in reference_names(s.get('name'))})}
                for r in voice_rows if r['line_id'] in allowed and r.get('status')=='candidate']})
    return result


def review_recheck_reason(issues, units):
    """Request a bounded source recheck, never silently dismiss a failed unit."""
    original={u['id'] for u in units if u.get('mode')=='original'}
    text_by_id={u['id']:u.get('text','') for u in units}
    normalize=lambda text:re.sub(r'[\s，,。；;：:！？!?“”「」"、]','',text)
    reasons=[]
    for issue in issues:
        message=issue['message']
        alleged=re.findall(r'(?:旁白|口播|正文|稿件)(?:却误|却|误|竟|仍)?'
            r'(?:写成|写为|称为|说成|使用了?|用了?)[“「"]([^”」"]+)[”」"]',message)
        if any(normalize(word) not in normalize(text_by_id.get(issue['id'],'')) for word in alleged):
            reasons.append('审核引述的错误措辞不在该单位实际口播中。该单位实际text为'
                +json.dumps(text_by_id.get(issue['id'],''),ensure_ascii=False)
                +'；先逐字核对待审稿，再比较原片，不能改写不存在的文字。其他真实人物或事实问题仍须指出。')
        if re.search(r'“([^”]+)”(?:而非|而不是|不是)“\1”',message):
            reasons.append('审核把相同文字说成不同内容；请核对原片与旁白的实际差异，允许准确同义概括。')
        if re.search(r'(?:字幕|放大图|画面).{0,20}标注.{0,12}(?:说话者|人物|为|是)',message):
            reasons.append('抽帧图标签仅有时间和对白编号，没有说话人姓名答案；字幕中的姓名可能是被称呼者，不能自动认作发言者。请重核实际称呼与回应。')
        if issue['id'] in original and re.search(r'无字幕|字幕[^。]{0,35}间隔|ASR漏记|ASR覆盖不足',message):
            reasons.append('该original单位是原片start到end完整连续音画，内部没有删剪，无字幕间隔也原样保留。请核对实际边界及跨单位衔接；不能仅凭内部停顿或原剧镜头切换推断本系统丢失原声。确有边界或衔接问题仍须拒绝。')
    return list(dict.fromkeys(reasons))


def audit_plan(video, plan, lines, voices, root, cfg, llm):
    if not cfg.vision:
        raise ValueError('自动长片审核需要画面，不能静默跳过')
    if voices['source'] != source_identity(video) or voices['transcript_digest'] != digest([s.model_dump() for s in lines]):
        raise ValueError('人物证据与当前素材或字幕不一致')
    clip = plan.clips[0]
    units = units_for_clip(clip)
    # Original footage is audited too: an edit may otherwise invent a response.
    units += [{'id':f'original-{i}', 'mode':'original', 'source_continuity':'single_uncut_interval',
               'text':'连续原声：核对对白切点、问答顺序与场景承接', 'start':b.start, 'end':b.end,
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
    # Keep the exact reviewed draft even when later measurement overwrites the
    # mutable plan-timed.json or a subsequent review is interrupted.
    write_json(folder/'plan.json',plan.model_dump())
    write_json(root/'automatic-review-state.json',{'status':'running','plan_digest':digest(plan.model_dump()),
        'review_root':str(folder.resolve())})
    all_issues=[{'id':f'beat-{row["beat"]}','message':'旁白混入制作或审查话术，必须面向观众直接讲剧情，不能照读核对记录：'
        +json.dumps(row['phrases'],ensure_ascii=False)} for row in prose_signals(clip)]
    batches, frames = [], []
    groups = []
    offset = 0
    for subset in ([u for u in units if u.get('mode')!='original'],[u for u in units if u.get('mode')=='original']):
        for start in range(0,len(subset),3):
            group = subset[start:start+3]
            groups.append((offset,group))
            offset += len(group)
    def review_group(job):
        offset,group = job
        original = all(u.get('mode')=='original' for u in group)
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
        evidence = review_voice_rows([r for r in voices['lines'] if r['line_id'] in {s.id for s in selected}]) if not original else []
        prompt = ('逐项核对文本事实、人物行为归属、画面相关性、连续原声切点、场景承接和重复解释。'
                  '旁白必须面向观众讲剧情，不能出现字幕里说、根据字幕、镜头里有人等制作核对话术；'
                  '不能只因与转写字面一致就放行，不成句的残片或明显同音错词须按原片核实并指出问题。'
                  '字幕字段中的asr_text只是机器转写，不是原片实际字幕，可能有错字和错断句。'
                  '旁白与原片图一致、仅与asr_text不同，不是旁白错误；转写疑点只能写notes，不能要求修改正确旁白来迎合错误转写。'
                  '这是剧情解说，不是逐字复读校对；允许同义概括、略去语气词和压缩正常停顿，不因没有逐字照读就判错。'
                  '旁白须自然地用第三人称讲述，不能把角色自称侄儿、朕或称呼您的裸台词直接当成解说。必要引用先交代发言者，不能让配音者冒充剧中人。'
                  '有清楚自称、称呼和连续问答时，可用这位晚辈、来求差事的人等有来源的角色关系叙述，不要求每个行为者先识别到姓名。'
                  '不能因同一模型写稿就默认正确。声纹是候选；画面人物可能是听者。'
                  '声纹有冲突时只能以明确叫名与连续问答核实，否则unknown；不能凭剧名记忆认人。'
                  '身份依据分为无身份主张、声纹加语境、明确称呼回应、依据不足。泛称仍可能有身份主张。'
                  'subjects列出涉及身份归属的实际人物名；声纹加语境必须列出与当前候选一致的名字。'
                  '区分说话人、被称呼者和被提及者。“四爷和十三爷被叫住”没有指认喊话人，不能因喊话人声纹未知否定被称呼者。用清楚称呼/回应核实相应角色，不能混淆主客体。'
                  '没有姓名、关系或特定人物暗示的中性叙述（例如当前确实有人说“记住”），若字幕及画面支持，可用no_identity_claim；不要求查出这位说话人的姓名。仍禁止用泛称掩盖错误的特定人物行为。'
                  '不得把承诺写成结果，不能预支当前镜头后文；shown_evidence是此单位之前观众已看到的对白，可支持承接与回顾。分析须与事实分开。'
                  'original单位只有原片本身的连续声音与画面，没有新增旁白或人物标签；只核对切点是否破坏完整对白、伪造问答因果。'
                  '原声本身没有新增身份主张时identity_basis=no_identity_claim、subjects=[]。'
                  'ASR可能有同音或繁简错字，应区别转写疑点与实质事实错误；他/她同音，不能据ASR字形断言原声所指性别错误。'
                  '旁白和标题中的人物归属仍必须有依据；用清楚的关系或行动描述人物可以，不能用含糊泛称掩盖错误行为归属。'
                  'title/angle/hook根据全片选用证据核查。剧情优先是长期规则：先讲清人物、处境、事件起因、行动、结果与场景承接，必要时才分析。'
                  '对照viewer_timeline检查是否遗漏理解当前情节所必需的信息、因果或转场，是否以观点替代必要剧情；实际缺失绑定受影响单位列issues。'
                  '简短承接或纯粹交代行动是合法口播，不因某段没有观点要求扩写。不能为了时长重复或删掉关键承接。'
                  '只审核本批units；不要求其他批次的标题或镜头出现在这里。每个问题都写具体id，不把单段问题复制给所有单位。'
                  '时间区间包含start、不包含end；end恰好等于下一条字幕start并未剪入该字幕，不得据此误判截断或预支。'
                  '图片编号按times顺序从0开始。证据必须来自给定字幕；每个单位至少引用一条字幕及一幅画面。'
                  '任何未知或错误均逐项标unknown/contradicted，issues每项只含id和message；不要直接修稿。不返回passed总开关，整批是否通过由程序按逐项结论和问题汇总。'
                  'issues只列真正需要修复的错误，已支持的说明、同音疑点已排除、无需认定身份等观察写notes，不列issues。'
                  'viewer_timeline提供实际前后剪辑稿；shown_evidence列此前选用的证据，不等于这些对白全部以原声播放，须对照mode和实际旁白text。不得预支后文。'
                  '回顾上一镜头已经发生的事情不要求该事情在当前画面再发生一次；须有此前证据并自然承接，不能把回顾误判成预支未来。'
                  f'\n格式{json.dumps(UnitReviews.model_json_schema(),ensure_ascii=False)}'
                  f'\nunits={json.dumps(group,ensure_ascii=False)}\ntimes={times}'
                  f'\n字幕={json.dumps(review_transcript_rows(selected),ensure_ascii=False)}'
                  f'\n声音候选={json.dumps(evidence,ensure_ascii=False)}'
                  f'\nviewer_timeline={json.dumps(audience_script(clip),ensure_ascii=False)}')
        if original:
            prompt = ('只审核本批连续原声的剪辑：切点是否切断完整对白、是否因剪接伪造问答因果或丢失必要承接。'
                '这些单位没有新增旁白、姓名标签或角色署名，text只是待核事项，不是观众会听到的附加断言。'
                '不要求先认出演员或说话人姓名；姓名未知本身不是剪辑错误，不据此标unknown。identity_basis固定no_identity_claim、subjects固定空数组。'
                'source_continuity=single_uncut_interval是渲染约束：start到end内保留原片全部连续音画，没有本系统内部删剪；字幕空白间隔内的声音及画面也完整保留，不能仅因字幕间有停顿就怀疑本系统剪掉了对白。只检查本系统选取的边界与跨单位承接，不重审原剧导演本身的剪接。'
                '若切点、问答或承接本身不清楚仍须unknown；实质剪辑问题须拒绝，不能因没有身份主张就默认支持。'
                'ASR同音字形不等于实际对白错误；区间含start、不含end，end等于下条字幕start没有剪入下条对白。'
                'issues仅列实际待修问题，已排除的疑点与解释放notes。每项结论必须引用提供的字幕与图片编号，图片从0编号。'
                '先对照viewer_timeline中的实际相邻原声和旁白，再判断缺失承接；当前单位evidence_ids不是全部所提供的上下文。原声听到字幕内容，旁白听到text，不能混为一谈。'
                '不要返回passed总开关；程序按每个单位的verdict及issues汇总。未知或实际错误仍须逐项拒绝。'
                f'\n格式{json.dumps(OriginalReviewBatch.model_json_schema(),ensure_ascii=False)}'
                f'\nunits={json.dumps(group,ensure_ascii=False)}\ntimes={times}'
                f'\n字幕={json.dumps(review_transcript_rows(selected),ensure_ascii=False)}'
                f'\nviewer_timeline={json.dumps(audience_script(clip),ensure_ascii=False)}')
        feedback = ''
        review_images = [sheet]
        caption_sheet = None
        caption_observations=None
        metadata=any('start' not in u for u in group)
        if metadata:
            caption_lines=metadata_caption_lines(group,selected)
            if not caption_lines:raise ValueError('标题、角度与钩子缺少可核对的原片字幕')
            caption_sheet=subtitle_evidence_sheet(video,folder/f'batch-{offset}-metadata-captions',caption_lines,'metadata')
            review_images.append(caption_sheet)
            prompt+=('\n另附按标题、角度、钩子用词定位的原片字幕放大图。ASR只是识别稿，必须逐字对照实际可见字幕，'
                '尤其核对人名、拆字、同音字和改变剧情含义的词。实际字幕与ASR冲突，以清晰可见原片为准；'
                '不得照抄ASR把疑点认定为事实，看不清仍标unknown。frame_ids仍使用主图times索引。')
        elif not original:
            caption_lines=[]
            for unit in group:
                cited=[s for s in selected if s.id in unit.get('evidence_ids',[])]
                caption_lines.extend(metadata_caption_lines([unit],cited)[:2])
            caption_lines=list({s.id:s for s in caption_lines}.values())[:6]
            if caption_lines:
                caption_sheet=subtitle_evidence_sheet(video,folder/f'batch-{offset}-narration-captions',caption_lines,'narration')
                review_images.append(caption_sheet)
                prompt+=('\n另附本批旁白引用的原片字幕放大图。核对官名、人名、同音字和残句；转写稿不是正确文字的保证。'
                    '还须按完整稿件判断自然听感与剧情承接，不能把制作核对话术当成合格解说。frame_ids仍使用主图times索引。')
        rejected_record = None
        for retry in range(cfg.repair_attempts+1):
            raw = llm.json('你是独立的影视内容与人物证据审核员，不读取人工答案。',prompt+feedback,review_images)
            try:
                decisions = (OriginalReviewBatch if original else UnitReviews).model_validate(raw)
                checked = decisions.model_dump()
                checked['passed'] = not decisions.issues and all(u.verdict=='supported' for u in decisions.units)
                issues = assess_batch(checked,group,selected,len(times),evidence)
                if issues:
                    rejected_record=(raw,issues)
                recheck=review_recheck_reason(issues,group)
                if recheck and retry<cfg.repair_attempts:
                    write_json(folder/f'batch-{offset}-source-recheck-{retry}.json',{'raw':raw,'issues':issues})
                    feedback=('\n先核对审核本身，不修改稿件：'+'；'.join(recheck)
                        +'\n上一轮审核：'+json.dumps(raw,ensure_ascii=False)
                        +'\n请重新给出本批全部units与真实问题；不能为了通过而忽略仍然未知或错误的内容。')
                    continue
                if (not original and retry<cfg.repair_attempts and caption_sheet is not None
                        and caption_observations is None
                        and any(re.search(r'ASR|转写|同音|错字|错词|残句',i['message']) for i in issues)):
                    caption_observations=read_caption_observations(llm,caption_sheet,[s.id for s in caption_lines])
                    write_json(folder/f'batch-{offset}-caption-readings.json',{'readings':caption_observations,
                        'image':str(caption_sheet.resolve()),'image_sha256':file_hash(caption_sheet)})
                    write_json(folder/f'batch-{offset}-source-recheck-{retry}.json',{'raw':raw,'issues':issues})
                    feedback=('\n上一轮混淆原片与机器转写，先重核审核自身，不先改稿。'
                        '这是另一个只看原片图、未看到转写和稿件的字幕辨读结果，仍须对图复核：'
                        +json.dumps(caption_observations,ensure_ascii=False)
                        +'。原片图与旁白相符时应supported；不得因原始ASR有错而拒绝正确旁白。'
                        '抽样图不含某句话不等于原片没说；按实际可见文字修正错字，不补未知事实。'
                        '只将当前稿件实际存在的事实、人物、剪辑或听感问题列issues；允许准确的同义概括。'
                        '\n上轮审核：'+json.dumps(raw,ensure_ascii=False))
                    continue
                # A reviewer cannot claim voice support that its own cited
                # candidates do not supply. Recheck the review annotation before
                # asking the writer to "fix" an otherwise unchanged sentence.
                supported = {u.id for u in decisions.units if u.verdict=='supported'}
                inconsistent = [i for i in issues if i['id'] in supported and i['message'].startswith((
                    '声音候选缺失或冲突','声音依据未支持','列出了人物归属却声明','明确称呼与回应需至少'))]
                caption_dispute = any(re.search(r'ASR|同音|错字|转写|字形|拆字|文字|看不清|字幕.*(?:不一致|不符|错误|不足|不完整)',i['message']) for i in issues)
                if retry<cfg.repair_attempts and caption_sheet is None and (inconsistent or caption_dispute):
                    zoom_ids = set()
                    for unit in group:
                        cited = [i for i in unit.get('evidence_ids',[]) if i in ids]
                        if cited:zoom_ids.update((cited[0],cited[-1]))
                    zoom_lines = caption_lines_for_issues(issues,selected,sorted(zoom_ids))
                    if zoom_lines:
                        caption_sheet = subtitle_evidence_sheet(video,folder/f'batch-{offset}-caption-images',zoom_lines,'captions')
                        review_images.append(caption_sheet)
                        raise ValueError('本批存在身份依据或转写字词争议，已追加原片两帧字幕放大图。'
                            '只读实际可见字幕，不凭记忆补字，不把ASR同音字直接认定为新人物或事实错误；'
                            'frame_ids仍使用主画面times索引，字幕图仅补文字证据。人物归属仍须核对称呼、回应、声音候选与实际前后稿，'
                            '原片也不清楚则仍须unknown，不得为了结束修复放行。')
                if inconsistent and retry<cfg.repair_attempts:
                    raise ValueError('审核自身的身份依据与所引证据不一致：'+json.dumps(inconsistent,ensure_ascii=False)
                        +'。先重核说话人、听者、被提及者以及实际称呼和回应，不能仅凭反应画面断定发言人；'
                        'voice_and_context要求subjects中每个人名都与本次声音候选一致，被点名者不是因此就有声音匹配。'
                        '皇帝、老臣等称谓也不能由程序凭剧名自动等同于库内姓名。'
                        '如果实际依据是明确点名、称呼、自称和紧接的回应或行动，应引用至少两条不同对白证据，'
                        '用explicit_address_response逐一说明每个subjects如何被核实；不是只换标签，必须指出实际证据。'
                        '没有这些依据就标unknown/contradicted，不能脑补姓名或用他人的声音放行。')
                break
            except ValueError as exc:
                write_json(folder/f'batch-{offset}-protocol-{retry}.json',{'raw':raw,'error':str(exc)})
                if retry == cfg.repair_attempts:
                    if rejected_record is not None:
                        # Preserve a structurally valid rejection if a later
                        # recheck is malformed. This can never create approval.
                        raw,issues=rejected_record
                        write_json(folder/f'batch-{offset}-recheck-failed.json',
                            {'error':str(exc),'action':'retain_last_valid_rejection'})
                        break
                    raise ValueError(f'自动审核批次{offset}格式或引用校验失败，未改写稿件：{str(exc)[:1200]}') from exc
                feedback = ('\n上次审核响应未通过格式/引用检查：'+str(exc)[:1800]
                    +'\n原响应：'+json.dumps(raw,ensure_ascii=False)
                    +'\n逐项引用边界='+json.dumps(review_citation_limits(group,selected,evidence),ensure_ascii=False)
                    +'\n每项supported只能引用该项allowed_evidence_ids，不能借用同批较后镜头的证据。'
                    'voice_and_context必须引用列有相应姓名的voice_candidates，且行为与语境相符；'
                    '人物姓名本身就是身份主张，不能声称“未点名”来绕过。没有声音候选时，'
                    '只有真实明确称呼及回应可使用explicit_address_response；没有这种证据就标unknown并说明缺口。'
                    'units必须是对象数组，每项完整包含id/verdict/reason/evidence_ids/frame_ids/identity_basis/subjects，'
                    '不能以单位ID字符串代替对象；顶层必须包含issues数组，无附加问题时返回空数组。'
                    '请重审同一批units，不要直接修改稿件，不得为了修复格式把存疑内容放行。')
        record = {'units':group,'times':times,'raw':raw,'issues':issues}
        write_json(folder/f'batch-{offset}.json',record)
        frames = [{'path':str(p.resolve()),'sha256':file_hash(p),
            **({'purpose':'metadata_captions'} if metadata and p==caption_sheet else {})} for p in review_images]
        return issues,str((folder/f'batch-{offset}.json').resolve()),frames
    # Batches are independent; keep results in source order and share LLM's
    # locked request allowance. No partial batch constitutes an approval.
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            for issues,batch,frame in pool.map(review_group,groups):
                all_issues.extend(issues)
                batches.append(batch)
                frames.extend(frame)
        except BaseException:
            pool.shutdown(wait=True,cancel_futures=True)
            raise
    report = {'mode':'automatic_source_review','version':VERSION,'metadata_caption_policy':METADATA_CAPTION_POLICY,'source':source_identity(video),
              'plan_digest':digest(plan.model_dump()),'transcript_digest':digest([s.model_dump() for s in lines]),
              'voice_digest':digest(voices),'passed':not all_issues,'issues':all_issues,'batches':batches,'frames':frames,
              'limitations':['机器审核可能误判；声纹候选不等于人工确定身份。']}
    write_json(folder/'report.json',report)
    write_json(root/'automatic-review.json',report)
    write_json(root/'automatic-review-state.json',{'status':'completed','plan_digest':report['plan_digest'],
        'review_root':str(folder.resolve())})
    return report


def retain_short_source_audio(plan, uid, lines, cfg, source_seconds):
    """A failed short bridge may keep real audio, never invented replacement speech."""
    if source_seconds is None or not uid.startswith('beat-'):
        return None
    index = int(uid.split('-')[1])
    before = plan.clips[0].beats[index]
    if before.mode!='narration' or not 1<=before.end-before.start<=8:
        return None
    if any(s.start+.001<cut<s.end-.001 for s in lines for cut in (before.start,before.end)):
        return None
    ids = [s.id for s in lines if s.start>=before.start-.001 and s.end<=before.end+.001]
    if not ids:
        return None
    revised = plan.model_copy(deep=True)
    revised.clips[0].beats[index] = Beat(mode='original',start=before.start,end=before.end,
        text='',evidence_ids=ids,reason='短承接修稿失败，保留当前区间完整真实对白；仍须重新核对剪辑和全稿')
    try:
        validate_plan(revised,lines,source_seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening)
    except ValueError:
        return None
    return revised


def restore_original_prefix(plan, uid, lines, cfg, source_seconds):
    """Restore a cited leading phrase covered by the immediately preceding bridge."""
    if not uid.startswith('original-') or source_seconds is None:
        return None
    index = int(uid.split('-')[1])
    if index==0:
        return None
    before,current = plan.clips[0].beats[index-1:index+1]
    if before.mode!='narration' or abs(before.end-current.start)>.04:
        return None
    if not any(s.id in current.evidence_ids and before.start<=s.start<s.end<=current.start+.001 for s in lines):
        return None
    return retain_short_source_audio(plan,f'beat-{index-1}',lines,cfg,source_seconds)


def repeated_original_issue(plan, uid, root, attempt):
    index=int(uid.split('-')[1])
    current=plan.clips[0].beats[index]
    for number in range(attempt):
        issue_path,plan_path=root/f'issues-{number}.json',root/f'plan-attempt-{number}.json'
        if not issue_path.exists() or not plan_path.exists():continue
        if not any(i['id']==uid for i in read_json(issue_path)):continue
        prior=Plan.model_validate(read_json(plan_path)).clips[0].beats
        if index>=len(prior) or prior[index].mode!='original':continue
        overlap=max(0,min(current.end,prior[index].end)-max(current.start,prior[index].start))
        if overlap>=.8*max(current.end-current.start,prior[index].end-prior[index].start):return True
    return False


def replan_original_window(plan, uid, issues, lines, root, cfg, llm, attempt, retry, source_seconds):
    from .draft import repair_draft
    from .director import compile_plan
    index=int(uid.split('-')[1])
    bounds=(plan.clips[0].beats[0].start,plan.clips[0].beats[-1].end)
    report={'clip':plan.clips[0].model_dump(),'issues':[{'code':'semantic_review',
        'message':f'clips[0].beats[{index}] '+i['message']} for i in issues if i['id']==uid]}
    raw=repair_draft(plan.model_dump(),report,lines,bounds,cfg,llm,
        root/'automatic-repairs'/str(attempt)/uid,digest(report),retry)
    candidate=compile_plan(raw,lines,bounds,fit_narration=False)
    validate_plan(candidate,lines,source_seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening)
    lookup={s.id:s for s in lines}
    if any(lookup[i].start>=b.end for b in candidate.clips[0].beats if b.mode=='narration' for i in b.evidence_ids):
        raise ValueError('局部重编引用了尚未播放的未来对白')
    return candidate


def repair_plan(plan, issues, lines, voices, root, cfg, llm, attempt, *, source_seconds=None):
    """Repair one failed unit at a time, keeping validated edits and shot IDs."""
    revised = plan.model_copy(deep=True)
    accepted = []
    failed = []
    lookup = {s.id:s for s in lines}
    for uid in dict.fromkeys(issue['id'] for issue in issues):
        clip = revised.clips[0]
        targets = {u['id']:u for u in units_for_clip(clip)}
        targets.update({f'original-{i}':{'id':f'original-{i}',**b.model_dump()} for i,b in enumerate(clip.beats) if b.mode=='original'})
        if uid not in targets:
            raise ValueError('局部修订收到不存在的单位')
        target = targets[uid]
        if (uid.startswith('original-') and cfg.duration_profile=='short' and source_seconds is not None
                and repeated_original_issue(revised,uid,root,attempt)):
            try:
                candidate=replan_original_window(revised,uid,issues,lines,root,cfg,llm,attempt,0,source_seconds)
                write_json(root/f'automatic-repair-{attempt}.json',{'changes':accepted,'structural_unit':uid,
                    'plan':candidate.model_dump(),'status':'requires_full_measurement_and_review'})
                return candidate
            except ValueError as exc:
                write_json(root/'automatic-repairs'/str(attempt)/uid/'window-error.json',{'error':str(exc)})
        max_characters = None
        measured = next((i.get('measurement') for i in issues if i['id']==uid and i.get('measurement')),None)
        if uid.startswith('beat-') and measured and measured.get('voice_seconds',0)>0:
            from .budget import spoken_characters
            available = (target['end']-target['start'])*(1.4 if cfg.target_seconds>=150 else 1.28)-.35
            if available < measured['voice_seconds']:
                current_chars = spoken_characters(target['text'])
                max_characters = max(5,min(current_chars-1,int(current_chars*available/measured['voice_seconds']*.92)))
        restored = restore_original_prefix(revised,uid,lines,cfg,source_seconds)
        if restored is not None:
            index = int(uid.split('-')[1])-1
            change = {'id':uid,'operation':'restore_original_prefix','affected_beat':index,
                'before':revised.clips[0].beats[index].model_dump(),
                'after':restored.clips[0].beats[index].model_dump(),'status':'requires_full_review'}
            write_json(root/'automatic-repairs'/str(attempt)/uid/'source-context-fallback.json',change)
            revised = restored
            accepted.append(change)
            continue
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
              f'\n可用字幕（asr_text为未核实机器转写，实际字词以附图为准）{json.dumps(review_transcript_rows([s for s in lines if s.id in eligible]),ensure_ascii=False)}'
              f'\n人物候选{json.dumps(review_voice_rows([r for r in voices.get("lines",[]) if r["line_id"] in eligible]),ensure_ascii=False)}')
        prompt+='\n面向观众的写作要求：'+WRITING_RULES
        if max_characters is not None:
            prompt += (f'\n本次为真实配音超出画面余量的缩写：text有效字数必须不超过{max_characters}字（不含标点）。'
                       '必须实质删掉重复复述，只保留必要承接或必要事实，不能仅换同义词、添语气词或照抄。'
                       '压缩不得歪曲事实或删除必要限定；后续仍重新配音和独立审核。')
        repair_images = []
        source = voices.get('source',{})
        if max_characters is None and cfg.vision and source.get('path') and Path(source['path']).is_file():
            # Give the writer the same primary evidence, not only another
            # model's description of an ASR/OCR discrepancy.
            available_lines = [s for s in lines if s.id in eligible]
            evidence_ids = target.get('evidence_ids',[])
            fallback_ids = evidence_ids[:1]+evidence_ids[-1:]
            zoom_lines = caption_lines_for_issues([i for i in issues if i['id']==uid],available_lines,fallback_ids)
            image_root = root/'automatic-repairs'/str(attempt)/uid/'source-images'
            if zoom_lines:
                repair_images.append(subtitle_evidence_sheet(Path(source['path']),image_root,zoom_lines,'captions'))
                prompt += ('\n附图是实际原片字幕放大图，按原片可见文字核对ASR同音字，不盲从ASR或上轮审核意见，'
                           '不把短促呼喝误识当作新人物姓名；看不清不得补字。身份仍依当前证据与称呼回应，不能猜。')
            if 'start' in target:
                a,b=target['start'],target['end']
                times=[round(t,3) for t in (a+.1,(a+b)/2,b-.1)]
                repair_images.append(contact_sheet(Path(source['path']),image_root/'selected',times,'selected'))
                prompt += ('\n最后一张图是当前实际剪入区间的首、中、尾画面，时间为'+json.dumps(times)
                    +'。它与引用对白的字幕图可能处于不同时间：此前对白可支持自然回顾，'
                    '不能据此前字幕图虚构当前画面中的人物、道具或动作。修复画面不符时必须对照这张图，'
                    '删除无据细节，直接交代有证据的剧情；不要把画面核对过程写进口播。')
        feedback = ''
        for retry in range(cfg.repair_attempts+1):
            raw = (llm.json('你是局部修稿编辑，只依据提供证据修复当前一个单位。',prompt+feedback,repair_images)
                   if repair_images else llm.json('你是局部修稿编辑，只依据提供证据修复当前一个单位。',prompt+feedback))
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
                if uid.startswith('beat-'):
                    index=int(uid.split('-')[1])
                    if signals:=[s for s in prose_signals(candidate.clips[0]) if s['beat']==index]:
                        raise ValueError('当前旁白仍混入制作或审查话术，不能进入配音：'+json.dumps(signals,ensure_ascii=False))
                if max_characters is not None and spoken_characters(candidate.clips[0].beats[int(uid.split('-')[1])].text)>max_characters:
                    raise ValueError(f'实测时长修复没有落实缩写：当前口播必须不超过{max_characters}个有效字')
                if source_seconds is not None:
                    validate_plan(candidate,lines,source_seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening)
                    for index,b in enumerate(candidate.clips[0].beats):
                        if uid not in {f'beat-{index}',f'original-{index}'}:
                            continue  # untouched units still undergo the full source audit
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
                    fallback = retain_short_source_audio(revised,uid,lines,cfg,source_seconds)
                    if fallback is not None:
                        index = int(uid.split('-')[1])
                        change = {'id':uid,'operation':'retain_source_audio',
                            'before':revised.clips[0].beats[index].model_dump(),
                            'after':fallback.clips[0].beats[index].model_dump(),
                            'status':'requires_full_review'}
                        write_json(record.with_name('source-audio-fallback.json'),change)
                        revised = fallback
                        accepted.append(change)
                        break
                    if uid.startswith('original-') and cfg.duration_profile=='short' and source_seconds is not None:
                        # A cut may need room occupied by the next bridge. A
                        # one-shot patch cannot repair that; replan only its
                        # neighboring window and then remeasure/review all units.
                        try:
                            candidate = replan_original_window(revised,uid,issues,lines,root,cfg,llm,attempt,retry,source_seconds)
                            write_json(root/f'automatic-repair-{attempt}.json',{'changes':accepted,
                                'structural_unit':uid,'plan':candidate.model_dump(),'status':'requires_full_measurement_and_review'})
                            return candidate
                        except ValueError as structural_error:
                            from .diagnostics import classify_error
                            if 'service_failure' in classify_error(str(structural_error)):raise
                            exc = structural_error
                    failed.extend({**i,'code':'voice_alignment' if i.get('measurement') else 'semantic_review',
                        'message':i['message']+'；局部修订未完成：'+str(exc)} for i in issues if i['id']==uid)
                    break
                feedback = '\n上轮局部修订未通过程序检查：'+str(exc)[:1800]+'\n上轮修订：'+json.dumps(raw,ensure_ascii=False)+'\n只返回当前target的合法修改，不能换段、清空口播、变更mode或突破原片时间线。'
    write_json(root/f'automatic-repair-{attempt}.json',{'changes':accepted})
    if failed:
        raise RepairNeeded({'stage':'automatic_partial','clip':revised.clips[0].model_dump(),'issues':failed})
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
    for clip in plan.clips:require_oral_style(clip)
    if (report.get('version')!=VERSION or report.get('mode') != 'automatic_source_review' or report.get('passed') is not True or report.get('issues') != []
            or report.get('metadata_caption_policy')!=METADATA_CAPTION_POLICY
            or not any(f.get('purpose')=='metadata_captions' for f in report.get('frames',[]))
            or report.get('source') != source_identity(video) or report.get('plan_digest') != digest(plan.model_dump())
            or report.get('transcript_digest') != digest([s.model_dump() for s in lines])
            or report.get('voice_digest') != digest(voices) or not report.get('frames')):
        raise ValueError('自动审核记录与当前稿件、素材或声纹版本不一致')
    if any(file_hash(Path(f['path'])) != f['sha256'] for f in report['frames']):
        raise ValueError('自动审核画面已改变')
