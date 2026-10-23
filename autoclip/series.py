"""One project, source-bound checkpoints and automated long-form delivery."""
import fcntl
import json
import re
import sys
import time
from pathlib import Path

from .automatic import (VERSION as REVIEW_VERSION, FrozenAutomaticScript, assert_approval, audit_plan,
                        link_voice_evidence, repair_plan, validate_voice_library, SCENE_OBSERVATION_POLICY)
from .titles import TITLE_POLICY, TITLE_COVER_POLICY, reviewed_title_evidence
from .branding import BrandSettings, package_clip
from .config import Settings
from .continuity import load_continuity
from .deliver import export_finished
from .director import analyze
from .editorial import VERSION as ORAL_VERSION, AUDIENCE_STYLE_POLICY, require_audience_timeline
from .ingest import save_transcript, transcribe
from .llm import LLM
from .models import Line, Plan, validate_plan
from .outline import compose_outline
from .planning import VERSION as PLANNING_VERSION, plan_outline
from .render import render_clip, VERSION as RENDER_VERSION
from .narrative import NARRATIVE_POLICY, require_commentary, review_commentary, require_commentary_review, formal_long
from .report import build_report
from .research import prepare_research
from .sample import measure_sample_plan
from .util import digest, duration, read_json, run, source_identity, write_json

VERSION = 'series-1'
EXTENSIONS = {'.mp4','.mkv','.mov','.avi','.m4v','.webm'}
PRODUCTION_DURATION = {'target_seconds':1200, 'min_seconds':1080, 'max_seconds':1320}


def default_project_output(source, test_sample=False):
    source = Path(source).resolve()
    name = re.sub(r'[^\w\u4e00-\u9fff.-]+','_',source.stem if source.is_file() else source.name)
    kind = '测试样片' if test_sample else '整剧制作'
    return Path(__file__).resolve().parents[1]/'output'/kind/(name+'-'+digest(str(source))[:8])


def delivery_policy(cfg, test_sample=False):
    return {'purpose':'test_sample' if test_sample else 'production',
            'target_seconds':cfg.target_seconds,
            'min_seconds':cfg.min_seconds or cfg.target_seconds*.6,
            'max_seconds':cfg.max_seconds or cfg.target_seconds*1.8}


def check_delivery(finished, cfg, root, test_sample=False):
    """Probe even a cached deliverable before it becomes a completed result."""
    require_audience_timeline(finished.get('timeline',[]))
    policy = delivery_policy(cfg,test_sample)
    actual = duration(Path(finished['video']))
    passed = policy['min_seconds'] <= actual <= policy['max_seconds']
    check = {**policy,'actual_seconds':actual,'passed':passed}
    if not (root/'delivery-check.json').exists() or read_json(root/'delivery-check.json') != check:
        write_json(root/'delivery-check.json',check)
    if not passed:
        raise ValueError(f"交付时长{actual:.2f}秒不满足{policy['min_seconds']:.0f}–{policy['max_seconds']:.0f}秒，本集未完成")
    return {**finished,'duration':actual,'task_kind':policy['purpose'],'duration_policy':policy}


def episode_sort_key(path):
    name = str(path)
    def number(match):
        digits = {'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
        total = current = 0
        for char in match[1]:
            if char in digits:
                current = digits[char]
            else:
                total += (current or 1)*{'十':10,'百':100}[char]
                current = 0
        return '第'+str(total+current)+match[2]
    name = re.sub(r'第([零一二两三四五六七八九十百]+)([集章话])',number,name)
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r'(\d+)',name)]


def discover(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output:
        raise ValueError('项目输出目录不能就是素材目录')
    paths = [source] if source.is_file() else [p for p in source.rglob('*')
        if p.is_file() and p.suffix.lower() in EXTENSIONS and output not in p.parents]
    paths = [p for p in paths if p.suffix.lower() in EXTENSIONS]
    if not paths:
        raise ValueError('素材目录没有视频')
    return sorted(paths,key=episode_sort_key)


def checkpoint(root, name, inputs, action):
    """Only reuse a completed stage whose result and every output still match."""
    marker = root/'stages'/f'{name}.json'
    signature = digest({'v':VERSION, 'inputs':inputs})
    if marker.exists():
        old = read_json(marker)
        if old.get('signature') == signature and old.get('status') == 'completed':
            try:
                if (old['result_digest'] == digest(old['result']) and
                    all(source_identity(Path(p)) == identity for p,identity in old['artifacts'].items())):
                    return old['result']
            except OSError:
                pass
    started = time.monotonic()
    write_json(marker, {'signature':signature, 'status':'running'})
    try:
        result, artifacts = action()
        hashes = {str(Path(p).resolve()):source_identity(Path(p)) for p in artifacts}
        write_json(marker, {'signature':signature,'status':'completed','result':result,
                           'result_digest':digest(result),'artifacts':hashes,'seconds':round(time.monotonic()-started,3)})
        return result
    except Exception as exc:
        write_json(marker, {'signature':signature,'status':'failed','error':str(exc)[:2000]})
        raise


def settings_file(path, cfg):
    values = cfg.model_dump(mode='json')
    path.parent.mkdir(parents=True,exist_ok=True)
    # Settings currently contains only TOML scalar values; no credentials are saved.
    path.write_text('\n'.join(f'{k} = {json.dumps(v,ensure_ascii=False)}' for k,v in values.items() if v is not None)+'\n')


def prepare_audio_candidates(sources, transcripts, root, cfg, model_path=None, python=None):
    python = python or (Path(__file__).resolve().parents[1]/'.venv-voices/bin/python')
    if not Path(python).is_file():
        python = Path(sys.executable)
    config = root/'settings.toml'
    settings_file(config,cfg)
    args = [python,'-m','autoclip','voice-prepare',*sources,'--series',cfg.series,
            '--output',root,'--config',config]
    for transcript in transcripts:
        args += ['--transcript',transcript]
    if model_path:
        args += ['--model-path',model_path]
    run(args,timeout=43200)
    return read_json(root/'manifest.json')


def scan_production_audio(video, library, root, model_path=None, python=None):
    """Scan all speech for production; enrollment previews are intentionally sparse."""
    manifest_path = root/'manifest.json'
    if manifest_path.exists():
        cached = read_json(manifest_path)
        if not cached.get('summary',{}).get('failed'):
            return cached
    python = python or (Path(__file__).resolve().parents[1]/'.venv-voices/bin/python')
    if not Path(python).is_file():
        python = Path(sys.executable)
    args = [python,'-m','autoclip','voice-scan-series',video,'--library',library,
            '--output',root,'--threshold','0.65','--margin','0.10']
    if model_path:
        args += ['--model-path',model_path]
    run(args,timeout=43200)
    manifest = read_json(manifest_path)
    if manifest.get('summary',{}).get('failed'):
        raise ValueError('本集全片声纹扫描失败，不能用部分扫描结果继续制作')
    return manifest


def ingest_episode(video, base, cfg):
    source = source_identity(video)
    sidecar = video.with_suffix('.srt')
    inputs = {'source':source,'srt':source_identity(sidecar) if sidecar.exists() else None,
              'asr':[cfg.asr_backend,cfg.asr_model,cfg.asr_prompt,cfg.chunk_seconds]}
    folder = base/'inputs'/digest(inputs)
    def action():
        write_json(folder/'source.json',source)
        lines = transcribe(video,folder,cfg)
        seconds = duration(video)
        if not lines or any(s.end>seconds+1 for s in lines):
            raise ValueError('字幕为空或超出当前原片')
        save_transcript(lines,folder)
        return {'transcript':str((folder/'transcript.json').resolve()),'seconds':seconds}, [folder/'transcript.json',folder/'source.json']
    result = checkpoint(folder,'ingest',inputs,action)
    return folder, [Line.model_validate(x) for x in read_json(Path(result['transcript']))],result['seconds']


def complete_long_plan(video, plan, lines, voices, root, cfg, llm, validate_clip=None):
    """Shared long/short measurement and source review; no human approval artifact."""
    issues = []
    repairs = {'timing':0, 'content':0}
    # TTS alignment must not consume the later factual repair allowance.
    # Each category remains bounded, including regressions after a content edit.
    for attempt in range(2*cfg.repair_attempts+2):
        write_json(root/f'plan-attempt-{attempt}.json',plan.model_dump())
        write_json(root/'plan.json',plan.model_dump())
        timing = measure_sample_plan(video,root/'plan.json',root/'transcript.json',cfg,root)
        plan = Plan.model_validate(read_json(root/'plan-timed.json'))
        if validate_clip:
            validate_clip(plan.clips[0])
        if timing['issues']:
            from .recut import cut_candidates
            candidates = cut_candidates(plan.clips[0],timing,lines,cfg,allow_original=False)
            chosen = None
            for candidate in candidates:
                if candidate['timing']['issues']:
                    continue
                if validate_clip:
                    try:
                        validate_clip(type(plan.clips[0]).model_validate(candidate['clip']))
                    except ValueError:
                        continue
                chosen = candidate
                break
            if chosen is not None:
                before = plan.clips[0].model_copy(deep=True)
                plan.clips[0] = type(before).model_validate(chosen['clip'])
                if any(a.text!=b.text or a.evidence_ids!=b.evidence_ids or
                       (a.mode=='original' and a!=b) for a,b in zip(before.beats,plan.clips[0].beats,strict=True)):
                    raise ValueError('空隙补画面不能改变文字、原声或引用')
                write_json(root/f'automatic-timing-{attempt}.json',chosen)
                write_json(root/'plan.json',plan.model_dump())
                timing = measure_sample_plan(video,root/'plan.json',root/'transcript.json',cfg,root)
                plan = Plan.model_validate(read_json(root/'plan-timed.json'))
                if validate_clip:
                    validate_clip(plan.clips[0])
        if timing['issues']:
            category = 'timing'
            issues = []
            for item in timing['issues']:
                targets = [item['beat']] if 'beat' in item else [i for i,b in enumerate(plan.clips[0].beats) if b.mode=='narration']
                for index in targets:
                    row = timing['beats'][index]
                    issues.append({'id':f'beat-{index}', 'message':item['message'], 'measurement':row})
        else:
            category = 'content'
            metrics=require_commentary(plan.clips[0],cfg,timing)
            write_json(root/'commentary-quality.json',{'policy':NARRATIVE_POLICY,**metrics})
            report = audit_plan(video,plan,lines,voices,root,cfg,llm)
            issues = report['issues']
            if report['passed']:
                narrative=review_commentary(plan.clips[0],lines,cfg,llm,root)
                if narrative is not None:
                    report={**report,'narrative_review':narrative}
                    issues=[{'id':f'beat-{i["beat"]}','stage':'narrative_review',
                             'text_digest':digest(plan.clips[0].beats[i['beat']].text),
                             'message':'整稿解说质量：'+i['problem']+'；修订方向：'+i['direction']}
                            for i in narrative['issues']]
                    report['passed']=narrative['passed']
                    report['issues']=issues
                    write_json(root/'automatic-review.json',report)
            if report['passed']:
                write_json(root/'repair-budget.json',{'used':repairs,'limit_per_category':cfg.repair_attempts,'pending':None})
                write_json(root/'plan-approved.json',plan.model_dump())
                return plan, report
        write_json(root/f'issues-{attempt}.json',issues)
        write_json(root/'repair-budget.json',{'used':repairs,'limit_per_category':cfg.repair_attempts,'pending':category})
        if repairs[category] >= cfg.repair_attempts:
            break
        repairs[category] += 1
        write_json(root/'repair-budget.json',{'used':repairs,'limit_per_category':cfg.repair_attempts,'pending':category})
        try:
            plan = repair_plan(plan,issues,lines,voices,root,cfg,llm,attempt,source_seconds=duration(video))
        except ValueError as exc:
            from .repair import RepairNeeded
            if not isinstance(exc,RepairNeeded) or exc.report.get('stage')!='automatic_partial':raise
            revised = plan.model_dump()
            revised['clips'][0] = exc.report['clip']
            plan = Plan.model_validate(revised)
            write_json(root/f'partial-repair-{attempt}.json',{'plan':plan.model_dump(),'pending':exc.report['issues'],
                'status':'requires_full_measurement_and_review'})
        validate_plan(plan,lines,duration(video),1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening)
        if validate_clip:
            validate_clip(plan.clips[0])
    raise ValueError('自动修复达到上限，本集未通过：'+json.dumps(issues,ensure_ascii=False)[:1800])


def resume_long_reference(video, root, inputs, lines, voices, cfg, seconds):
    """Carry a source-bound measured draft across a library change, never approval."""
    import shutil
    candidates=[]
    for previous in root.parent.iterdir():
        if previous==root:continue
        try:
            old_voices=read_json(previous/'voice-evidence.json')
            old_inputs={**inputs,'bank':old_voices['library_digest'],'manifest':old_voices['manifest_digest']}
            if (old_inputs['bank']==inputs['bank'] or previous.name!=digest(old_inputs)
                    or read_json(previous/'source.json')!=inputs['source']
                    or read_json(previous/'transcript.json')!=inputs['lines']
                    or read_json(previous/'settings.json')!=inputs['cfg']
                    or old_voices['source']!=inputs['source']
                    or old_voices['transcript_digest']!=digest(inputs['lines'])):continue
            analysis=read_json(previous/'stages/analysis.json')
            required=[previous/'analysis.json',previous/'research.json']
            if (analysis.get('status')!='completed'
                    or analysis['signature']!=digest({'v':VERSION,'inputs':old_inputs})
                    or analysis['result_digest']!=digest(analysis['result'])
                    or not all(str(p.resolve()) in analysis['artifacts'] for p in required)
                    or not all(source_identity(Path(p))==identity for p,identity in analysis['artifacts'].items())):continue
            path=previous/'plan-timed.json'
            plan=Plan.model_validate(read_json(path))
            if len(plan.clips)!=1:continue
            require_commentary(plan.clips[0],cfg)
            validate_plan(plan,lines,seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening)
            lookup={s.id:s for s in lines}
            if any(lookup[i].start>=b.end for b in plan.clips[0].beats if b.mode=='narration' for i in b.evidence_ids):continue
            candidates.append((path.stat().st_mtime_ns,path,plan))
        except (OSError,ValueError,KeyError,TypeError):continue
    if not candidates:return None
    _,path,plan=max(candidates,key=lambda row:row[0])
    def reuse_analysis():
        for name in ('analysis.json','research.json'):
            shutil.copy2(path.parent/name,root/name)
        return read_json(path.parent/'stages/analysis.json')['result'],[root/'analysis.json',root/'research.json']
    # A later enrollment must be able to carry forward this run's repaired
    # draft too, rather than falling back to the original pre-enrollment text.
    checkpoint(root,'analysis',inputs,reuse_analysis)
    write_json(root/'reference-plan.json',plan.model_dump())
    write_json(root/'reference-resume.json',{'previous_plan':str(path.resolve()),
        'plan_digest':digest(plan.model_dump()),'new_voice_digest':digest(voices),
        'status':'requires_full_measurement_and_review'})
    return plan


def resume_current_long_draft(root, inputs, lines, voices, cfg, seconds, initial):
    """Resume inside the approval action so its stable checkpoint key is unchanged."""
    try:
        if (root.name!=digest(inputs) or read_json(root/'source.json')!=inputs['source']
                or read_json(root/'transcript.json')!=inputs['lines']
                or read_json(root/'settings.json')!=inputs['cfg']
                or read_json(root/'voice-evidence.json')!=voices):
            return initial
        path=root/'plan-timed.json'
        plan=Plan.model_validate(read_json(path))
        pending=root/'pending-repair.json'
        if pending.exists():
            saved=read_json(pending)
            if (saved.get('base_plan_digest')==digest(plan.model_dump())
                    and saved.get('voice_digest')==digest(voices)
                    and saved.get('status')=='requires_full_measurement_and_review'):
                plan=Plan.model_validate(saved['plan'])
                path=pending
        validate_plan(plan,lines,seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening)
        lookup={s.id:s for s in lines}
        if any(lookup[i].start>=b.end for b in plan.clips[0].beats if b.mode=='narration'
               for i in b.evidence_ids):
            return initial
    except (OSError,ValueError,KeyError,TypeError):
        return initial
    write_json(root/'current-draft-resume.json',{'previous_plan':str(path.resolve()),
        'plan_digest':digest(plan.model_dump()),'inputs_digest':digest(inputs),
        'status':'requires_full_measurement_and_review'})
    return plan


def run_episode(video, base, input_root, lines, seconds, cfg, bank, manifest, brand, index, test_sample=False):
    previous = load_continuity(video,cfg)
    inputs = {'v':VERSION,'source':source_identity(video),'lines':[s.model_dump() for s in lines],
              'delivery_policy':delivery_policy(cfg,test_sample),
              'cfg':cfg.model_dump(mode='json'),'conflict_brief':cfg.conflict_brief,'bank':digest(bank),'manifest':digest(manifest),
              'prior':previous['signature'],'episode_number':cfg.episode_order.get(str(video)), 'brand':brand.model_dump(mode='json'),
              'brand_files':[source_identity(p) for p in [brand.watermark,*brand.watermarks,brand.font] if p and p.is_file()]}
    root = base/'runs'/digest(inputs)
    root.mkdir(parents=True,exist_ok=True)
    # Write stable inputs only once, so successful checkpoints remain reusable.
    for name,value in [('source.json',inputs['source']),('transcript.json',inputs['lines']),('settings.json',cfg.model_dump(mode='json'))]:
        if not (root/name).exists():
            write_json(root/name,value)
    voices = link_voice_evidence(video,lines,bank,manifest)
    if not (root/'voice-evidence.json').exists() or read_json(root/'voice-evidence.json') != voices:
        write_json(root/'voice-evidence.json',voices)
    llm = LLM(cfg,root)
    def state(stage):
        write_json(base/'status.json',{'stage':stage,'source':str(video),'run':str(root)})
    try:
        def analysis_action():
            state('整理本集与前情')
            prepare_research(video,root,lines,seconds,cfg,llm,previous)
            maps = analyze(video,root,lines,seconds,cfg,llm,previous)
            return maps,[root/'analysis.json',root/'research.json']
        recovered=resume_long_reference(video,root,inputs,lines,voices,cfg,seconds)
        if recovered is None:
            checkpoint(root,'analysis',inputs,analysis_action)
        notes = json.dumps({'policy':voices['policy'], 'lines':[{'id':r['line_id'],'status':r['status'],
            'names':sorted({s['name'] for s in r['samples'] if s.get('name')})} for r in voices['lines']]},ensure_ascii=False)
        def outline_action():
            state('自动安排章节与镜头')
            result = plan_outline(video,root,lines,seconds,cfg,llm,notes=notes,enforce_duration=not test_sample)
            return result,[root/'outline.auto.json']
        outline = checkpoint(root,'outline',{'inputs':inputs,'planning_version':PLANNING_VERSION},outline_action) if recovered is None else None
        def writing_action():
            state('写稿与口播精修')
            plan = compose_outline(video,root/'transcript.json',root/'outline.auto.json',root,cfg,llm)
            return plan.model_dump(),[root/'plan.json']
        # Draft artifact is immutable; later timing/repair writes separate mutable files.
        def saved_draft():
            result,_ = writing_action()
            write_json(root/'initial-plan.json',result)
            return result,[root/'initial-plan.json']
        draft = (checkpoint(root,'draft',{'inputs':inputs,'outline':outline,'oral_version':ORAL_VERSION},saved_draft)
                 if recovered is None else recovered.model_dump())
        def approve_action():
            state('自动测时、内容核对与局部修复')
            generation=digest({'draft':draft,'narrative_policy':NARRATIVE_POLICY})
            marker=root/'draft-generation.json'
            pending=(resume_current_long_draft(root,inputs,lines,voices,cfg,seconds,Plan.model_validate(draft))
                     if marker.exists() and read_json(marker).get('generation')==generation else Plan.model_validate(draft))
            write_json(marker,{'generation':generation,'policy':NARRATIVE_POLICY})
            plan,report = complete_long_plan(video,pending,lines,voices,root,cfg,llm)
            return {'plan':plan.model_dump(),'report':report},[root/'plan-approved.json',root/'automatic-review.json',*[Path(f['path']) for f in report['frames']]]
        approved = checkpoint(root,'approval',{'inputs':inputs,'draft':draft,'voices':voices,'review_version':REVIEW_VERSION,
            'scene_observation_policy':SCENE_OBSERVATION_POLICY,'title_policy':TITLE_POLICY,'narrative_policy':NARRATIVE_POLICY,
            'whole_script_review':'whole-script-1',
            'audience_style_policy':AUDIENCE_STYLE_POLICY},approve_action)
        plan = Plan.model_validate(approved['plan'])
        assert_approval(approved['report'],video,plan,lines,voices)
        if formal_long(cfg):require_commentary_review(approved['report'].get('narrative_review'),plan.clips[0])
        def render_action():
            state('渲染、技术检查与包装')
            # A separate machine gate has approved the exact timed plan. Never forge development-review.json.
            local = cfg.model_copy(update={'identity_gate':False})
            clean = render_clip(video,root,plan.clips[0],index,local,FrozenAutomaticScript(),lines)
            if [b.text for b in plan.clips[0].beats] != [b['text'] for b in clean['timeline']]:
                raise ValueError('渲染文本与自动核定稿不一致')
            clean['qa']['semantic_review'] = 'automatic_source_review_not_human'
            clean['automatic_review'] = str((root/'automatic-review.json').resolve())
            clean['title_evidence_ids'] = reviewed_title_evidence(approved['report'])
            write_json(Path(clean['video']).parent/'manifest.json',clean)
            finished = package_clip(video,root,clean,index,cfg.series,brand)
            finished['production_id'] = digest([inputs,approved['plan']])
            return finished,[Path(finished[k]) for k in ('video','cover','poster') if finished.get(k)]
        finished = checkpoint(root,'render',{'inputs':inputs,'approved':approved,
            'title_cover_policy':TITLE_COVER_POLICY,'render_version':RENDER_VERSION,
            'audience_style_policy':AUDIENCE_STYLE_POLICY},render_action)
        finished = check_delivery(finished,cfg,root,test_sample)
        if source_identity(video) != inputs['source']:
            raise ValueError('制作期间原片发生变化，不能交付此版本')
        write_json(base/'source.json',inputs['source'])
        write_json(base/'transcript.json',inputs['lines'])
        write_json(base/'settings.json',cfg.model_dump(mode='json'))
        write_json(base/'research.json',read_json(root/'research.json'))
        write_json(base/'results.json',[finished])
        write_json(base/'status.json',{'stage':'已完成','source':str(video),'run':str(root),'clips':1})
        return {'source':str(video),'status':'completed','run':str(root),'video':finished['video'],
                'duration':finished['duration'],'task_kind':finished['task_kind'],
                'repairs':len(list(root.glob('automatic-repair-*.json'))),'calls_this_run':llm.calls}
    finally:
        write_json(root/'usage.json',llm.usage())
        llm.client.close()


def run_series(source, output=None, series=None, config=None, library=None, voice_manifest=None,
               model_path=None, voice_python=None, limit=None, stage='all', test_sample=False):
    output = Path(output or default_project_output(source,test_sample)).resolve()
    output.mkdir(parents=True,exist_ok=True)
    with (output/'project.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _run_series(source,output,series,config,library,voice_manifest,model_path,voice_python,limit,stage,test_sample)


def failed_episode_voice_followup(video, base, lines, voices, cfg, library, model_path=None, voice_python=None):
    """Resume only a current, source-bound failed review into human enrollment."""
    from .short_voices import pending_voice_followup, prepare_voice_followup
    status=read_json(base/'status.json') if (base/'status.json').exists() else {}
    error=str(status.get('error',''))
    # A legacy waiting status may have dropped the actual service error.
    # Read only this episode's bound approval checkpoint, never another path.
    previous=Path(status['run']).resolve() if status.get('run') else None
    if previous and previous.parent==(base/'runs').resolve() and (previous/'stages/approval.json').is_file():
        approval=read_json(previous/'stages/approval.json')
        if approval.get('status')=='failed' and not error:error=str(approval.get('error',''))
    interrupted=error.startswith(('已达到本次 API 调用次数上限','模型调用失败','模型接口 HTTP','配音服务失败'))
    if interrupted or (error.startswith('自动审核批次') and '格式或引用校验失败' in error):
        # Older versions could create a voice task from a stale report after a
        # protocol failure. Such a task has no completed current identity audit.
        pointer=base/'voice-followup.json'
        if pointer.exists():
            task=read_json(pointer)
            if task.get('status')=='waiting_voice_confirmation':
                write_json(pointer,{**task,'status':'superseded','reason':'服务中断或审核响应校验失败，先继续自动核对'})
        return None
    pending=pending_voice_followup(video,lines,voices,base,cfg,library)
    if pending:return pending
    if status.get('status')!='failed' or not status.get('run'):return None
    root=Path(status['run']).resolve()
    if root.parent!=(base/'runs').resolve():return None
    if not all((root/name).is_file() for name in ('settings.json','source.json','voice-evidence.json')):return None
    if (read_json(root/'settings.json')!=cfg.model_dump(mode='json')
            or read_json(root/'source.json')!=source_identity(video)
            or read_json(root/'voice-evidence.json')!=voices):return None
    return prepare_voice_followup(video,lines,voices,base,cfg,library,[root],model_path,voice_python)


def _run_series(source,output,series,config,library,voice_manifest,model_path,voice_python,limit,stage,test_sample):
    if stage not in {'all','prepare'} or (limit is not None and limit<1):
        raise ValueError('stage应为all/prepare，limit须为正数')
    source = Path(source).resolve()
    saved_path = output/'project.json'
    saved = read_json(saved_path) if saved_path.exists() else {}
    if saved and saved['source'] != str(source):
        raise ValueError('此项目绑定其他素材目录，请使用另一输出目录')
    purpose = 'test_sample' if test_sample else 'production'
    if saved.get('purpose') and saved['purpose'] != purpose:
        raise ValueError('正式制作与测试样片不能混用同一项目目录；测试续跑须显式加--test-sample，正式制作请使用另一输出目录')
    config = Path(config or saved.get('config') or Path(__file__).resolve().parents[1]/'configs.series.toml').resolve()
    cfg = Settings.load(config)
    cfg.series = series or saved.get('series') or (source.stem if source.is_file() else source.name)
    cfg.output = output/'episodes'
    cfg.continuity_history = cfg.output
    cfg.media_cache = output/'media-cache'
    cfg.identity_registry = None  # The new path uses audio + contextual visual evidence, not manually enrolled faces.
    cfg.identity_gate = False
    if cfg.duration_profile != 'long' or not cfg.vision:
        raise ValueError('整剧自动入口需要long配置及画面审核')
    if not test_sample:
        # One production contract for every episode, including legacy config files.
        cfg = cfg.model_copy(update=PRODUCTION_DURATION)
    # Story continuity is a project rule, independent of length or test purpose.
    cfg.narrative_mode = 'story_first'
    cfg.clips = 1
    library = Path(library or saved.get('library') or output/'voice-enrollment/registry.json').resolve()
    voice_manifest = Path(voice_manifest or saved['voice_manifest']).resolve() if voice_manifest or saved.get('voice_manifest') else None
    model_path = Path(model_path or saved['model_path']).resolve() if model_path or saved.get('model_path') else None
    voice_python = Path(voice_python or saved['voice_python']).resolve() if voice_python or saved.get('voice_python') else None
    paths = discover(source,output)
    cfg.episode_order = {str(path):index+1 for index,path in enumerate(paths)}
    project = {'version':VERSION,'source':str(source),'series':cfg.series,'config':str(config),
               'purpose':purpose,'duration_policy':delivery_policy(cfg,test_sample),'editorial_policy':'story_first',
               'effective_settings':cfg.model_dump(mode='json'),
               'library':str(library),'voice_manifest':str(voice_manifest) if voice_manifest else None,
               'model_path':str(model_path) if model_path else None,'voice_python':str(voice_python) if voice_python else None,
               'episodes':[str(p) for p in paths]}
    if saved and (saved.get('duration_policy') != project['duration_policy']
                  or saved.get('editorial_policy') != project['editorial_policy']):
        for status in cfg.output.glob('*/status.json'):
            row = read_json(status)
            if row.get('stage') in {'已完成','部分完成'}:
                write_json(status,{**row,'stage':'制作规则已变更，等待自动复核'})
    write_json(saved_path,project)
    write_json(output/'series-status.json',{'status':'running','stage':'initializing','episodes':[]})
    # Reject short/invalid production inputs before transcription, voice scanning or model calls.
    source_errors = {}
    for video in paths[:limit] if limit else paths:
        if not test_sample:
            try:
                seconds = duration(video)
                if seconds < cfg.min_seconds:
                    raise ValueError(f'正式制作需要完整单集素材且至少{cfg.min_seconds:.0f}秒；当前素材{seconds:.2f}秒，不能作为约20分钟长片交付。短素材验收须显式使用--test-sample')
            except Exception as exc:
                source_errors[video] = str(exc)[:2000]
    selected = paths[:limit] if limit else paths
    eligible = [p for p in selected if p not in source_errors]
    if not eligible:
        outcomes = []
        for video,message in source_errors.items():
            outcome = {'source':str(video),'status':'failed','error':message,'calls_this_run':0}
            base = cfg.output/(video.stem+'-'+digest(source_identity(video)))
            write_json(base/'status.json',{'stage':'素材预检失败',**outcome})
            outcomes.append(outcome)
        result = {'status':'partial_failure','purpose':purpose,'duration_policy':delivery_policy(cfg,test_sample),'episodes':outcomes}
        write_json(output/'series-status.json',result)
        write_json(output/'production-metrics.json',{'episodes':len(outcomes),'completed':0,'failed':len(outcomes),
            'new_model_calls':0,'automatic_repairs':0,'human_steps':['voice_enrollment'],'human_revision_minutes':None})
        from .feedback import summarize_project
        summarize_project(output)
        return result
    if library.exists():
        from .short_voices import apply_confirmed_followups
        apply_confirmed_followups(cfg.output,library,cfg.series)
    bank = read_json(library) if library.exists() else None
    if not bank or not bank.get('characters'):
        enrollment = output/'voice-enrollment'
        manifest_path = enrollment/'manifest.json'
        if not manifest_path.exists():
            seed = eligible[:2]
            transcripts = []
            for video in seed:
                base = cfg.output/(video.stem+'-'+digest(source_identity(video)))
                input_root,_,_ = ingest_episode(video,base,cfg)
                transcripts.append(input_root/'transcript.json')
            prepare_audio_candidates(seed,transcripts,enrollment,cfg,model_path,voice_python)
        result = {'status':'waiting_voice_confirmation','purpose':purpose,'duration_policy':delivery_policy(cfg,test_sample),
                  'review_root':str(enrollment),
                  'instruction':'仅此步骤需要人工：voice-review --output '+str(enrollment)+'；确认后重跑同一series命令。'}
        write_json(output/'series-status.json',result)
        return result
    # The same reference validation is used by short-video production.
    validate_voice_library(bank,cfg.series)
    old_digest = saved.get('library_digest')
    project['library_digest'] = digest(bank)
    if old_digest and old_digest != project['library_digest']:
        write_json(output/'reference-impact.json',{'previous':old_digest,'current':project['library_digest'],
            'affected_episodes':[str(p) for p in paths],'action':'后续处理生成新版本并重做关联与审核；历史版本保留'})
        for status in cfg.output.glob('*/status.json'):
            row = read_json(status)
            if row.get('stage') == '已完成':
                write_json(status,{**row,'stage':'参考已变更，等待自动复核'})
    write_json(saved_path,project)
    candidates = read_json(voice_manifest) if voice_manifest else None
    brand_path = Path(__file__).resolve().parents[1]/'branding.toml'
    brand = BrandSettings.load(brand_path)
    outcomes = []
    for index,video in enumerate(selected):
        base = cfg.output/(video.stem+'-'+digest(source_identity(video)))
        write_json(output/'series-status.json',{'status':'running','current_episode':str(video),
                                              'stage':'preparing_episode','episodes':outcomes})
        for marker in cfg.output.glob('*/source.json'):
            old = read_json(marker)
            if old.get('path') == str(video) and marker.parent != base:
                write_json(marker.parent/'status.json',{'stage':'原片已变更，旧版本归档','source':str(video)})
        episode_started = time.time()
        current_voices=None
        try:
            if video in source_errors:
                raise ValueError(source_errors[video])
            if digest(read_json(library)) != digest(bank):
                raise ValueError('声纹库在运行期间改变，重跑后按新版本继续')
            input_root,lines,seconds = ingest_episode(video,base,cfg)
            if stage == 'prepare':
                outcomes.append({'source':str(video),'status':'prepared'})
            else:
                current_manifest = candidates
                if (current_manifest is None or current_manifest.get('model') != bank.get('model')
                        or not any(s['source']==source_identity(video) for s in current_manifest['sources'])):
                    voice_root = base/'voice-scan'/digest(['vad-production-1',source_identity(video),digest(bank),str(model_path)])
                    current_manifest = scan_production_audio(video,library,voice_root,model_path,voice_python)
                current_voices=link_voice_evidence(video,lines,bank,current_manifest)
                pending=failed_episode_voice_followup(video,base,lines,current_voices,cfg,library,model_path,voice_python)
                if pending:
                    active=read_json(base/'status.json')
                    outcome={'source':str(video),'status':'waiting_voice_confirmation','run':active.get('run'),
                             'voice_confirmation':pending,'calls_this_run':0}
                    write_json(base/'status.json',{'stage':'等待新增声纹确认',**outcome})
                else:
                    outcome = run_episode(video,base,input_root,lines,seconds,cfg,bank,current_manifest,brand,index,test_sample)
                if digest(read_json(library)) != digest(bank):
                    raise ValueError('声纹库在审核期间改变，本集旧核定不交付；重跑后自动复核')
                outcomes.append(outcome)
        except Exception as exc:
            active = read_json(base/'status.json') if (base/'status.json').exists() else {}
            usage_path = Path(active.get('run') or base)/'usage.json'
            usage = read_json(usage_path) if usage_path.exists() and usage_path.stat().st_mtime >= episode_started else {}
            outcome = {'source':str(video),'status':'failed','error':str(exc)[:2000],
                       'run':active.get('run'), 'calls_this_run':usage.get('calls_this_run',0)}
            write_json(base/'status.json',{'stage':'自动处理失败，可续跑',**outcome})
            if current_voices is not None:
                try:
                    pending=(failed_episode_voice_followup(video,base,lines,current_voices,cfg,library,model_path,voice_python)
                             if digest(read_json(library))==digest(bank) else None)
                    if pending:
                        outcome.update(status='waiting_voice_confirmation',voice_confirmation=pending)
                        write_json(base/'status.json',{'stage':'等待新增声纹确认',**outcome})
                except Exception as followup_error:
                    write_json(base/'voice-followup-error.json',{'error':str(followup_error)[:1500]})
            outcomes.append(outcome)
        result = {'status':'running','episodes':outcomes}
        write_json(output/'series-status.json',result)
        write_json(output/'production-metrics.json',{'episodes':len(outcomes),
            'completed':sum(o['status']=='completed' for o in outcomes),
            'failed':sum(o['status']=='failed' for o in outcomes),
            'waiting_voice_confirmation':sum(o['status']=='waiting_voice_confirmation' for o in outcomes),
            'new_model_calls':sum(o.get('calls_this_run',0) for o in outcomes),
            'automatic_repairs':sum(o.get('repairs',0) for o in outcomes),
            'human_steps':['voice_enrollment'],'human_revision_minutes':None})
    if any(o['status']=='completed' for o in outcomes):
        export_finished(cfg.output)
    build_report(cfg.output)
    result = {'status':'partial_failure' if any(o['status']=='failed' for o in outcomes) else
              'waiting_voice_confirmation' if any(o['status']=='waiting_voice_confirmation' for o in outcomes) else ('prepared' if stage=='prepare' else 'completed'),
              'purpose':purpose,'duration_policy':delivery_policy(cfg,test_sample),
              'episodes':outcomes,'gallery':str(cfg.output/'index.html'),'exports':str(cfg.output/'成片')}
    write_json(output/'series-status.json',result)
    from .feedback import summarize_project
    summarize_project(output)
    return result
