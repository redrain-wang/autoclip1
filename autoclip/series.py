"""One project, source-bound checkpoints and automated long-form delivery."""
import fcntl
import json
import re
import sys
import time
from pathlib import Path

from .automatic import (FrozenAutomaticScript, assert_approval, audit_plan,
                        link_voice_evidence, repair_plan)
from .branding import BrandSettings, package_clip
from .config import Settings
from .continuity import load_continuity
from .deliver import export_finished
from .director import analyze
from .ingest import save_transcript, transcribe
from .llm import LLM
from .models import Line, Plan, validate_plan
from .outline import compose_outline
from .planning import plan_outline
from .render import render_clip
from .report import build_report
from .research import prepare_research
from .sample import measure_sample_plan
from .util import digest, duration, read_json, run, source_identity, write_json

VERSION = 'series-1'
EXTENSIONS = {'.mp4','.mkv','.mov','.avi','.m4v','.webm'}


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


def complete_long_plan(video, plan, lines, voices, root, cfg, llm):
    """Measure, review and repair without requesting a human approval artifact."""
    issues = []
    for attempt in range(cfg.repair_attempts+1):
        write_json(root/f'plan-attempt-{attempt}.json',plan.model_dump())
        write_json(root/'plan.json',plan.model_dump())
        timing = measure_sample_plan(video,root/'plan.json',root/'transcript.json',cfg,root)
        plan = Plan.model_validate(read_json(root/'plan-timed.json'))
        if timing['issues']:
            issues = []
            for item in timing['issues']:
                targets = [item['beat']] if 'beat' in item else [i for i,b in enumerate(plan.clips[0].beats) if b.mode=='narration']
                for index in targets:
                    row = timing['beats'][index]
                    issues.append({'id':f'beat-{index}', 'message':item['message'], 'measurement':row})
        else:
            report = audit_plan(video,plan,lines,voices,root,cfg,llm)
            issues = report['issues']
            if report['passed']:
                write_json(root/'plan-approved.json',plan.model_dump())
                return plan, report
        write_json(root/f'issues-{attempt}.json',issues)
        if attempt == cfg.repair_attempts:
            break
        plan = repair_plan(plan,issues,lines,voices,root,cfg,llm,attempt)
        validate_plan(plan,lines,duration(video),1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening)
    raise ValueError('自动修复达到上限，本集未通过：'+json.dumps(issues,ensure_ascii=False)[:1800])


def run_episode(video, base, input_root, lines, seconds, cfg, bank, manifest, brand, index):
    previous = load_continuity(video,cfg)
    inputs = {'v':VERSION,'source':source_identity(video),'lines':[s.model_dump() for s in lines],
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
    if not (root/'voice-evidence.json').exists():
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
        checkpoint(root,'analysis',inputs,analysis_action)
        notes = json.dumps({'policy':voices['policy'], 'lines':[{'id':r['line_id'],'status':r['status'],
            'names':sorted({s['name'] for s in r['samples'] if s.get('name')})} for r in voices['lines']]},ensure_ascii=False)
        def outline_action():
            state('自动安排章节与镜头')
            result = plan_outline(video,root,lines,seconds,cfg,llm,notes=notes)
            return result,[root/'outline.auto.json']
        outline = checkpoint(root,'outline',inputs,outline_action)
        def writing_action():
            state('写稿与口播精修')
            plan = compose_outline(video,root/'transcript.json',root/'outline.auto.json',root,cfg,llm)
            return plan.model_dump(),[root/'plan.json']
        # Draft artifact is immutable; later timing/repair writes separate mutable files.
        def saved_draft():
            result,_ = writing_action()
            write_json(root/'initial-plan.json',result)
            return result,[root/'initial-plan.json']
        draft = checkpoint(root,'draft',{'inputs':inputs,'outline':outline},saved_draft)
        def approve_action():
            state('自动测时、内容核对与局部修复')
            plan,report = complete_long_plan(video,Plan.model_validate(draft),lines,voices,root,cfg,llm)
            return {'plan':plan.model_dump(),'report':report},[root/'plan-approved.json',root/'automatic-review.json',*[Path(f['path']) for f in report['frames']]]
        approved = checkpoint(root,'approval',{'inputs':inputs,'draft':draft,'voices':voices},approve_action)
        plan = Plan.model_validate(approved['plan'])
        assert_approval(approved['report'],video,plan,lines,voices)
        def render_action():
            state('渲染、技术检查与包装')
            # A separate machine gate has approved the exact timed plan. Never forge development-review.json.
            local = cfg.model_copy(update={'identity_gate':False})
            clean = render_clip(video,root,plan.clips[0],index,local,FrozenAutomaticScript(),lines)
            if [b.text for b in plan.clips[0].beats] != [b['text'] for b in clean['timeline']]:
                raise ValueError('渲染文本与自动核定稿不一致')
            clean['qa']['semantic_review'] = 'automatic_source_review_not_human'
            clean['automatic_review'] = str((root/'automatic-review.json').resolve())
            write_json(Path(clean['video']).parent/'manifest.json',clean)
            finished = package_clip(video,root,clean,index,cfg.series,brand)
            finished['production_id'] = digest([inputs,approved['plan']])
            return finished,[Path(finished[k]) for k in ('video','cover','poster') if finished.get(k)]
        finished = checkpoint(root,'render',{'inputs':inputs,'approved':approved},render_action)
        if source_identity(video) != inputs['source']:
            raise ValueError('制作期间原片发生变化，不能交付此版本')
        write_json(base/'source.json',inputs['source'])
        write_json(base/'transcript.json',inputs['lines'])
        write_json(base/'settings.json',cfg.model_dump(mode='json'))
        write_json(base/'research.json',read_json(root/'research.json'))
        write_json(base/'results.json',[finished])
        write_json(base/'status.json',{'stage':'已完成','source':str(video),'run':str(root),'clips':1})
        return {'source':str(video),'status':'completed','run':str(root),'video':finished['video'],
                'repairs':len(list(root.glob('automatic-repair-*.json'))),'calls_this_run':llm.calls}
    finally:
        write_json(root/'usage.json',llm.usage())
        llm.client.close()


def run_series(source, output, series=None, config=None, library=None, voice_manifest=None,
               model_path=None, voice_python=None, limit=None, stage='all'):
    output = Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    with (output/'project.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _run_series(source,output,series,config,library,voice_manifest,model_path,voice_python,limit,stage)


def _run_series(source,output,series,config,library,voice_manifest,model_path,voice_python,limit,stage):
    if stage not in {'all','prepare'} or (limit is not None and limit<1):
        raise ValueError('stage应为all/prepare，limit须为正数')
    source = Path(source).resolve()
    saved_path = output/'project.json'
    saved = read_json(saved_path) if saved_path.exists() else {}
    if saved and saved['source'] != str(source):
        raise ValueError('此项目绑定其他素材目录，请使用另一输出目录')
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
    library = Path(library or saved.get('library') or output/'voice-enrollment/registry.json').resolve()
    voice_manifest = Path(voice_manifest or saved['voice_manifest']).resolve() if voice_manifest or saved.get('voice_manifest') else None
    model_path = Path(model_path or saved['model_path']).resolve() if model_path or saved.get('model_path') else None
    voice_python = Path(voice_python or saved['voice_python']).resolve() if voice_python or saved.get('voice_python') else None
    paths = discover(source,output)
    cfg.episode_order = {str(path):index+1 for index,path in enumerate(paths)}
    project = {'version':VERSION,'source':str(source),'series':cfg.series,'config':str(config),
               'library':str(library),'voice_manifest':str(voice_manifest) if voice_manifest else None,
               'model_path':str(model_path) if model_path else None,'voice_python':str(voice_python) if voice_python else None,
               'episodes':[str(p) for p in paths]}
    write_json(saved_path,project)
    write_json(output/'series-status.json',{'status':'running','stage':'initializing','episodes':[]})
    bank = read_json(library) if library.exists() else None
    if not bank or not bank.get('characters'):
        enrollment = output/'voice-enrollment'
        manifest_path = enrollment/'manifest.json'
        if not manifest_path.exists():
            seed = paths[:2]
            transcripts = []
            for video in seed:
                base = cfg.output/(video.stem+'-'+digest(source_identity(video)))
                input_root,_,_ = ingest_episode(video,base,cfg)
                transcripts.append(input_root/'transcript.json')
            prepare_audio_candidates(seed,transcripts,enrollment,cfg,model_path,voice_python)
        result = {'status':'waiting_voice_confirmation','review_root':str(enrollment),
                  'instruction':'仅此步骤需要人工：voice-review --output '+str(enrollment)+'；确认后重跑同一series命令。'}
        write_json(output/'series-status.json',result)
        return result
    if bank['series'] != cfg.series:
        raise ValueError('人工声纹库剧名与项目不一致')
    # Validate all references even for a prepare-only invocation.
    for person in bank['characters']:
        for sample in person['samples']:
            if sample.get('status') != 'human_confirmed' or source_identity(Path(sample['source']['path'])) != sample['source']:
                raise ValueError('人工声纹参考无效或已改变')
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
    for index,video in enumerate(paths[:limit] if limit else paths):
        base = cfg.output/(video.stem+'-'+digest(source_identity(video)))
        write_json(output/'series-status.json',{'status':'running','current_episode':str(video),
                                              'stage':'preparing_episode','episodes':outcomes})
        for marker in cfg.output.glob('*/source.json'):
            old = read_json(marker)
            if old.get('path') == str(video) and marker.parent != base:
                write_json(marker.parent/'status.json',{'stage':'原片已变更，旧版本归档','source':str(video)})
        episode_started = time.time()
        try:
            if digest(read_json(library)) != digest(bank):
                raise ValueError('声纹库在运行期间改变，重跑后按新版本继续')
            input_root,lines,seconds = ingest_episode(video,base,cfg)
            if stage == 'prepare':
                outcomes.append({'source':str(video),'status':'prepared'})
            else:
                current_manifest = candidates
                if (current_manifest is None or current_manifest.get('model') != bank.get('model')
                        or not any(s['source']==source_identity(video) for s in current_manifest['sources'])):
                    voice_root = base/'voice-candidates'/digest([source_identity(video),[s.model_dump() for s in lines],str(model_path),bank['model']])
                    if (voice_root/'manifest.json').exists():
                        current_manifest = read_json(voice_root/'manifest.json')
                    else:
                        current_manifest = prepare_audio_candidates([video],[input_root/'transcript.json'],voice_root,cfg,model_path,voice_python)
                outcome = run_episode(video,base,input_root,lines,seconds,cfg,bank,current_manifest,brand,index)
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
            outcomes.append(outcome)
        result = {'status':'running','episodes':outcomes}
        write_json(output/'series-status.json',result)
        write_json(output/'production-metrics.json',{'episodes':len(outcomes),
            'completed':sum(o['status']=='completed' for o in outcomes),
            'failed':sum(o['status']=='failed' for o in outcomes),
            'new_model_calls':sum(o.get('calls_this_run',0) for o in outcomes),
            'automatic_repairs':sum(o.get('repairs',0) for o in outcomes),
            'human_steps':['voice_enrollment'],'human_revision_minutes':None})
    if any(o['status']=='completed' for o in outcomes):
        export_finished(cfg.output)
    build_report(cfg.output)
    result = {'status':'partial_failure' if any(o['status']=='failed' for o in outcomes) else ('prepared' if stage=='prepare' else 'completed'),
              'episodes':outcomes,'gallery':str(cfg.output/'index.html')}
    write_json(output/'series-status.json',result)
    from .feedback import summarize_project
    summarize_project(output)
    return result
