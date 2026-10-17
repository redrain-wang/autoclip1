"""Prepare a finite, named voice-enrollment checklist from source-bound candidate clips."""
from pathlib import Path

from .util import digest, duration, read_json, run, source_identity, write_json
from .voices import VERSION, load_encoder


def prepare_coverage(plan_path, library_path, root, model_path=None):
    import numpy as np
    import wave
    plan, library = read_json(Path(plan_path)), read_json(Path(library_path))
    root = Path(root).resolve()
    if plan['series'] != library['series']:
        raise ValueError('补录名单与声纹库剧名不一致')
    targets = plan['targets']
    ids, names = set(), set()
    sources, samples, groups = {}, [], []
    encoder, model = load_encoder(model_path)
    if model != library['model']:
        raise ValueError('补录与已有声纹模型版本不一致')
    for target in targets:
        if not target.get('id') or target['id'] in ids or not target.get('name'):
            raise ValueError('补录人物必须有唯一编号和姓名')
        ids.add(target['id'])
        for alias in {target['name'], *target.get('aliases', [])}:
            if alias in names:
                raise ValueError('补录人物姓名或别名存在冲突')
            names.add(alias)
        sample_ids = []
        for candidate in target.get('candidates', []):
            video, transcript = Path(candidate['video']).resolve(), Path(candidate['transcript']).resolve()
            source = source_identity(video)
            if read_json(transcript.parent/'source.json') != source:
                raise ValueError('补录字幕与原片版本不匹配')
            start, end = float(candidate['start']), float(candidate['end'])
            total = duration(video)
            if not 0 <= start < end <= total or not 1.2 <= end-start <= 12:
                raise ValueError('补录片段须在原片内且长 1.2–12 秒')
            evidence = [r for r in read_json(transcript) if r['start'] < end and r['end'] > start]
            if not evidence or not candidate.get('basis'):
                raise ValueError('候选片段需有字幕定位与选择依据')
            source_id = digest(source)
            sources[source_id] = {'id': source_id, 'label': video.stem, 'source': source, 'duration': total}
            sample_id = digest({'source': source, 'start': start, 'end': end, 'model': model})
            if sample_id in {s['id'] for s in samples}:
                raise ValueError('不能把同一候选声音同时分配给两个目标人物')
            work = root/'cache'
            work.mkdir(parents=True, exist_ok=True)
            clip = work/f'{sample_id}.wav'
            if not clip.exists():
                run(['ffmpeg', '-v', 'error', '-y', '-ss', start, '-i', video, '-t', end-start,
                     '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', clip])
            cache = work/f'{sample_id}.json'
            if cache.exists():
                embedding = read_json(cache)['embedding']
            else:
                with wave.open(str(clip), 'rb') as stream:
                    audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32)/32768
                embedding = encoder(audio)
                write_json(cache, {'embedding': embedding})
            samples.append({'id': sample_id, 'source_id': source_id, 'start': start, 'end': end,
                            'text': ' '.join(r['text'] for r in evidence), 'embedding': embedding,
                            'group_id': target['id'], 'target_id': target['id'],
                            'candidate_basis': candidate['basis']})
            sample_ids.append(sample_id)
        if sample_ids:
            groups.append({'id': target['id'], 'sample_ids': sample_ids, 'preview_ids': sample_ids[:3]})
    settings = {'version': VERSION, 'series': plan['series'], 'model': model,
                'coverage_scope': plan['scope'], 'coverage_targets': targets,
                'baseline_library_digest': digest(library)}
    manifest = {**settings, 'dataset_id': digest([settings, list(sources.values())]),
                'sources': list(sources.values()), 'samples': samples, 'groups': groups}
    path = root/'manifest.json'
    if path.exists() and read_json(path)['dataset_id'] != manifest['dataset_id']:
        raise ValueError('补录目录已有其他名单或参考版本，请使用新目录保留确认记录')
    write_json(root/'baseline-library.json', library)
    write_json(path, manifest)
    print(f'补录名单 {len(targets)} 位，准备 {len(samples)} 段候选。已有确认需合并计算覆盖进度。', flush=True)
    return manifest
