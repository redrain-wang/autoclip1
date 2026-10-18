"""Shared voice enrollment, evidence and approval gates for short films."""
from pathlib import Path

from .automatic import assert_approval, link_voice_evidence, validate_voice_library
from .models import Plan
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
