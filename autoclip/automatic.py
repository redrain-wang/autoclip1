"""Source-bound automated review and local repair, separate from human approval."""
import json
import re
import threading
from copy import deepcopy
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .identity import units_for_clip, file_hash
from .editorial import audience_script, prose_signals, require_oral_style, WRITING_RULES
from .ingest import contact_sheet, subtitle_evidence_sheet
from .titles import TITLE_POLICY, TITLE_RULES, title_repair_prompt, title_changes, title_issues
from .narrative import DEPTH_RULES, formal_long, require_paragraph_preserved, editorial_repair, require_commentary, verify_editorial_patch
from .models import Plan, Beat, validate_plan, evidence_matches
from .repair import RepairNeeded
from .util import digest, read_json, source_identity, write_json
from .voices import match_batch
from .llm import review_json

VERSION = 'automatic-review-10'
IDENTITY_NAME_POLICY = 'documented-role-names-1'
METADATA_CAPTION_POLICY = 'metadata-captions-2'
SCENE_OBSERVATION_POLICY = 'blind-selected-frames-4-boundary-context'
IDENTITY_DIALOGUE_RULES = (
    '声音候选未知不等于对白身份未知。explicit_address_response也可用明确自称与他人回应核实称谓级身份；'
    '例如自称朕并与称臣者连续问答可核实皇帝这一角色，但不能因此补出某位皇帝的姓名；'
    '侄儿、臣等自称只能证明相应关系，不能单凭它们补姓名。'
    '称呼与回应不要求固定先后：下一位说话人回应前一人的话并明确称呼对方，也可构成依据；'
    '须引用至少两条对白并说明对应关系，核对说话人、听者和连续对话，不能拿任意两条含姓名对白凑证据。'
    '这些属于对白依据，不是voice_and_context，不要求捏造声音候选。'
)


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
    # An exact source interval already named by the human is primary evidence.
    # Rematching a different scan window must not erase that confirmation.
    confirmed = [{'id':s['id'], 'start':s['start'], 'end':s['end'],
                  'name':person['name'], 'status':'human_confirmed',
                  'source':s['source']}
                 for person in library['characters'] for s in person['samples']
                 if s['source']==source]
    rows = []
    for line in lines:
        candidates = [s for s in matched['samples'] if min(s['end'],line.end)-max(s['start'],line.start) >= .3]
        candidates += [s for s in confirmed if min(s['end'],line.end)-max(s['start'],line.start) >= .3]
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


class SubjectIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid')
    subject: str = Field(min_length=2)
    basis: Literal['voice_and_context','explicit_address_response','explicit_reference']
    evidence_ids: list[int] = Field(min_length=1)
    reason: str = Field(min_length=1)


class UnitDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    verdict: Literal['supported','contradicted','unknown']
    reason: str = Field(min_length=1)
    evidence_ids: list[int] = Field(min_length=1)
    frame_ids: list[int] = Field(min_length=1)
    identity_basis: Literal['no_identity_claim','voice_and_context','explicit_address_response','explicit_reference','mixed','insufficient'] = Field(
        description='explicit_reference仅用于被提及者，不能指认发言者或行为者。不同人物使用不同依据时必须用mixed，在subject_evidence逐人列明依据和引用。')
    subjects: list[str] = Field(default_factory=list)
    subject_evidence: list[SubjectIdentity] = Field(default_factory=list)


class UnitIssue(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    message: str = Field(min_length=1)


def normalize_review_response(raw):
    """Recover harmless format drift, retaining every substantive assertion.

    Empty extension fields carry no review decision. Nonempty unit notes move
    to the existing batch-note channel, which still checks required corrections.
    Required fields, verdicts, citations and unknown nonempty fields stay strict.
    The caller keeps the untouched model response as the audit trail.
    """
    if not isinstance(raw,dict):return raw,[]
    normalized=deepcopy(raw)
    changes=[]
    def empty(value):
        return value is None or (isinstance(value,(str,list,dict)) and len(value)==0)
    def remove_empty_extensions(row,allowed,path):
        for key in list(row):
            if key not in allowed and empty(row[key]):
                del row[key]
                changes.append({'path':f'{path}.{key}','operation':'remove_empty_extension'})
    # Keep passed strict in UnitReviews; only ReviewBatch accepts that field.
    remove_empty_extensions(normalized,{'units','issues','notes','passed'},'response')
    units=normalized.get('units')
    if not isinstance(units,list):return normalized,changes
    for index,unit in enumerate(units):
        if not isinstance(unit,dict):continue
        path=f'units[{index}]'
        notes=unit.get('notes')
        if notes and isinstance(unit.get('id'),str):
            entries=notes if isinstance(notes,list) else [notes]
            lifted=[]
            for note in entries:
                if isinstance(note,str) and note.strip():
                    lifted.append({'id':unit['id'],'message':note})
                elif (isinstance(note,dict) and set(note).issubset({'id','message'})
                        and note.get('id',unit['id'])==unit['id']
                        and isinstance(note.get('message'),str) and note['message'].strip()):
                    lifted.append({'id':unit['id'],'message':note['message']})
                else:break
            else:
                batch_notes=normalized.get('notes',[])
                if isinstance(batch_notes,list):
                    normalized['notes']=[*batch_notes,*lifted]
                    del unit['notes']
                    changes.append({'path':path+'.notes','operation':'move_to_batch_notes','count':len(lifted)})
        remove_empty_extensions(unit,set(UnitDecision.model_fields),path)
        proofs=unit.get('subject_evidence')
        if isinstance(proofs,list):
            for number,proof in enumerate(proofs):
                if isinstance(proof,dict):
                    remove_empty_extensions(proof,set(SubjectIdentity.model_fields),f'{path}.subject_evidence[{number}]')
    return normalized,changes


class UnitReviews(BaseModel):
    model_config = ConfigDict(extra='forbid')
    units: list[UnitDecision]
    issues: list[UnitIssue]
    notes: list[UnitIssue] = Field(default_factory=list)

    @model_validator(mode='before')
    @classmethod
    def recover_review_format(cls,raw):
        return normalize_review_response(raw)[0]


class ReviewBatch(UnitReviews):
    passed: StrictBool


class OriginalDecision(UnitDecision):
    identity_basis: Literal['no_identity_claim']
    subjects: list[str] = Field(default_factory=list,max_length=0)


class OriginalReviewBatch(UnitReviews):
    units: list[OriginalDecision]


def review_response_schema(units, frame_count, original=False):
    """Bind a protocol recheck to actual units and main-sheet frames."""
    schema=(OriginalReviewBatch if original else UnitReviews).model_json_schema()
    ids=list(dict.fromkeys(u['id'] for u in units))
    if not ids or frame_count<1:raise ValueError('审核响应约束缺少单位或主画面')
    for name in ('UnitDecision','OriginalDecision','UnitIssue'):
        if name not in schema['$defs']:continue
        fields=schema['$defs'][name]['properties']
        fields['id']={'type':'string','enum':ids}
        if 'frame_ids' in fields:
            fields['frame_ids']['items']={'type':'integer','enum':list(range(frame_count))}
    return schema


def review_response_constraints(units, frame_count, original=False):
    ids=[u['id'] for u in units]
    return ('\n本批返回格式的精确约束：units、issues、notes的id只能原样复制'
        +json.dumps(ids,ensure_ascii=False)
        +'，不能发明missing、gap、internal-boundary等新编号。跨单位缺口绑定本批受影响的实际单位，'
        '可给同一id列多项具体问题；不能为了修正编号删除真实问题、拒绝结论或必要修订备注。'
        'frame_ids仅用主图编号，补充字幕图不增加主图格数。'
        '\n本批精确JSON格式='+json.dumps(review_response_schema(units,frame_count,original),ensure_ascii=False))


class CaptionReading(BaseModel):
    model_config=ConfigDict(extra='forbid')
    line_id: int
    text: str


class CaptionReadings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    readings: list[CaptionReading] = Field(min_length=1,max_length=6)


class SceneObservation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    frame_id: int
    visible: str = Field(min_length=1)


class SceneObservations(BaseModel):
    model_config=ConfigDict(extra='forbid')
    frames: list[SceneObservation]


def read_scene_observations(llm, sheet, times, retries):
    """Describe selected pixels without exposing script, ASR or caption crops."""
    prompt=('只描述主图各格实际可见的场所、人物、动作和道具，不审稿、不识别角色姓名。'
        '图片从0编号，按times顺序；每格都要单独描述。房间陈设不能写成有人行动，'
        '固定烛台不能写成人手提灯，不能从字幕文字推测不可见的动作。'
        '不根据其他格或剧情记忆补当前格；没人就明确写未见人物，看不清就明确写看不清。'
        '描述只能作为后续核对材料，不能宣布任何稿件通过。'
        '\n格式='+json.dumps(SceneObservations.model_json_schema(),ensure_ascii=False)
        +'\ntimes='+json.dumps(times))
    feedback=''
    for retry in range(retries+1):
        raw=llm.json('你是原片画面观察员，只描述所给主图，不读取稿件或字幕转写。',prompt+feedback,[sheet])
        try:
            parsed=SceneObservations.model_validate(raw)
            if len(parsed.frames)!=len(times) or {r.frame_id for r in parsed.frames}!=set(range(len(times))):
                raise ValueError('画面观察必须精确覆盖每个主图编号一次')
            return sorted(parsed.model_dump()['frames'],key=lambda r:r['frame_id'])
        except ValueError as exc:
            if retry==retries:raise ValueError('原片画面观察响应校验失败：'+str(exc)) from exc
            feedback='\n上次响应格式有误：'+str(exc)+'\n只重读同一主图，返回全部frames对象。'


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


@lru_cache(maxsize=16)
def series_role_names(series):
    from .research import profile
    return tuple(frozenset([c['name'],*c.get('aliases',[])]) for c in profile(series)['characters'])


def reference_names(name, series=None):
    """Normalize the same enrolled role's documented names, never another voice."""
    names={name} if name else set()
    if name and (match:=re.fullmatch(r'([^（）()]+)[（(][^（）()]+[）)]',name)):
        names.add(match[1].strip())
    if series:
        matches=[aliases for aliases in series_role_names(series) if names & aliases]
        if len(matches)==1:names.update(matches[0])
    return names


def caption_lines_for_issues(issues, lines, fallback_ids):
    """Prioritize the actual subtitle IDs disputed by the reviewer, not just endpoints."""
    lookup = {s.id:s for s in lines}
    ids = []
    for issue in issues:
        for first,last in re.findall(r'(?:字幕|对白|line|ID)\s*(\d+)(?:\s*[-–/、]\s*(\d+))?',issue['message']):
            a,b = int(first),int(last or first)
            ids.extend(range(min(a,b),max(a,b)+1) if abs(b-a)<=6 else [a,b])
    # A reviewer may quote a disputed ASR phrase without citing its numeric ID.
    # Show that actual caption before accepting its interpretation of the words.
    quotes=[word for issue in issues for word in re.findall(r'[“「"]([^”」"]{2,80})[”」"]',issue['message'])]
    if quotes:
        ranked=metadata_caption_lines([{'text':'，'.join(quotes)}],lines)
        ids.extend(s.id for s in ranked if any(re.sub(r'\W','',word) in re.sub(r'\W','',s.text) for word in quotes))
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


def explicit_reference_in_source(name, text, context=''):
    if name in text:
        return True
    # Imperial siblings call the same ranked prince “八哥/八弟”. This only
    # recognizes the title; it cannot infer a personal name or a speaker.
    rank=re.fullmatch(r'([一二三四五六七八九十]+)(?:阿哥|爷)',name)
    return bool(rank and re.search(r'皇阿玛|皇父|皇额娘',text+' '+context)
                and re.search(r'(?<![一二三四五六七八九十百\d])'+rank.group(1)+r'(?:哥|弟)',text))


def read_disputed_reference_captions(video, root, decisions, issues, lines, llm, already_read=()):
    """Read actual name captions independently before rejecting an ASR spelling."""
    disputed={i['id'] for i in issues if i['message'].startswith('被提及人物须在')}
    ids=[]
    for unit in decisions.units:
        if unit.id not in disputed:continue
        ids.extend(i for proof in unit.subject_evidence if proof.basis=='explicit_reference' for i in proof.evidence_ids)
        if unit.identity_basis=='explicit_reference':ids.extend(unit.evidence_ids)
    lookup={s.id:s for s in lines}
    chosen=[lookup[i] for i in dict.fromkeys(ids) if i in lookup and i not in already_read][:6]
    if not chosen:return None
    sheet=subtitle_evidence_sheet(video,root,chosen,'reference-names')
    readings=read_caption_observations(llm,sheet,[s.id for s in chosen])
    write_json(root/'readings.json',{'policy':'independent-reference-caption-1','readings':readings,
        'image':str(sheet.resolve()),'image_sha256':file_hash(sheet)})
    return readings,sheet


def assess_batch(raw, units, lines, frame_count, voice_rows, caption_readings=(), *, series=None):
    parsed = ReviewBatch.model_validate(raw)
    expected = {u['id']:u for u in units}
    if len(parsed.units) != len(expected) or {u.id for u in parsed.units} != set(expected):
        raise ValueError('自动审核未精确覆盖全部待核单位；实际id='
            +json.dumps([u.id for u in parsed.units],ensure_ascii=False)
            +'，允许且必须覆盖的id='+json.dumps(list(expected),ensure_ascii=False))
    lookup = {s.id:s for s in lines}
    caption_text={r['line_id']:r['text'] for r in caption_readings}
    if not set(caption_text).issubset(lookup):
        raise ValueError('字幕辨读引用了未提供的编号')
    voices = {r['line_id']:r for r in voice_rows}
    issues = [{'id':u.id,'message':u.reason} for u in parsed.units if u.verdict != 'supported']
    for issue in parsed.issues:
        if issue.id not in expected:
            # Some reviewers add a finding suffix (beat-4-identity). Retain the
            # rejection under its unambiguous parent; never discard the issue.
            parents=[uid for uid in expected if issue.id.startswith(uid+'-')]
            if len(parents)!=1:
                raise ValueError('自动审核问题指向了本批次以外的单位；非法id='
                    +json.dumps(issue.id,ensure_ascii=False)+'，允许id='+json.dumps(list(expected),ensure_ascii=False)
                    +'；跨单位缺口也须绑定本批实际单位，不能发明问题编号，真实拒绝及问题内容必须保留')
            issue.id=parents[0]
        issues.append(issue.model_dump())
    for issue in required_note_repairs(parsed.notes,units):
        if issue['id'] not in expected:raise ValueError('审核备注要求修订本批次以外的单位')
        issues.append(issue)
    for unit in parsed.units:
        if unit.subject_evidence:
            # Per-person evidence is the more precise representation. Normalize
            # its redundant summary label/list, then validate every citation
            # below (including unknown IDs and future evidence). No verdict or
            # rejection is changed by this normalization.
            unit.identity_basis='mixed'
            unit.evidence_ids=sorted(set(unit.evidence_ids).union(
                i for proof in unit.subject_evidence for i in proof.evidence_ids))
        if not set(unit.evidence_ids).issubset(lookup):
            raise ValueError(f'{unit.id} 引用了未提供的字幕编号：{sorted(set(unit.evidence_ids)-set(lookup))}')
        if any(i<0 or i>=frame_count for i in unit.frame_ids):
            raise ValueError(f'{unit.id} 引用了未提供的主画面编号；frame_ids只能为{list(range(frame_count))}，'
                '字幕放大图不另编号，不能把其格子追加到主图编号后。请按主图times重核对应关系。')
        target = expected[unit.id]
        if target.get('mode')=='original' and (unit.identity_basis!='no_identity_claim' or unit.subjects):
            raise ValueError(f'{unit.id} 是没有新增身份主张的原声；只核对剪辑，不能因说话人姓名未知改写原声')
        if 'end' in target and any(lookup[i].start >= target['end'] and i not in target.get('shown_evidence',[])
                and not (target.get('mode')=='original' and lookup[i].start<target['end']+8)
                for i in unit.evidence_ids):
            if unit.verdict == 'supported':
                raise ValueError(f'{unit.id} 声称有据但引用未来对白；请重核引用与结论，不能据此直接改稿')
            issues.append({'id':unit.id,'message':'审核依据预支了本镜头之后的剧情'})
        if unit.identity_basis == 'insufficient':
            issues.append({'id':unit.id,'message':'人物依据不足，不能放行'})
        if unit.identity_basis == 'no_identity_claim' and unit.subjects:
            issues.append({'id':unit.id,'message':'列出了人物归属却声明没有身份主张，须明确依据'})
        if unit.identity_basis == 'no_identity_claim' and target.get('mode')!='original':
            named = {name for row in voice_rows for sample in row.get('samples',[])
                     for name in reference_names(sample.get('name'),series)
                     if len(name)>=2 and name in target['text']}
            if named:
                issues.append({'id':unit.id,'message':'列出了人物归属却声明没有身份主张，正文明确出现：'
                    +'、'.join(sorted(named))+'；须核实实际称呼、说话人及被提及者，不能省略subjects绕过人物审核'})
        proofs=[(unit.identity_basis,unit.subjects,unit.evidence_ids)]
        if unit.identity_basis=='mixed':
            named=[p.subject for p in unit.subject_evidence]
            if (not unit.subjects or len(named)!=len(set(named)) or not set(named).issubset(unit.subjects)
                    or (unit.verdict=='supported' and set(named)!=set(unit.subjects))):
                raise ValueError('混合身份依据必须逐人完整覆盖subjects且不得重复')
            # A rejected unit may explain only the people it could identify.
            # Missing proof is the content defect, not a malformed rejection.
            # Its unknown/contradicted verdict remains in issues above.
            proofs=[(p.basis,[p.subject],p.evidence_ids) for p in unit.subject_evidence]
        for basis,subjects,cited_ids in proofs:
            if basis == 'explicit_address_response' and len(set(cited_ids))<2:
                issues.append({'id':unit.id,'message':'明确称呼与回应需至少两条不同证据'})
            if basis == 'explicit_reference':
                cited_text=' '.join(caption_text.get(i,lookup[i].text) for i in cited_ids)
                context=' '.join(caption_text.get(i,lookup[i].text) for i in unit.evidence_ids)
                missing=[name for name in subjects if len(name)<2 or not explicit_reference_in_source(name,cited_text,context)
                         or not explicit_reference_in_source(name,target['text'],context)]
                if not subjects or missing:
                    cited_rows=[{'id':i,'text':caption_text.get(i,lookup[i].text)} for i in cited_ids]
                    literal_names=[name for name in missing if len(name)>=2 and name in target['text']]
                    detail='\n该proof实际引用='+json.dumps(cited_rows,ensure_ascii=False)
                    if literal_names:
                        candidates=[{'id':s.id,'text':caption_text.get(s.id,s.text)} for s in lines
                            if ('end' not in target or s.start<target['end']
                                or s.id in target.get('shown_evidence',[])
                                or (target.get('mode')=='original' and s.start<target['end']+8))
                            and any(name in caption_text.get(s.id,s.text) for name in literal_names)][:6]
                        detail+='\n同名字面候选（供对图复核，非身份认定，不自动补引用或放行）='+json.dumps(candidates,ensure_ascii=False)
                    issues.append({'id':unit.id,'message':'被提及人物须在所引对白及当前稿中明确出现，不能靠未引用的声音候选补出身份；'
                        '本次未满足的subjects='+json.dumps(missing or subjects,ensure_ascii=False)
                        +'。如人物需通过称呼、回应或声音认定，须重核实际依据，不能将认人标成仅被提及。'+detail})
            if basis == 'voice_and_context':
                associated = [voices.get(i, {}) for i in cited_ids]
                candidate_names = {name for r in associated for s in r.get('samples',[])
                                   for name in reference_names(s.get('name'),series)}
                if not any(r.get('status') == 'candidate' for r in associated) or any(r.get('status') == 'conflict' for r in associated):
                    issues.append({'id':unit.id,'message':'声音候选缺失或冲突，须用明确称呼与回应核实或删除不确定归属'})
                if not subjects or not set(subjects).issubset(candidate_names):
                    issues.append({'id':unit.id,'message':'声音依据未支持本条主张中的人物，不能借其他人的候选放行'})
    if not parsed.passed and not issues:
        raise ValueError('自动审核声称未通过但未指出任何具体单位问题')
    return issues


def review_citation_limits(units, lines, voice_rows):
    """Explain the existing per-unit gates without inventing identity evidence."""
    result=[]
    for unit in units:
        allowed={s.id for s in lines if 'end' not in unit or s.start<unit['end']
                 or (unit.get('mode')=='original' and s.start<unit['end']+8)
                 or s.id in unit.get('shown_evidence',[])}
        result.append({'id':unit['id'],'end_exclusive':unit.get('end'),
            **({'boundary_context_ids':[s.id for s in lines if unit['end']<=s.start<unit['end']+8]}
               if unit.get('mode')=='original' else {}),
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
    metadata={u['id'] for u in units if u['id'] in {'title','angle','hook'} and 'start' not in u}
    text_by_id={u['id']:u.get('text','') for u in units}
    normalize=lambda text:re.sub(r'[\s，,。；;：:！？!?“”「」"、]','',text)
    reasons=[]
    for issue in issues:
        message=issue['message']
        if issue['id'] in metadata and re.search(
                r'(?:当前|本)(?:单位|段|批次|镜头).{0,35}(?:后文|尚未出现|预支)|'
                r'后(?:文|续).{0,20}(?:提前|预支)|并非紧接|压缩过度|中间.{0,20}(?:问答|穿插)|过程.{0,12}(?:省略|压缩)',message):
            reasons.append('先核对元信息的范围：title/angle/hook按全片实际选用内容审核，没有单镜头的end截止。'
                '摘要可以省略中间问答和反应镜头，“随后”不等于无间隔紧接；不能要求摘要逐步复盘。'
                '请引用全片所选的实际对白核实主体、必要因果和承诺/结果，不因提及后半场景判预支；'
                '若事件未在选用素材出现、来自别集，或省略确实改变事实/因果，仍须明确拒绝，不能仅换标签放行。')
        if (re.search(r'候选|candidate|human_confirmed',message,re.IGNORECASE)
                and re.search(r'(?:未见|没有|缺少|并非|不是|未经|未).{0,12}(?:人工确认|human_confirmed)'
                    r'|(?:应|需|必须).{0,12}(?:说明|标明|注明).{0,12}候选',message,re.IGNORECASE)):
            reasons.append('先核对声音依据的判定标准：voice_and_context使用匹配现有人工库的candidate加实际对白语境，'
                '不是要求本集每一句都另有human_confirmed标记。candidate不能单独认人，实际语境仍须对应同一角色；'
                '明确称呼与回应也可独立核实身份，不因无human_confirmed而一概拒绝。'
                'human_confirmed仍仅在其原片区间有效，不能外延；真正的声音冲突、断开的发言或不明归属仍须拒绝。'
                '候选状态用于内部核验，不要求把声纹候选等制作说明写进口播。请对照实际证据重核，不能仅切换标签放行。')
        if re.search(r'判\s*supported\s*更妥|(?:应|应该|应当)判(?:为|定为)?\s*supported',message,re.IGNORECASE):
            reasons.append('审核把“应当判supported”的说明列成了错误。请重新核对原片，明确一致的verdict和真实问题；若仍有独立错误必须列明，不能把无错误说明交给写稿器。')
        alleged=re.findall(r'(?:旁白|口播|正文|稿件|文本|修订稿)(?:却误|却|误|竟|仍)?'
            r'(?:写成|写为|称为|说成|使用了?|用了?|称|写)[“「"]([^”」"]+)[”」"]',message)
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


def review_timeline(clip, group):
    """Narration sees only prior/current story; original cut review needs the next cut."""
    timeline=audience_script(clip)
    if any('start' not in u for u in group):return timeline
    last=max(int(u['id'].split('-')[1]) for u in group)
    return timeline[:last+(3 if all(u.get('mode')=='original' for u in group) else 1)]


def original_continuity(clip, index):
    """Only exact adjacent source intervals constitute an uncut audio chain."""
    if clip.beats[index].mode!='original':return {}
    first=last=index
    while (first>0 and clip.beats[first-1].mode=='original'
           and abs(clip.beats[first-1].end-clip.beats[first].start)<1e-6):first-=1
    while (last+1<len(clip.beats) and clip.beats[last+1].mode=='original'
           and abs(clip.beats[last].end-clip.beats[last+1].start)<1e-6):last+=1
    if first==last:return {}
    return {'uncut_chain':{'first_beat':first,'last_beat':last,
        'start':clip.beats[first].start,'end':clip.beats[last].end,
        'start_is_internal_boundary':index>first,'end_is_internal_boundary':index<last}}


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
               'evidence_ids':b.evidence_ids,**original_continuity(clip,i)} for i,b in enumerate(clip.beats) if b.mode == 'original']
    lookup = {s.id:s for s in lines}
    for unit in units:
        if unit['id'].startswith(('beat-','original-')):
            index = int(unit['id'].split('-')[1])
            prior = {eid for b in clip.beats[:index] for eid in b.evidence_ids}
            unit['shown_evidence'] = sorted(eid for eid in prior if lookup[eid].start<unit['end']
                or (cfg.opening=='original_teaser' and index==1 and eid in clip.beats[0].evidence_ids))
    key = digest({'v':VERSION,'scene_policy':SCENE_OBSERVATION_POLICY,'title_policy':TITLE_POLICY,'plan':plan.model_dump(),'voices':voices,'model':cfg.llm_model,'endpoint':cfg.llm_base_url})
    folder = root/'automatic-review'/key
    # Keep the exact reviewed draft even when later measurement overwrites the
    # mutable plan-timed.json or a subsequent review is interrupted.
    write_json(folder/'plan.json',plan.model_dump())
    review_state={'status':'running','plan_digest':digest(plan.model_dump()),
        'review_root':str(folder.resolve()),'completed_batches':0}
    write_json(root/'automatic-review-state.json',review_state)
    all_issues=[{'id':f'beat-{row["beat"]}','message':'旁白混入制作或审查话术，必须面向观众直接讲剧情，不能照读核对记录：'
        +json.dumps(row['phrases'],ensure_ascii=False)} for row in prose_signals(clip)]
    all_issues.extend(title_issues(clip.title))
    batches, frames = [], []
    groups = []
    offset = 0
    for subset, size in (([u for u in units if 'start' not in u],3),
                         ([u for u in units if 'start' in u and u.get('mode')!='original'],1),
                         ([u for u in units if u.get('mode')=='original'],3)):
        for start in range(0,len(subset),size):
            group = subset[start:start+size]
            groups.append((offset,group))
            offset += len(group)
    review_state['total_batches']=len(groups)
    progress_lock=threading.Lock()
    def progress(event, group, issue_count=0):
        with progress_lock:
            if event=='completed':review_state['completed_batches']+=1
            label='、'.join(u['id'] for u in group)
            review_state.update(current_units=[u['id'] for u in group],event=event)
            write_json(root/'automatic-review-state.json',review_state)
            detail='开始' if event=='started' else f'完成，待修{issue_count}项'
            print(f'{video.stem}：内容核对 {review_state["completed_batches"]}/{len(groups)}（{label}，{detail}）',flush=True)
    def review_group(job):
        offset,group = job
        progress('started',group)
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
        timeline=review_timeline(clip,group)
        prompt = (IDENTITY_DIALOGUE_RULES+'逐项核对文本事实、人物行为归属、画面相关性、连续原声切点、场景承接和重复解释。'
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
                  '同段不同人物可有不同依据：此时identity_basis=mixed，并在subject_evidence按subjects逐人列subject、basis、evidence_ids、reason。每人的引用只能来自本单位evidence_ids，不能把甲的声音借给乙；例如甲由确认声音核实，乙由明确称呼及回应核实，不必强迫乙也有声音候选。'
                  '区分说话人、被称呼者和被提及者。“四爷和十三爷被叫住”没有指认喊话人，不能因喊话人声纹未知否定被称呼者。用清楚称呼/回应核实相应角色，不能混淆主客体。'
                  '若只转述对白提到的人，例如“有人认为八爷有五成胜算”，没有把八爷当作这句发言者或给画面人物冠名，可用explicit_reference；subjects列被提及者，必须在引用的原对白中明确出现。不能用此依据认定谁在说话或行动，相关观点仍须注明归属，不能把传言当成事实。'
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
                  f'\nviewer_timeline={json.dumps(timeline,ensure_ascii=False)}')
        if original:
            prompt = ('只审核本批连续原声的剪辑：切点是否切断完整对白、是否因剪接伪造问答因果或丢失必要承接。'
                '这些单位没有新增旁白、姓名标签或角色署名，text只是待核事项，不是观众会听到的附加断言。'
                '不要求先认出演员或说话人姓名；姓名未知本身不是剪辑错误，不据此标unknown。identity_basis固定no_identity_claim、subjects固定空数组。'
                'source_continuity=single_uncut_interval是渲染约束：start到end内保留原片全部连续音画，没有本系统内部删剪；字幕空白间隔内的声音及画面也完整保留，不能仅因字幕间有停顿就怀疑本系统剪掉了对白。只检查本系统选取的边界与跨单位承接，不重审原剧导演本身的剪接。'
                '若切点、问答或承接本身不清楚仍须unknown；实质剪辑问题须拒绝，不能因没有身份主张就默认支持。'
                'ASR同音字形不等于实际对白错误；区间含start、不含end，end等于下条字幕start没有剪入下条对白。'
                '原声切点可引用结束后8秒以内的对白作边界对照，判断是否剪断；这不表示该对白已剪入，不能据此新增剧情或人物标签。'
                'issues仅列实际待修问题，已排除的疑点与解释放notes。每项结论必须引用提供的字幕与图片编号，图片从0编号。'
                '先对照viewer_timeline中的实际相邻原声和旁白，再判断缺失承接；当前单位evidence_ids不是全部所提供的上下文。原声听到字幕内容，旁白听到text，不能混为一谈。'
                '不要返回passed总开关；程序按每个单位的verdict及issues汇总。未知或实际错误仍须逐项拒绝。'
                f'\n格式{json.dumps(OriginalReviewBatch.model_json_schema(),ensure_ascii=False)}'
                f'\nunits={json.dumps(group,ensure_ascii=False)}\ntimes={times}'
                f'\n字幕={json.dumps(review_transcript_rows(selected),ensure_ascii=False)}'
                f'\nviewer_timeline={json.dumps(timeline,ensure_ascii=False)}')
            if any(u.get('uncut_chain') for u in group):
                prompt+=('\nuncut_chain是程序按实际相邻original区间计算的连续原声链，不是模型猜测。'
                    '其中internal_boundary为true的边界两侧来源时间完全相接，原片对白会在下一段继续播放，'
                    '中间没有删掉声音，也没有插入旁白。不能仅因当前内部单位在半句话处分段就判整片切断对白；'
                    '必须沿同一链查看实际起止边界。链的外部切点、真实时间缺口和跨链因果仍须严格核对。')
        if not original:
            prompt += ('\n解读与事实的边界：'+DEPTH_RULES+
                '必须逐句核对每个事实断言，不能用其中一句有据替整段放行。'
                'viewer_timeline及当前稿都是待核文本，不能彼此证明角色身份、地点或事件；仅原片、对白和声音依据可证明事实。'
                '声音记录status=human_confirmed仅表示该source与start/end区间的说话人已人工确认，不自动延伸到邻近声音或被提及者。'
                '对明确建立在已播事实上的分析，核查推理是否成立，而不是要求逐字找到同样台词。'
                '不能为回避推断把完整解释退化成动作复述；修改只针对无据主张，保留有依据的分析与必要上下文。')
        # A syntactically valid JSON response can still fail schema/citation
        # validation. Replaying that same cached response after an exhausted
        # retry loop cannot recover. Carry its actual feedback into a new,
        # bounded review request; retain this seed after recovery for cache reuse.
        resume_path=folder/f'batch-{offset}-protocol-resume.json'
        resume=read_json(resume_path) if resume_path.exists() else None
        previous_failure=folder/f'batch-{offset}-protocol-{cfg.repair_attempts}.json'
        if resume is None and not (folder/f'batch-{offset}.json').exists() and previous_failure.exists():
            resume={**read_json(previous_failure),'round':1}
            write_json(resume_path,resume)
        feedback = ('\n上次运行本批审核响应未通过格式/引用检查；本次只重核同一批，不修改稿件。'
            +f'恢复轮次{resume["round"]}。校验错误：'+resume['error'][:1800]
            +'\n上次无效响应：'+json.dumps(resume['raw'],ensure_ascii=False)
            +'\n逐项引用边界='+json.dumps(review_citation_limits(group,selected,evidence),ensure_ascii=False)
            +'\n须返回本批全部units对象和issues数组；真实未知或错误仍须拒绝，不能为了恢复而放行。') if resume else ''
        review_images = [sheet]
        caption_sheet = None
        boundary_sheet = None
        caption_observations=None
        metadata=any('start' not in u for u in group)
        if metadata:
            prompt += ('\n元信息范围：title/angle/hook是全片梗概、角度与设问，不是当前镜头的口播。'
                '按全片实际选用证据核查，允许概括、省略中间过程和提及本片后半场景；没有单镜头的end截止。'
                '“随后”不等于无间隔紧接，不因中间有问答或反应镜头就判时间错误；不要求摘要逐步复盘全部对白。'
                '概括仍必须保留真实行动主体、必要因果及结果边界，不能把认捐写成钱已到账；未选用情节或其他集事实仍拒绝。'
                '\n标题编辑审核规则（只作用于title单位）：'+TITLE_RULES+
                '分别检查剧情准确性、具体看点、精炼程度；任何不满足均列id=title的具体issues，不能只查事实就放行。'
                '不因另一种措辞更喜欢而拒绝合格标题；不要求标题覆盖所有支线。')
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
            observations=read_scene_observations(llm,sheet,times,cfg.repair_attempts)
            write_json(folder/f'batch-{offset}-scene-observations.json',{'policy':SCENE_OBSERVATION_POLICY,
                'frames':observations,'times':times,'image':str(sheet.resolve()),'image_sha256':file_hash(sheet)})
            prompt+=('\n以下是只看主图、未读取稿件和ASR的独立画面观察，仍须对图核实：'
                +json.dumps(observations,ensure_ascii=False)
                +'\n严格区分当前实际剪入画面与引用对白的字幕图。引用字幕图可能在更早时间，不能把其中动作或道具移到当前画面。'
                '逐项按start/end对应主图times，只能用对应格证明当前动作。若观察与旁白冲突，回看主图，不能直接复述旁白宣布支持；'
                '无法消除的冲突必须unknown/contradicted并列明问题，不能仅凭对白支持而放过无据动作。'
                '注意：近景未展示环境，不等于人物不在已交代的场所；抽样帧未拍到动作，不等于连续原片中没有该动作。'
                '准确概括此前已播放的对白、自然回顾已经发生的剧情，不要求当前每格重演一遍；须对照完整前情判断，'
                '只有确实虚构动作、错配人物或将别处事件说成当前发生才拒绝，不能把未见当成相反事实。')
        rejected_record = None
        scope_protocol=bool(resume and re.search('本批次以外|未精确覆盖全部待核单位',resume['error']))
        for retry in range(cfg.repair_attempts+1):
            call=(lambda *args:review_json(llm,*args)) if retry or resume else llm.json
            recovered=None
            if retry==0 and resume and not scope_protocol:
                try:
                    (OriginalReviewBatch if original else UnitReviews).model_validate(resume['raw'])
                    recovered=resume['raw']
                except ValueError:pass
            contract=review_response_constraints(group,len(times),original) if scope_protocol else ''
            raw = recovered if recovered is not None else call('你是独立的影视内容与人物证据审核员。稿件及历史审核结论不能充当事实依据；人工确认声纹仅在所标原片区间内有效。',prompt+feedback+contract,review_images)
            try:
                decisions = (OriginalReviewBatch if original else UnitReviews).model_validate(raw)
                checked = decisions.model_dump()
                checked['passed'] = not decisions.issues and all(u.verdict=='supported' for u in decisions.units)
                issues = assess_batch(checked,group,selected,len(times),evidence,caption_observations or (),series=cfg.series)
                if retry<cfg.repair_attempts:
                    read=read_disputed_reference_captions(video,folder/f'batch-{offset}-reference-captions-{retry}',decisions,issues,selected,llm,
                        already_read={r['line_id'] for r in caption_observations or []})
                    if read:
                        readings,reference_sheet=read
                        caption_observations=list({r['line_id']:r for r in [*(caption_observations or []),*readings]}.values())
                        review_images.append(reference_sheet)
                        feedback=('\n姓名字形存在争议，先对照新增原片字幕图及独立辨读结果复核；不能把ASR错字当成新人物，'
                            '不能仅因辨读确认姓名就默认动作或归属也成立。仍逐项核实事实及人物依据。'
                            +json.dumps(caption_observations,ensure_ascii=False)+'\n上一轮响应：'+json.dumps(raw,ensure_ascii=False))
                        continue
                if issues:
                    rejected_record=(raw,issues)
                if original and issues and boundary_sheet is None and retry<cfg.repair_attempts:
                    disputed={i['id'] for i in issues}
                    ceiling=max(max(b.end for b in clip.beats),max(s.end for s in lines))-.1
                    boundary_times=sorted({round(max(0,min(ceiling,t)),3) for u in group if u['id'] in disputed
                        for t in (u['start']-.8,u['start']+.1,u['end']-.1,u['end']+.8)})
                    boundary_sheet=contact_sheet(video,folder/f'batch-{offset}-boundary-context',boundary_times,'boundaries')
                    review_images.append(boundary_sheet)
                    write_json(folder/f'batch-{offset}-boundary-context.json',{'times':boundary_times,
                        'image':str(boundary_sheet.resolve()),'image_sha256':file_hash(boundary_sheet),'prior_issues':issues})
                    feedback=('\n先复核边界疑点，尚未改稿。追加原片切点前后画面，times='+json.dumps(boundary_times)
                        +'；其中选段之外的图仅作边界上下文，不代表剪入成片，frame_ids仍只用第一张主图编号。'
                        '逐项指出到底删掉了哪句必要对白或伪造了什么问答，不能仅因原片自身转场或人物变化判系统剪断。'
                        '每个original区间内部没有删剪，字幕间的声音与画面也全部保留。'
                        '连续呼唤姓名及完整回应也可以构成合法原声片段，不要求每段都是长句或完整问答。'
                        '对照实际前后稿和uncut_chain，只有真实边界或承接问题才拒绝；确实无法核实仍unknown。'
                        '\n上轮核验='+json.dumps(raw,ensure_ascii=False)+'\n返回本批全部units和issues。')
                    continue
                recheck=review_recheck_reason(issues,group)
                if recheck and retry<cfg.repair_attempts:
                    write_json(folder/f'batch-{offset}-source-recheck-{retry}.json',{'raw':raw,'issues':issues})
                    feedback=('\n先核对审核本身，不修改稿件：'+'；'.join(recheck)
                        +'\n上一轮审核：'+json.dumps(raw,ensure_ascii=False)
                        +'\n请重新给出本批全部units与真实问题；不能为了通过而忽略仍然未知或错误的内容。')
                    continue
                if (not original and retry<cfg.repair_attempts and caption_sheet is not None
                        and caption_observations is None
                        and any(re.search(r'ASR|转写|同音|错字|错词|残句|(?:给定|提供的)字幕|字幕[^。]{0,35}没有|自称',i['message']) for i in issues)):
                    disputed=caption_lines_for_issues(issues,selected,[s.id for s in caption_lines])
                    if [s.id for s in disputed]!=[s.id for s in caption_lines]:
                        caption_lines=disputed
                        caption_sheet=subtitle_evidence_sheet(video,folder/f'batch-{offset}-disputed-captions',caption_lines,'disputed')
                        review_images.append(caption_sheet)
                    caption_observations=read_caption_observations(llm,caption_sheet,[s.id for s in caption_lines])
                    write_json(folder/f'batch-{offset}-caption-readings.json',{'readings':caption_observations,
                        'image':str(caption_sheet.resolve()),'image_sha256':file_hash(caption_sheet)})
                    write_json(folder/f'batch-{offset}-source-recheck-{retry}.json',{'raw':raw,'issues':issues})
                    feedback=('\n上一轮存在需要核实的对白疑点，先重核原片及审核自身，不先改稿。'
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
                    '声音候选缺失或冲突','声音依据未支持','列出了人物归属却声明','明确称呼与回应需至少','被提及人物须在'))]
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
                if re.search('本批次以外|未精确覆盖全部待核单位',str(exc)):scope_protocol=True
                write_json(folder/f'batch-{offset}-protocol-{retry}.json',{'raw':raw,'error':str(exc)})
                if retry == cfg.repair_attempts:
                    if rejected_record is not None:
                        # Preserve a structurally valid rejection if a later
                        # recheck is malformed. This can never create approval.
                        raw,issues=rejected_record
                        write_json(folder/f'batch-{offset}-recheck-failed.json',
                            {'error':str(exc),'action':'retain_last_valid_rejection'})
                        break
                    write_json(resume_path,{'round':(resume or {}).get('round',0)+1,'raw':raw,'error':str(exc)})
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
                if resume:
                    feedback+='\n保留本次恢复轮次'+str(resume['round'])+'及原失败上下文：'+json.dumps(resume,ensure_ascii=False)
        record = {'units':group,'times':times,'raw':raw,'issues':issues,
                  'format_repairs':normalize_review_response(raw)[1]}
        write_json(folder/f'batch-{offset}.json',record)
        frames = [{'path':str(p.resolve()),'sha256':file_hash(p),
            **({'purpose':'metadata_captions'} if metadata and p==caption_sheet else {})} for p in review_images]
        progress('completed',group,len(issues))
        return issues,str((folder/f'batch-{offset}.json').resolve()),frames
    # Batches are independent; keep results in source order and share LLM's
    # locked request allowance. No partial batch constitutes an approval.
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            for issues,batch,frame in pool.map(review_group,groups):
                all_issues.extend(issues)
                batches.append(batch)
                frames.extend(frame)
        except BaseException as exc:
            pool.shutdown(wait=True,cancel_futures=True)
            review_state.update(status='failed' if isinstance(exc,Exception) else 'interrupted',
                error=str(exc)[:1500] or type(exc).__name__)
            write_json(root/'automatic-review-state.json',review_state)
            raise
    report = {'mode':'automatic_source_review','version':VERSION,'metadata_caption_policy':METADATA_CAPTION_POLICY,
              'scene_observation_policy':SCENE_OBSERVATION_POLICY,'title_policy':TITLE_POLICY,'source':source_identity(video),
              'plan_digest':digest(plan.model_dump()),'transcript_digest':digest([s.model_dump() for s in lines]),
              'voice_digest':digest(voices),'passed':not all_issues,'issues':all_issues,'batches':batches,'frames':frames,
              'limitations':['机器审核可能误判；声纹候选不等于人工确定身份。']}
    write_json(folder/'report.json',report)
    write_json(root/'automatic-review.json',report)
    write_json(root/'automatic-review-state.json',{**review_state,'status':'completed'})
    return report


def retain_short_source_audio(plan, uid, lines, cfg, source_seconds):
    """Keep complete dialogue pending full measurement and whole-film review."""
    if source_seconds is None or not uid.startswith('beat-'):
        return None
    index = int(uid.split('-')[1])
    before = plan.clips[0].beats[index]
    limit=40 if formal_long(cfg) else 8
    if before.mode!='narration' or not 1<=before.end-before.start<=limit:
        return None
    start,end=before.start,before.end
    if formal_long(cfg):
        # TTS measurement may have trimmed the narration picture mid-sentence.
        # Restore only intersecting complete utterances, never another shot.
        while True:
            crossing=[s for s in lines if s.start<end-.001 and s.end>start+.001]
            expanded=(min([start,*[s.start for s in crossing]]),max([end,*[s.end for s in crossing]]))
            if expanded==(start,end):break
            start,end=expanded
            if end-start>limit:return None
        left=plan.clips[0].beats[index-1].end if index else 0
        right=plan.clips[0].beats[index+1].start if index+1<len(plan.clips[0].beats) else source_seconds
        if start<left-.001 or end>right+.001:return None
    if any(s.start+.001<cut<s.end-.001 for s in lines for cut in (start,end)):
        return None
    ids = [s.id for s in lines if s.start>=start-.001 and s.end<=end+.001]
    if not ids:
        return None
    revised = plan.model_copy(deep=True)
    revised.clips[0].beats[index] = Beat(mode='original',start=start,end=end,
        text='',evidence_ids=ids,reason='局部口播修稿未通过，保留本区间完整真实对白；仍须重新测时及核对剪辑和全稿')
    try:
        if not formal_long(cfg):require_commentary(revised.clips[0],cfg)
        validate_plan(revised,lines,source_seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening,
            check_duration=not formal_long(cfg))
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


def verify_narration_patch(video, candidate, uid, prior_claims, lines, voices, root, llm, *, retries=0, series=None):
    """Check the changed unit against source before spending a full-film re-audit."""
    index=int(uid.split('-')[1])
    clip=candidate.clips[0]
    beat=clip.beats[index]
    shown={eid for b in clip.beats[:index] for eid in b.evidence_ids}
    unit={'id':uid,'text':beat.text,'start':beat.start,'end':beat.end,
          'evidence_ids':beat.evidence_ids,
          'shown_evidence':[s.id for s in lines if s.id in shown and s.start<beat.end]}
    selected=[s for s in lines if s.id in beat.evidence_ids or s.id in unit['shown_evidence']
              or beat.start-8<=s.start<beat.end+8]
    evidence=review_voice_rows([r for r in voices.get('lines',[]) if r['line_id'] in {s.id for s in selected}])
    times=[round(t,3) for t in (beat.start+.1,(beat.start+beat.end)/2,beat.end-.1)]
    images=[contact_sheet(video,root/'selected',times,'selected')]
    caption_lines=caption_lines_for_issues(prior_claims,selected,beat.evidence_ids)
    if caption_lines:images.append(subtitle_evidence_sheet(video,root/'captions',caption_lines,'captions'))
    schema=UnitReviews.model_json_schema()
    for name in ('UnitDecision','UnitIssue'):
        schema['$defs'][name]['properties']['id']={'type':'string','const':uid}
    prompt=(IDENTITY_DIALOGUE_RULES+f'只核验当前单位{uid}，units、issues、notes内的id只能原样写{uid}，不得添加子项编号。'
        '独立核验units中当前这一段口播的事实、人物归属和画面，不改稿。'
        '只核对实际text中的断言，不猜测旧稿写过什么；逐句核对，不能用其中一句有据替整段放行。'
        '第一张图是当前实际剪入区间的首中尾画面，frame_ids只能是0、1、2；其后是引用对白字幕图。'
        '过去对白可以支持自然回顾，但不能将此前出现的动作、道具或人物说成正在当前画面发生。'
        '字幕中的称呼可能指听者，不能自动当作说话人。明确称呼与回应须引用至少两条不同对白；'
        '声音加语境须列出实际候选姓名，不能自行添加别名。无法核实就unknown，具体问题写issues。'
        '同段不同人物依据不同时用mixed，并在subject_evidence逐人列subject、basis、evidence_ids、reason；每人的证据均须在本单位总引用中。不能强迫所有人物共用声纹依据，也不能借用另一人的声纹。'
        '只转述对白中明确提及的人、未认定说话人或画面人物时，可用explicit_reference，subjects列原对白明确出现的被提及者；不能凭此指认动作或发言者，也不能把转述的观点当成既成事实。'
        '声音记录status=human_confirmed表示该source及start/end区间的说话人已人工确认，不能将它误读成未确认机器候选；只对该区间有效，不能延伸到邻近发言。'
        'ASR不是原片字幕的正确答案，不要求正确转述迎合机器错字。仅换同义词、保留无据动作仍须拒绝。'
        '每个结论都必须引用提供的对白和主图编号。只返回units、issues及可选notes，不返回passed。'
        '\n格式'+json.dumps(schema,ensure_ascii=False)
        +'\nunits='+json.dumps([unit],ensure_ascii=False)+'\ntimes='+json.dumps(times)
        +'\n字幕='+json.dumps(review_transcript_rows(selected),ensure_ascii=False)
        +'\n声音候选='+json.dumps(evidence,ensure_ascii=False)
        +'\n逐项引用边界='+json.dumps(review_citation_limits([unit],selected,evidence),ensure_ascii=False)
        +'\nviewer_timeline='+json.dumps(review_timeline(clip,[unit]),ensure_ascii=False)
        +'\n稿件与前情都是待核文本，不能互相充当事实证据。'+DEPTH_RULES)
    feedback=''
    caption_observations=[]
    asr_dispute_checked=False
    for retry in range(retries+1):
        raw=review_json(llm,'你是独立的局部修订核验员，不是写稿者。稿件及历史审核结论不能充当事实依据；人工确认声纹仅在所标原片区间内有效。',prompt+feedback,images)
        try:
            decisions=UnitReviews.model_validate(raw)
            checked=decisions.model_dump()
            checked['passed']=not decisions.issues and all(u.verdict=='supported' for u in decisions.units)
            issues=assess_batch(checked,[unit],selected,len(times),evidence,caption_observations,series=series)
            if retry<retries:
                read=read_disputed_reference_captions(video,root/f'reference-captions-{retry}',decisions,issues,selected,llm,
                    already_read={r['line_id'] for r in caption_observations})
                if read:
                    readings,reference_sheet=read
                    caption_observations=list({r['line_id']:r for r in [*caption_observations,*readings]}.values())
                    images.append(reference_sheet)
                    feedback=('\n先对照新增原片字幕图及独立辨读核对姓名字形，不能因ASR错字拒绝正确姓名；'
                        '姓名有据不代表当前动作或人物归属自动有据，仍须核实全部实际断言。'
                        +json.dumps(caption_observations,ensure_ascii=False)+'\n上一轮核验：'+json.dumps(raw,ensure_ascii=False))
                    continue
                if not asr_dispute_checked and any(re.search(
                        r'ASR|转写|同音|错字|错词|字形|定字|(?:给定|提供的)字幕|字幕[^。]{0,35}没有',
                        i['message'],re.IGNORECASE) for i in issues):
                    asr_dispute_checked=True
                    disputed=caption_lines_for_issues(issues,selected,beat.evidence_ids)
                    if disputed:
                        sheet=subtitle_evidence_sheet(video,root/'disputed-captions',disputed,'disputed')
                        readings=read_caption_observations(llm,sheet,[s.id for s in disputed])
                        caption_observations=list({r['line_id']:r for r in [*caption_observations,*readings]}.values())
                        images.append(sheet)
                        write_json(root/'disputed-captions/readings.json',{'readings':readings,
                            'image':str(sheet.resolve()),'image_sha256':file_hash(sheet)})
                        feedback=('\n先核实原片对白争议，尚未改稿。这是未看转写和稿件、只读原片字幕图的独立辨读：'
                            +json.dumps(caption_observations,ensure_ascii=False)
                            +'。须对图重核全部实际断言；字形辨读不代表动作、归属或整句事实已通过。'
                            '不能以原始ASR错字为由修改正确旁白，仍有未知或错误则明确拒绝。'
                            +'\n上一轮核验：'+json.dumps(raw,ensure_ascii=False))
                        continue
            supported={u.id for u in decisions.units if u.verdict=='supported'}
            inconsistent=[i for i in issues if i['id'] in supported and i['message'].startswith((
                '声音候选缺失或冲突','声音依据未支持','列出了人物归属却声明','明确称呼与回应需至少','被提及人物须在'))]
            recheck=review_recheck_reason(issues,[unit])
            if inconsistent or recheck:
                raise ValueError('先重核核验结论自身，稿件尚未修改：'+json.dumps(inconsistent+recheck,ensure_ascii=False))
            break
        except ValueError as exc:
            write_json(root/f'protocol-{retry}.json',{'raw':raw,'passed':False,'error':str(exc)})
            if retry==retries:
                write_json(root/'verification.json',{'raw':raw,'passed':False,'error':str(exc)})
                raise
            feedback=('\n核验响应第'+str(retry+1)+'次未通过格式、引用或结论一致性检查：'+str(exc)
                +'\n上一轮核验：'+json.dumps(raw,ensure_ascii=False)
                +'\n只重新核验同一段，不要求改稿。no_identity_claim不能列subjects；若有真实身份主张须提供实际依据。'
                'voice_and_context必须使用给定候选名；explicit_address_response须有至少两条明确称呼及回应。'
                '没有依据就unknown并说明缺口，不能仅切换标签放行。返回完整units和issues对象。')
    write_json(root/'verification.json',{'raw':raw,'passed':not issues,'issues':issues,
        'format_repairs':normalize_review_response(raw)[1],
        'unit':unit,'frames':[{'path':str(p.resolve()),'sha256':file_hash(p)} for p in images]})
    if issues:raise ValueError('局部修订仍未解决原片问题：'+json.dumps(issues,ensure_ascii=False))


def repair_plan(plan, issues, lines, voices, root, cfg, llm, attempt, *, source_seconds=None):
    """Repair one failed unit at a time, keeping validated edits and shot IDs."""
    revised = plan.model_copy(deep=True)
    accepted = []
    failed = []
    lookup = {s.id:s for s in lines}
    units=list(dict.fromkeys(issue['id'] for issue in issues))
    # Build the missing explanation before cutting repetition elsewhere, so a
    # temporary word-count deficit cannot force filler back into a transition.
    if issues and all(i.get('stage')=='narrative_review' for i in issues):
        def needs_explanation(uid):
            return any(i['id']==uid and re.search(r'没有(?:说明|解释|点出)|未(?:交代|说明|讲清|解释)|缺(?:少|乏).{0,12}(?:因果|解释|分析)',i['message']) for i in issues)
        units.sort(key=lambda uid:not needs_explanation(uid))
    for unit_number,uid in enumerate(units,1):
        label={'title':'标题','angle':'解读角度','hook':'主问题'}.get(uid)
        if label is None and uid.startswith(('beat-','original-')):
            label=f'第{int(uid.split("-")[1])+1}个片段'+('旁白' if uid.startswith('beat-') else '原声')
        label=label or uid
        print(f'局部修稿：{label}（本轮第{unit_number}/{len(units)}项）',flush=True)
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
        prompt+='\n面向观众的写作要求：'+WRITING_RULES+'\n人物依据：'+IDENTITY_DIALOGUE_RULES
        if uid.startswith('original-'):
            prompt+=('\n原声修复必须实际调整start或end，保留有据的完整对白和必要回应。'
                '仅改reason或evidence_ids不会改变观众听到的音画，不能算解决切点、缺句或承接问题。'
                '不能在reason声称原声真实就保留同一区间；无法核实时须继续选择可核实的合法切点，不猜对白。')
        target_issues=[i for i in issues if i['id']==uid]
        condense=editorial_repair(target_issues,target.get('text',''))
        if condense:
            prompt+=('\n本次是整稿编辑提出的衔接、分析或重复问题，不是仅做事实纠错。必须落实具体修订方向。'
                '允许删除重复问答并将冗余段缩为必要的短转场，不必保住原字数；不能换词复述或填空话。'
                '主要冲突的有据分析在整片中仍须充分展开，修后仍接受全片分量、事实与整稿质量检查。')
        elif uid.startswith('beat-') and formal_long(cfg) and max_characters is None:
            prompt+=('\n这是正式长解说的事实或整稿质量修复。只纠正无据断言，保留正确的解释与完整思路。'
                '主要段落不能修成两句转场或换词复述；用当前允许的事实解释处境、行动约束或眼前代价。'
                '不要预支后文，不为凑字编造。若某一细节需删，重写它与前后段的连接，而不是删掉整段的分析。')
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
        if uid == 'title':
            prompt = title_repair_prompt(clip,[i for i in issues if i['id']==uid],
                review_transcript_rows([s for s in lines if s.id in eligible]),
                review_voice_rows([r for r in voices.get('lines',[]) if r['line_id'] in eligible]))
        feedback = ''
        for retry in range(cfg.repair_attempts+1):
            print(f'局部修稿：{label}，第{retry+1}/{cfg.repair_attempts+1}次修订',flush=True)
            writer=(lambda *args:review_json(llm,*args)) if retry else llm.json
            raw = (writer('你是局部修稿编辑，只依据提供证据修复当前一个单位。',prompt+feedback,repair_images)
                   if repair_images else writer('你是局部修稿编辑，只依据提供证据修复当前一个单位。',prompt+feedback))
            record = root/'automatic-repairs'/str(attempt)/uid/f'attempt-{retry}.json'
            try:
                if uid == 'title':
                    write_json(record.with_name(f'candidates-{retry}.json'),{'policy':TITLE_POLICY,'raw':raw,
                        'status':'draft_requires_source_review'})
                    raw = title_changes(raw,eligible)
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
                if candidate==revised:
                    raise ValueError('当前修订未改变稿件或镜头，不能用原稿重复核验；须落实具体问题')
                if uid.startswith('original-'):
                    index=int(uid.split('-')[1])
                    after=candidate.clips[0].beats[index]
                    if abs(after.start-target['start'])<.001 and abs(after.end-target['end'])<.001:
                        raise ValueError('原声修复未改变实际剪辑区间；仅改内部reason或evidence_ids不能解决音画问题，须调整合法切点')
                if uid.startswith('beat-'):
                    index=int(uid.split('-')[1])
                    minimum=max((i.get('minimum_chars',0) for i in target_issues if i.get('stage')=='commentary_balance'),default=0)
                    if minimum:
                        from .budget import spoken_characters
                        if spoken_characters(candidate.clips[0].beats[index].text)<minimum:
                            raise ValueError(f'全片讲述补充未落实，本段须至少{minimum}个有效字；只补有据解释，不能重复或编造')
                    if max_characters is None:
                        require_paragraph_preserved(target['text'],candidate.clips[0].beats[index].text,cfg,issues=target_issues)
                    if signals:=[s for s in prose_signals(candidate.clips[0]) if s['beat']==index]:
                        raise ValueError('当前旁白仍混入制作或审查话术，不能进入配音：'+json.dumps(signals,ensure_ascii=False))
                if max_characters is not None and spoken_characters(candidate.clips[0].beats[int(uid.split('-')[1])].text)>max_characters:
                    raise ValueError(f'实测时长修复没有落实缩写：当前口播必须不超过{max_characters}个有效字')
                if condense and uid.startswith('beat-'):
                    verify_editorial_patch(candidate.clips[0],int(uid.split('-')[1]),target_issues,lines,llm,record.parent/f'verify-{retry}',retries=cfg.repair_attempts)
                if source_seconds is not None:
                    validate_plan(candidate,lines,source_seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening,
                        check_duration=not (condense or formal_long(cfg)))
                    for index,b in enumerate(candidate.clips[0].beats):
                        if uid not in {f'beat-{index}',f'original-{index}'}:
                            continue  # untouched units still undergo the full source audit
                        teaser = set(candidate.clips[0].beats[0].evidence_ids) if cfg.opening=='original_teaser' and index==1 else set()
                        if any(lookup[i].start>=b.end and i not in teaser for i in b.evidence_ids):
                            raise ValueError(f'镜头{index}修订引用了尚未播放的未来对白')
                        if b.mode=='original' and any(s.start+.2<cut<s.end-.2 for s in lines for cut in (b.start,b.end)):
                            raise ValueError(f'镜头{index}修订后的原声切点落在字幕句中')
                if (uid.startswith('beat-') and max_characters is None and cfg.vision
                        and source.get('path') and Path(source['path']).is_file()):
                    verify_narration_patch(Path(source['path']),candidate,uid,[i for i in issues if i['id']==uid],
                        lines,voices,record.parent/f'verify-{retry}',llm,retries=cfg.repair_attempts,series=cfg.series)
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
                feedback = f'\n上轮局部修订未通过程序检查（第{retry+1}次）：'+str(exc)[:1800]+'\n上轮修订：'+json.dumps(raw,ensure_ascii=False)+'\n只返回当前target的合法修改，不能换段、清空口播、变更mode或突破原片时间线。'
        if accepted:
            write_json(root/'pending-repair.json',{'base_plan_digest':digest(plan.model_dump()),
                'voice_digest':digest(voices),'plan':revised.model_dump(),
                'status':'requires_full_measurement_and_review'})
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
    if any(title_issues(clip.title) for clip in plan.clips):
        raise ValueError('标题未满足精炼与单行规则')
    if (report.get('version')!=VERSION or report.get('mode') != 'automatic_source_review' or report.get('passed') is not True or report.get('issues') != []
            or report.get('metadata_caption_policy')!=METADATA_CAPTION_POLICY
            or report.get('scene_observation_policy')!=SCENE_OBSERVATION_POLICY
            or report.get('title_policy')!=TITLE_POLICY
            or not any(f.get('purpose')=='metadata_captions' for f in report.get('frames',[]))
            or report.get('source') != source_identity(video) or report.get('plan_digest') != digest(plan.model_dump())
            or report.get('transcript_digest') != digest([s.model_dump() for s in lines])
            or report.get('voice_digest') != digest(voices) or not report.get('frames')):
        raise ValueError('自动审核记录与当前稿件、素材或声纹版本不一致')
    if any(file_hash(Path(f['path'])) != f['sha256'] for f in report['frames']):
        raise ValueError('自动审核画面已改变')
