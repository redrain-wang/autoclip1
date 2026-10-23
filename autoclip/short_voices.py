"""Shared voice enrollment, evidence and approval gates for short films."""
from pathlib import Path

from .automatic import assert_approval, link_voice_evidence, validate_voice_library
from .models import Plan, validate_plan
from .util import digest, read_json, source_identity, write_json


def prepare_project(source, paths, cfg, library=None, voice_manifest=None, model_path=None, voice_python=None):
    from .series import ingest_episode, prepare_audio_candidates
    output = cfg.output
    if (output/'project.json').exists():
        raise ValueError('短片与长片项目不能共用同一输出目录')
    marker = output/'short-project.json'
    saved = read_json(marker) if marker.exists() else {}
    if saved and saved['source'] != str(source):
        raise ValueError('短片项目绑定其他素材，请使用另一输出目录')
    if saved and saved['series'] != cfg.series:
        raise ValueError('短片项目剧名已改变，请使用另一输出目录')
    enrollment = output/'voice-enrollment'
    library = Path(library or saved.get('library') or enrollment/'registry.json').resolve()
    if not library.exists() and library != (enrollment/'registry.json').resolve():
        raise ValueError(f'声纹库文件不存在：{library}')
    if library.exists():
        apply_confirmed_followups(output,library,cfg.series)
    def location(name,value):
        return Path(value or saved[name]).resolve() if value or saved.get(name) else None
    voice_manifest = location('voice_manifest',voice_manifest)
    model_path = location('model_path',model_path)
    voice_python = location('voice_python',voice_python)
    project = {'version':1,'source':str(source),'series':cfg.series,'library':str(library),
        'voice_manifest':str(voice_manifest) if voice_manifest else None,
        'model_path':str(model_path) if model_path else None,'voice_python':str(voice_python) if voice_python else None,
        'episodes':[str(p) for p in paths],'editorial_policy':'story_first','identity_policy':'voice_and_source_context'}
    bank = read_json(library) if library.exists() else None
    if bank and bank.get('characters'):
        validate_voice_library(bank,cfg.series)
        project['library_digest'] = digest(bank)
    elif library.exists() and library != (enrollment/'registry.json').resolve():
        raise ValueError('指定的声纹库没有人工确认人物')
    write_json(marker,project)
    if not bank or not bank.get('characters'):
        if not (enrollment/'manifest.json').exists():
            seed = paths[:2]
            transcripts = []
            for video in seed:
                base = output/(video.stem+'-'+digest(source_identity(video))[:8])
                folder,_,_ = ingest_episode(video,base,cfg)
                transcripts.append(folder/'transcript.json')
            prepare_audio_candidates(seed,transcripts,enrollment,cfg,model_path,voice_python)
        result = {'status':'waiting_voice_confirmation','review_root':str(enrollment),
            'instruction':f'仅声纹确认需人工：voice-review --output {enrollment}；确认后重跑同一短片命令。'}
        write_json(output/'voice-status.json',result)
        return result
    if saved.get('library_digest') and saved['library_digest'] != project['library_digest']:
        write_json(output/'voice-reference-impact.json',{'previous':saved['library_digest'],
            'current':project['library_digest'],'action':'每条短片重新关联与核对，旧媒体保留'})
        for state in output.glob('*/status.json'):
            row = read_json(state)
            if row.get('stage') in {'已完成','部分完成'}:
                write_json(state,{**row,'stage':'声纹参考已变更，等待自动复核'})
    write_json(output/'voice-status.json',{'status':'ready','library_digest':project['library_digest']})
    return {**project,'status':'ready','bank':bank,'library':library,'voice_manifest':voice_manifest,
            'model_path':model_path,'voice_python':voice_python}


def apply_confirmed_followups(output, library, series):
    """Only finished human enrollment can append references to its original bank."""
    import fcntl
    from .voices import registry, merge_libraries
    for pointer in output.glob('*/voice-followup.json'):
        task = read_json(pointer)
        if task.get('status') in {'applied','resolved'} or task.get('library')!=str(library.resolve()):continue
        folder = Path(task['review_root'])
        if not (folder/'review.json').exists():continue
        manifest,review = read_json(folder/'manifest.json'),read_json(folder/'review.json')
        if review.get('enrollment',{}).get('status')!='finished':continue
        if review['dataset_id']!=manifest['dataset_id']:raise ValueError('新增声纹确认记录与候选批次不符')
        if task.get('source')!=source_identity(Path(task['source']['path'])):
            raise ValueError('新增声音的原片已改变，请重新准备')
        if not any(s['source']==task['source'] for s in manifest['sources']):
            raise ValueError('新增声音确认页与原片不符')
        additions = registry(manifest,review)
        if not additions.get('characters'):continue
        validate_voice_library(additions,series)
        with library.with_suffix('.enrollment.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            current = read_json(library)
            validate_voice_library(current,series)
            combined = merge_libraries([current,additions])
            old_samples = {s['id'] for p in current['characters'] for s in p['samples']}
            if all(s['id'] in old_samples for p in additions['characters'] for s in p['samples']):
                combined = current  # A crash after writing the bank must not append it twice.
            validate_voice_library(combined,series)
            write_json(library.parent/'history'/f'{library.stem}-{digest(current)}.json',current)
            write_json(library,combined)
        write_json(pointer,{**task,'status':'applied','review_revision':review['revision'],
                            'library_digest':digest(combined)})


def prepare_voice_followup(video, lines, voices, root, cfg, library, story_roots,
                           model_path=None, voice_python=None):
    """Collect only unresolved identity evidence; never prefill character names."""
    import re
    from .series import prepare_audio_candidates
    from .voice_review import ReviewStore
    from .voices import candidate_spans
    selected,requests = {},[]
    lookup = {s.id:s for s in lines}
    transcript_digest = digest([s.model_dump() for s in lines])
    for folder in story_roots:
        if not (folder/'automatic-review.json').exists():continue
        report=read_json(folder/'automatic-review.json')
        state_path=folder/'automatic-review-state.json'
        if state_path.exists():
            state=read_json(state_path)
            if state.get('status')!='completed' or state.get('plan_digest')!=report.get('plan_digest'):
                continue  # An interrupted new review cannot recycle an older identity problem.
        paths=[folder/'plan-timed.json']
        for batch in report.get('batches',[]):
            parent=Path(batch).resolve().parent
            if parent.parent==(folder/'automatic-review').resolve():paths.append(parent/'plan.json')
        plan=None
        for path in dict.fromkeys(paths):
            if not path.is_file():continue
            candidate=Plan.model_validate(read_json(path))
            if digest(candidate.model_dump())==report.get('plan_digest'):
                plan=candidate
                break
        if plan is None:continue
        if (report.get('passed') or report.get('source')!=source_identity(video)
                or report.get('transcript_digest')!=transcript_digest
                or report.get('plan_digest')!=digest(plan.model_dump()) or report.get('voice_digest')!=digest(voices)):
            continue
        for issue in report.get('issues',[]):
            if issue['message'].startswith('被提及人物须在'):
                continue  # A reference/citation annotation cannot be fixed by enrolling a voice.
            match = re.fullmatch(r'beat-(\d+)',issue['id'])
            if not match or not re.search(r'声音|身份|人物|主语|说话人',issue['message']):continue
            index=int(match[1])
            if index>=len(plan.clips[0].beats):continue
            beat=plan.clips[0].beats[index]
            possible=[lookup[i] for i in beat.evidence_ids if i in lookup]
            valid_ids={s['line_id'] for s in candidate_spans(possible,max(s.end for s in lines))}
            candidates=sorted((s for s in possible if s.id in valid_ids and s.confidence>=.4),
                               key=lambda s:-(s.end-s.start)*s.confidence)[:2]
            selected.update((s.id,s) for s in candidates)
            requests.append({'unit':issue['id'],'reason':issue['message'],'line_ids':[s.id for s in candidates]})
    if not selected:return None
    chosen=sorted(selected.values(),key=lambda s:(s.start,s.id))[:12]
    bank=read_json(library)
    signature=digest({'source':source_identity(video),'bank':digest(bank),'lines':[s.model_dump() for s in chosen]})
    folder=root/'voice-followups'/signature
    if not (folder/'manifest.json').exists():
        write_json(folder/'input'/'source.json',source_identity(video))
        write_json(folder/'input'/'transcript.json',[s.model_dump() for s in chosen])
        manifest=prepare_audio_candidates([video],[folder/'input'/'transcript.json'],folder,cfg,model_path,voice_python)
    else:
        manifest=read_json(folder/'manifest.json')
    if manifest['model']!=bank['model'] or manifest['series']!=bank['series']:
        raise ValueError('新增声纹候选与原库模型或剧名不一致')
    if not any(s['source']==source_identity(video) for s in manifest['sources']):
        raise ValueError('新增声纹候选原片不一致')
    if not manifest.get('baseline_library_digest'):
        if (folder/'review.json').exists():raise ValueError('新增声音批次初始化不完整，请使用新的输出目录')
        manifest={**manifest,'baseline_library_digest':digest(bank),
            'dataset_id':digest([manifest['dataset_id'],digest(bank)]),'automatic_gap_requests':requests}
        write_json(folder/'baseline-library.json',bank)
        write_json(folder/'manifest.json',manifest)
    if manifest['baseline_library_digest']!=digest(bank):raise ValueError('新增声音批次基础库不一致')
    ReviewStore(folder)  # exports an empty local bank; only the human may name a sample
    task={'status':'waiting_voice_confirmation','review_root':str(folder.resolve()),
          'library':str(library.resolve()),'source':source_identity(video),'sample_lines':[s.id for s in chosen],
          'library_digest':digest(bank),'voice_digest':digest(voices),'transcript_digest':transcript_digest,
          'settings_digest':digest(cfg.model_dump(mode='json')),
          'instruction':f'voice-review --output "{folder.resolve()}"；试听确认后重跑原制作命令，系统会备份并合并新增人工参考。'}
    write_json(root/'voice-followup.json',task)
    return task


def pending_voice_followup(video, lines, voices, root, cfg, library):
    pointer=root/'voice-followup.json'
    if not pointer.exists():return None
    task=read_json(pointer)
    if task.get('status')=='waiting_voice_confirmation' and task.get('review_root'):
        manifest_path=Path(task['review_root'])/'manifest.json'
        requests=read_json(manifest_path).get('automatic_gap_requests',[]) if manifest_path.is_file() else []
        if requests and all(r.get('reason','').startswith('被提及人物须在') for r in requests):
            write_json(pointer,{**task,'status':'superseded','reason':'仅被提及者的引用校验问题，先继续自动核验，不要求新增声纹'})
            return None
    if (task.get('status')=='waiting_voice_confirmation' and task.get('source')==source_identity(video)
            and task.get('library')==str(library.resolve()) and task.get('library_digest')==digest(read_json(library))
            and task.get('voice_digest')==digest(voices)
            and task.get('transcript_digest')==digest([s.model_dump() for s in lines])
            and task.get('settings_digest')==digest(cfg.model_dump(mode='json'))):
        return task
    return None


def resume_reference_plan(video, lines, voices, story, cfg, root, seconds):
    """Reuse text after a reference change, never its previous approval."""
    candidates=[]
    source=source_identity(video)
    transcript_digest=digest([s.model_dump() for s in lines])
    for previous in root.parent.iterdir():
        if not all((previous/name).is_file() for name in
                ('source.json','transcript.json','voice-evidence.json','plan-timed.json')):continue
        try:
            old_voices=read_json(previous/'voice-evidence.json')
            if (read_json(previous/'source.json')!=source or old_voices.get('source')!=source
                    or old_voices.get('transcript_digest')!=transcript_digest
                    or digest(read_json(previous/'transcript.json'))!=transcript_digest
                    or (previous!=root and old_voices.get('library_digest')==voices.get('library_digest'))
                    or (previous==root and old_voices!=voices)
                    or previous.name!=digest({'voices':old_voices,'cfg':cfg.model_dump(mode='json'),'story':story.model_dump()})):
                continue
            approved=previous!=root and (previous/'plan-approved.json').exists()
            path=previous/('plan-approved.json' if approved else 'plan-timed.json')
            plan=Plan.model_validate(read_json(path))
            if len(plan.clips)!=1:continue
            validate_plan(plan,lines,seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening)
            lookup={s.id:s for s in lines}
            if any(lookup[i].start>=b.end for b in plan.clips[0].beats if b.mode=='narration' for i in b.evidence_ids):continue
            if approved:
                assert_approval(read_json(previous/'automatic-review.json'),video,plan,lines,old_voices)
            if any(b.start<story.start or b.end>story.end for b in plan.clips[0].beats):continue
            candidates.append((0 if previous==root else 1 if approved else 2,-path.stat().st_mtime_ns,path,plan))
        except (ValueError,KeyError,OSError):continue
    if not candidates:return None
    _,_,path,plan=min(candidates,key=lambda row:row[:2])
    write_json(root/'reference-resume.json',{'previous_plan':str(path.resolve()),'plan_digest':digest(plan.model_dump()),
        'new_voice_digest':digest(voices),'status':'requires_full_measurement_and_review'})
    return plan


def episode_evidence(video, lines, library, root, cfg, manifest_path=None, model_path=None, voice_python=None):
    from .series import scan_production_audio
    bank = read_json(library)
    validate_voice_library(bank,cfg.series)
    manifest = read_json(manifest_path) if manifest_path else None
    if manifest and manifest.get('summary',{}).get('failed'):
        raise ValueError('声纹扫描含失败记录，不能使用部分结果交付')
    if (manifest is None or manifest.get('model') != bank.get('model')
            or not any(s['source']==source_identity(video) for s in manifest['sources'])):
        folder = root/'voice-scan'/digest(['vad-production-1',source_identity(video),digest(bank),str(model_path)])
        manifest = scan_production_audio(video,library,folder,model_path,voice_python)
    voices = link_voice_evidence(video,lines,bank,manifest)
    write_json(root/'voice-evidence.json',voices)
    return bank, voices


def ensure_current_library(library, bank):
    current = read_json(library)
    if digest(current) != digest(bank):
        raise RuntimeError('声纹库在运行期间改变，当前结果不交付；重跑后自动复核')
    validate_voice_library(current,bank['series'])


def spoken_plan_matches(plan, timeline):
    fields = ('mode','start','end','text','evidence_ids')
    return len(plan.clips[0].beats)==len(timeline) and all(
        all(b.model_dump()[k]==row.get(k) for k in fields)
        for b,row in zip(plan.clips[0].beats,timeline))


def retain_approved(clips, video, lines, voices, cfg, root):
    """Legacy or stale successes remain on disk but cannot bypass the current gate."""
    from .series import check_delivery
    accepted, retired = [], []
    for clip in clips:
        try:
            record = clip['voice_review']
            if record['settings_digest'] != digest(cfg.model_dump(mode='json')):
                raise ValueError('配置已变更')
            if record['media'] != source_identity(Path(clip['video'])):
                raise ValueError('成片文件已改变')
            plan = Plan.model_validate(read_json(Path(record['plan'])))
            assert_approval(read_json(Path(record['approval'])),video,plan,lines,voices)
            if not spoken_plan_matches(plan,clip['timeline']):
                raise ValueError('已出片内容与核定稿不一致')
            accepted.append(check_delivery(clip,cfg,Path(record['plan']).parent))
        except (KeyError,ValueError,OSError) as exc:
            retired.append({'title':clip.get('title'),'video':clip.get('video'),'reason':str(exc)})
    if retired:
        write_json(root/'previous-completed'/f'{digest(clips)}.json',clips)
        write_json(root/'voice-revalidation.json',{'retired':retired,'action':'不复用旧核定，按当前声纹库重新制作；旧媒体保留'})
    return accepted
