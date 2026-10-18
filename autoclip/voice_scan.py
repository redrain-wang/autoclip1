"""Resumable, transcript-independent VAD + CAM++ scan of an entire series."""
import math
import time
import wave
from collections import Counter
from pathlib import Path

from .util import digest, duration, read_json, run, source_identity, write_json
from .voices import load_encoder, match_embedding

SCAN_VERSION = 1


def speech_windows(spans, offset, total, window=3., minimum=1.5):
    result = []
    for a, b in spans:
        start, end = max(0., offset+a/1000), min(total, offset+b/1000)
        if end <= start:
            continue
        while end-start >= minimum-1e-6:
            right = min(end, start+window)
            result.append((round(start, 3), round(right, 3)))
            start = right
    return result


def cluster_unknown(samples, seed=17):
    """Bounded-memory coarse grouping for discovery, not a character identity claim."""
    import numpy as np
    from sklearn.cluster import MiniBatchKMeans
    from .voices import normalize, similarity
    candidates = [s for s in samples if s['status']=='unknown' and s['duration'] >= 2
                  and not s['opening_or_credits'] and s['rms'] >= .003 and s['clipped_fraction'] < .02]
    if not candidates:
        return []
    vectors = np.asarray([s['embedding'] for s in candidates], dtype=np.float32)
    k = min(len(candidates), 256, max(1, round(math.sqrt(len(candidates)))))
    labels = (MiniBatchKMeans(n_clusters=k, batch_size=1024, n_init=3, random_state=seed)
              .fit_predict(vectors)) if k > 1 else [0]*len(candidates)
    groups = {}
    for i, label in enumerate(labels):
        groups.setdefault(int(label), []).append(i)
    rows = []
    for ids in groups.values():
        center = normalize(vectors[ids].mean(axis=0))
        ranked = sorted(ids, key=lambda i: -similarity(candidates[i]['embedding'], center))
        # A few representative segments, preferring different episodes and locations.
        selected = []
        for i in ranked:
            sample = candidates[i]
            if all(sample['source_id'] != candidates[j]['source_id'] for j in selected):
                selected.append(i)
            if len(selected) == 3:
                break
        if not selected:
            selected = ranked[:1]
        best_scores = sorted(candidates[i].get('score', 0.) for i in ids)
        median = best_scores[len(best_scores)//2]
        episodes = len({candidates[i]['source_id'] for i in ids})
        rows.append({'id': digest(sorted(candidates[i]['id'] for i in ids)),
                     'count': len(ids), 'episodes': episodes, 'nearest_known_median': median,
                     'cohesion': sum(similarity(candidates[i]['embedding'], center) for i in ids)/len(ids),
                     'sample_ids': [candidates[i]['id'] for i in ids],
                     'preview_ids': [candidates[i]['id'] for i in selected],
                     'priority': len(ids)*math.sqrt(episodes)*max(.05, .65-median)})
    return sorted(rows, key=lambda g: -g['priority'])


def scan_series(sources, library_path, root, model_path=None, vad_path=None, threshold=.60, margin=.10):
    import numpy as np
    from funasr import AutoModel
    import fcntl
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = (root/'scan.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise ValueError('此目录的全集扫描已经在运行') from exc
    try:
        return _scan(sources, library_path, root, model_path, vad_path, threshold, margin, np, AutoModel)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _scan(sources, library_path, root, model_path, vad_path, threshold, margin, np, AutoModel):
    library = read_json(Path(library_path))
    if not 0 < threshold < 1 or not 0 <= margin <= 2:
        raise ValueError('匹配阈值或分差无效')
    paths = [Path(p).resolve() for p in sources]
    if not paths or len(set(paths)) != len(paths):
        raise ValueError('需要不重复的原片列表')
    for source in {digest(s['source']):s['source'] for p in library['characters'] for s in p['samples']}.values():
        if source_identity(Path(source['path'])) != source:
            raise ValueError('人工参考的原片版本已改变')
    encoder, model = load_encoder(model_path)
    if model != library['model']:
        raise ValueError('扫描与人工参考的声纹模型不一致')
    if vad_path is None:
        from modelscope import snapshot_download
        vad_path = snapshot_download('iic/speech_fsmn_vad_zh-cn-16k-common-pytorch')
    vad_path = Path(vad_path).resolve()
    vad_signature = digest([(n, (vad_path/n).read_bytes().hex()) for n in ('config.yaml', 'model.pt')])
    vad = AutoModel(model=str(vad_path), device='cpu', disable_update=True, disable_log=True, disable_pbar=True)
    signature = {'version': SCAN_VERSION, 'model': model, 'vad': vad_signature,
                 'window': 3., 'minimum': 1.5, 'chunk': 120}
    identities = [source_identity(p) for p in paths]
    inputs = {**signature, 'sources': identities, 'library': digest(library), 'threshold': threshold, 'margin': margin}
    run_id = digest(inputs)
    marker = root/'inputs.json'
    if marker.exists() and read_json(marker)['run_id'] != run_id:
        raise ValueError('此目录已有不同素材或参考库的扫描；请使用新的输出目录保留结果')
    write_json(marker, {'run_id':run_id, **inputs})
    write_json(root/'library.json', library)
    started = time.monotonic()
    completed, failures, sources_out, all_samples = [], [], [], []
    def progress(stage, **extra):
        elapsed = time.monotonic()-started
        write_json(root/'scan-progress.json', {'run_id':run_id, 'stage':stage, 'total':len(paths),
                   'completed':len(completed), 'failed':len(failures), 'episodes':completed,
                   'failures':failures, 'elapsed_seconds':round(elapsed,1), **extra})
    progress('running')
    for video, source in zip(paths, identities):
        ep_started = time.monotonic()
        sid = digest(source)
        work = root/'cache'/sid
        work.mkdir(parents=True, exist_ok=True)
        episode_path = root/'episodes'/f'{video.stem}.json'
        try:
            total = duration(video)
            row = {'id':sid, 'label':video.stem, 'source':source, 'duration':total}
            sources_out.append(row)
            if episode_path.exists():
                cached = read_json(episode_path)
                if cached['source'] == source and cached['signature'] == signature and cached['library_digest']==digest(library):
                    all_samples.extend(cached['samples']);completed.append(video.stem)
                    progress('running',current=video.stem,source_seconds=total,processed_seconds=total)
                    print(f'{video.stem}：复用已完成结果', flush=True)
                    continue
            progress('decoding', current=video.stem, source_seconds=total, processed_seconds=0)
            audio_path = work/'audio.wav'
            if not audio_path.exists():
                temp = work/'audio.tmp.wav'
                run(['ffmpeg','-v','error','-y','-i',video,'-vn','-ac','1','-ar',16000,'-c:a','pcm_s16le',temp])
                temp.replace(audio_path)
            samples, speech_seconds, short_spans = [], 0., 0
            with wave.open(str(audio_path)) as stream:
                if (stream.getnchannels(),stream.getsampwidth(),stream.getframerate()) != (1,2,16000):
                    raise ValueError('音频缓存格式不匹配')
                for offset in range(0, math.ceil(total), 120):
                    cache = work/f"features-{digest(signature)}-{offset:05}.json"
                    if cache.exists():
                        chunk_result = read_json(cache)
                    else:
                        stream.setpos(offset*16000)
                        audio = np.frombuffer(stream.readframes(120*16000),dtype='<i2').astype(np.float32)/32768
                        detected = vad.generate(input=audio,fs=16000,disable_pbar=True)[0]['value']
                        local_total = min(total,offset+len(audio)/16000)
                        windows = speech_windows(detected,offset,local_total)
                        features = []
                        for start,end in windows:
                            sound = audio[round((start-offset)*16000):round((end-offset)*16000)]
                            rms = float(np.sqrt(np.mean(sound**2)))
                            if rms < .001:
                                continue
                            embedding = encoder(sound)
                            features.append({'id':digest({'source':source,'model':model,'start':start,'end':end}),
                                'source_id':sid,'start':start,'end':end,'duration':round(end-start,3),
                                'text':'原声候选（本次仅分析声音，未逐句转写）','embedding':embedding,'rms':round(rms,6),
                                'clipped_fraction':float(np.mean(np.abs(sound)>=.99)),
                                'opening_or_credits':start<154 or end>total-40})
                        chunk_result={'samples':features,'speech_seconds':sum((b-a)/1000 for a,b in detected),
                                      'short_spans':sum(b-a<1500 for a,b in detected)}
                        write_json(cache,chunk_result)
                    speech_seconds += chunk_result['speech_seconds'];short_spans += chunk_result['short_spans']
                    for sample in chunk_result['samples']:
                        samples.append({**sample, **match_embedding(sample['embedding'],library,threshold,margin)})
                    progress('running',current=video.stem,source_seconds=total,processed_seconds=min(total,offset+120),
                             current_samples=len(samples))
            if source_identity(video) != source:
                raise ValueError('原片在扫描期间发生变化')
            stats={'count':len(samples),'speech_seconds':round(speech_seconds,2),
                   'embedded_seconds':round(sum(s['duration'] for s in samples),2),'short_spans':short_spans,
                   'statuses':dict(Counter(s['status'] for s in samples)),
                   'elapsed_seconds':round(time.monotonic()-ep_started,2)}
            write_json(episode_path,{'source':source,'source_row':row,'signature':signature,
                                    'library_digest':digest(library),'stats':stats,'samples':samples})
            all_samples.extend(samples);completed.append(video.stem)
            progress('running',current=video.stem,source_seconds=total,processed_seconds=total)
            print(f"{video.stem} 完成 {len(samples)} 段，{stats['elapsed_seconds']} 秒；{len(completed)}/{len(paths)} 集",flush=True)
        except Exception as exc:
            failures.append({'episode':video.stem,'error':str(exc)[:600]})
            progress('running',current=video.stem)
            print(f'{video.stem} 失败：{exc}',flush=True)
    progress('clustering', samples=len(all_samples))
    groups = cluster_unknown(all_samples)
    summary={'run_id':run_id,'series':library['series'],'completed':len(completed),'total':len(paths),
             'failed':failures,'sample_count':len(all_samples),'statuses':dict(Counter(s['status'] for s in all_samples)),
             'candidate_characters':dict(Counter(s['name'] for s in all_samples if s['status']=='suggested')),
             'unknown_groups':len(groups),'elapsed_seconds':round(time.monotonic()-started,1),
             'scope':'全片语音检测；声纹窗口1.5–3秒；过短、静音片段不建立声纹。片头片尾仍扫描，但不优先用于人工补录。',
             'interpretation':'机器匹配均为候选，未自动补入人工标准库；匿名分组不等于人物。'}
    write_json(root/'scan-summary.json',summary)
    write_json(root/'discovery-groups.json',groups)
    write_json(root/'manifest.json',{'version':SCAN_VERSION,'dataset_id':run_id,'series':library['series'],
               'model':model,'sources':sources_out,'samples':all_samples,'groups':[], 'summary':summary})
    progress('complete' if not failures else 'partial_failure',summary=summary)
    print('全集扫描结果：'+str(summary),flush=True)
    return summary


def import_discovery(scan_root, review_root, limit=12):
    """Add a small anonymous listening queue, preserving every human decision."""
    from .voice_review import ReviewStore
    scan_root, review_root = Path(scan_root), Path(review_root)
    scan = read_json(scan_root/'manifest.json')
    if scan['summary']['failed'] or scan['summary']['completed'] != scan['summary']['total']:
        raise ValueError('请先完成全部集数扫描，再导入优先样本')
    groups = read_json(scan_root/'discovery-groups.json')
    store = ReviewStore(review_root)
    manifest, review = store.manifest, store.review()
    if manifest.get('discovery_scan_id') == scan['dataset_id']:
        return manifest
    if manifest['series'] != scan['series'] or manifest['model'] != scan['model']:
        raise ValueError('全集扫描与补录页的剧名或模型不一致')
    if not 1 <= limit <= 30:
        raise ValueError('每轮优先样本应在 1–30 组之间')
    sample_map = {s['id']:s for s in scan['samples']}
    source_map = {s['id']:s for s in scan['sources']}
    existing_samples = {s['id']:s for s in manifest['samples']}
    existing_sources = {s['id']:s for s in manifest['sources']}
    library = store.combined_library(review)
    references = {}
    for person in library['characters']:
        for sample in person['samples']:
            references.setdefault(sample['source_id'], []).append(sample)
    selected_groups, added_samples = [], []
    for group in groups:
        if group['count'] < 4 or group['cohesion'] < .5:
            continue
        previews = []
        for sid in group['preview_ids']:
            sample = sample_map[sid]
            if sid in existing_samples or any(
                sample['start'] < ref['end']+1 and sample['end'] > ref['start']-1
                for ref in references.get(sample['source_id'], [])):
                continue
            previews.append(sid)
        if not previews:
            continue
        gid = f"scan-{group['id']}"
        selected_groups.append({'id':gid,'count':group['count'],'episodes':group['episodes'],
                                'preview_ids':previews,'cohesion':group['cohesion']})
        for sid in previews:
            sample = sample_map[sid]
            added_samples.append({**sample,'group_id':gid,'target_id':None,
                                  'candidate_basis':'来自全集未命名声音的聚类代表，身份尚未确认。'})
            source = source_map[sample['source_id']]
            if source_identity(Path(source['source']['path'])) != source['source']:
                raise ValueError('优先样本的原片版本已改变')
            existing_sources[source['id']] = source
        if len(selected_groups) >= limit:
            break
    if not selected_groups:
        raise ValueError('未找到适合当前轮次的新声音样本')
    old_id = manifest['dataset_id']
    manifest = {**manifest,'samples':manifest['samples']+added_samples,'sources':list(existing_sources.values()),
                'discovery_groups':selected_groups,'discovery_scan_id':scan['dataset_id']}
    manifest['dataset_id'] = digest([old_id,scan['dataset_id'],[s['id'] for s in added_samples]])
    updated_review = {**review,'dataset_id':manifest['dataset_id'],'revision':review['revision']+1,
                      'enrollment':{'status':'active'}}
    write_json(review_root/'history'/f'manifest-before-discovery-{old_id}.json',store.manifest)
    write_json(review_root/'history'/f'review-before-discovery-{review["revision"]:06}.json',review)
    write_json(review_root/'manifest.json',manifest)
    write_json(review_root/'review.json',updated_review)
    ReviewStore(review_root)
    return manifest
