"""Human-reviewed, source-bound voice enrollment. Clusters never imply identity."""
import hashlib
import math
from pathlib import Path

from .util import digest, duration, read_json, run, source_identity, write_json

VERSION = 1
MODEL = 'iic/speech_campplus_sv_zh-cn_16k-common'


def normalize(vector):
    values = [float(x) for x in vector]
    length = math.sqrt(sum(x*x for x in values))
    if not values or not all(math.isfinite(x) for x in values) or length < 1e-8:
        raise ValueError('声纹特征无效')
    return [x/length for x in values]


def similarity(a, b):
    if len(a) != len(b):
        raise ValueError('声纹模型维度不一致')
    return sum(x*y for x, y in zip(a, b))


def fingerprint(model_path):
    weight = model_path/'campplus_cn_common.bin'
    if not weight.is_file():
        raise ValueError(f'缺少 CAM++ 模型权重：{weight}')
    return {'model': MODEL, 'sha256': hashlib.sha256(weight.read_bytes()).hexdigest()}


def load_encoder(model_path=None):
    try:
        import torch
        from funasr import AutoModel
    except ImportError as exc:
        raise RuntimeError('请安装 voices 可选依赖；见 docs/人工声纹确认.md') from exc
    if model_path is None:
        from modelscope import snapshot_download
        model_path = Path(snapshot_download(MODEL))
    model_path = Path(model_path).resolve()
    torch.set_num_threads(4)
    model = AutoModel(model=str(model_path), device='cpu', disable_update=True,
                      disable_pbar=True, disable_log=True)
    def encode(waveform):
        with torch.inference_mode():
            output = model.generate(input=waveform, fs=16000, disable_pbar=True)
        return normalize(output[0]['spk_embedding'].detach().cpu().reshape(-1).tolist())
    return encode, fingerprint(model_path)


def candidate_spans(lines, total):
    """Do not join adjacent subtitle lines: they can belong to different speakers."""
    spans = []
    for line in lines:
        start, end = float(line.start), float(line.end)
        if not all(math.isfinite(x) for x in (start, end)) or not 0 <= start < end <= total+.1:
            continue
        # Short replies and very long ASR spans are poor enrollment material.
        if not 2.2 <= end-start <= 12 or len(line.text.strip()) < 3:
            continue
        # Preserve exact source offset; do not add adjacent speakers as padding.
        end = min(end, start+8, total)
        spans.append({'start': round(start, 3), 'end': round(end, 3),
                      'text': line.text, 'line_id': line.id})
    return spans


def group_samples(samples, threshold=.5):
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering
    if not 0 < threshold < 1:
        raise ValueError('聚类阈值必须在 0 与 1 之间')
    if not samples:
        raise ValueError('没有可试听的语音片段')
    vectors = np.asarray([s['embedding'] for s in samples])
    if len(samples) == 1:
        labels = [0]
    else:
        labels = AgglomerativeClustering(n_clusters=None, metric='cosine', linkage='average',
                                        distance_threshold=1-threshold).fit_predict(vectors)
    groups = {}
    for index, label in enumerate(labels):
        groups.setdefault(int(label), []).append(index)
    ordered = sorted(groups.values(), key=lambda ids: (-len(ids), ids[0]))
    result = []
    for number, ids in enumerate(ordered, 1):
        center = normalize(vectors[ids].mean(axis=0))
        ranked = sorted(ids, key=lambda i: -similarity(samples[i]['embedding'], center))
        # Preview representative clips from different times; all members remain accessible.
        previews = []
        for i in ranked:
            if all(samples[i]['source_id'] != samples[j]['source_id'] or
                   abs(samples[i]['start']-samples[j]['start']) >= 25 for j in previews):
                previews.append(i)
            if len(previews) == 4:
                break
        gid = f'voice-{number:03}'
        for i in ids:
            samples[i]['group_id'] = gid
        result.append({'id': gid, 'sample_ids': [samples[i]['id'] for i in ranked],
                       'preview_ids': [samples[i]['id'] for i in previews]})
    return result


def prepare(sources, transcripts, root, series, cfg, model_path=None, threshold=.5, skip_start=0., skip_end=0.):
    import numpy as np
    import wave
    from .ingest import transcribe
    from .models import Line
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not series.strip():
        raise ValueError('请填写剧名')
    if not 0 < threshold < 1:
        raise ValueError('聚类阈值必须在 0 与 1 之间')
    if not all(math.isfinite(x) and x >= 0 for x in (skip_start, skip_end)):
        raise ValueError('片头片尾排除秒数必须非负且有限')
    if transcripts and len(transcripts) != len(sources):
        raise ValueError('字幕路径数量必须与原片数量一致，并按同一顺序提供')
    if len({str(Path(p).resolve()) for p in sources}) != len(sources):
        raise ValueError('原片不能重复')
    encoder, model_identity = load_encoder(model_path)
    settings = {'version': VERSION, 'series': series, 'model': model_identity,
                'threshold': threshold, 'skip_start': skip_start, 'skip_end': skip_end,
                'sources': [source_identity(Path(p)) for p in sources]}
    source_rows, samples, line_sets = [], [], []
    for i, path in enumerate(sources):
        video = Path(path).resolve()
        source = settings['sources'][i]
        sid = digest(source)
        work = root/'cache'/sid
        work.mkdir(parents=True, exist_ok=True)
        if transcripts:
            transcript = Path(transcripts[i])
            marker = transcript.parent/'source.json'
            if not marker.exists() or read_json(marker) != source:
                raise ValueError(f'字幕与原片版本不匹配：{transcript}')
            lines = [Line.model_validate(x) for x in read_json(transcript)]
        else:
            lines = transcribe(video, work, cfg)
        line_sets.append([x.model_dump() for x in lines])
        total = duration(video)
        source_rows.append({'id': sid, 'label': video.stem, 'source': source, 'duration': total})
        wav = work/'audio.wav'
        if not wav.exists():
            run(['ffmpeg', '-v', 'error', '-y', '-i', video, '-vn', '-ac', '1',
                 '-ar', '16000', '-c:a', 'pcm_s16le', wav])
        with wave.open(str(wav), 'rb') as stream:
            audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32)/32768
        spans = [s for s in candidate_spans(lines, total)
                 if s['start'] >= skip_start and s['end'] <= total-skip_end]
        print(f'{video.name}：提取 {len(spans)} 个候选声音片段', flush=True)
        for j, span in enumerate(spans):
            sample_id = digest({'source': source, 'span': span, 'model': model_identity, 'version': VERSION})
            cache = work/f'{sample_id}.json'
            if cache.exists():
                embedding = normalize(read_json(cache)['embedding'])
            else:
                chunk = audio[round(span['start']*16000):round(span['end']*16000)]
                if len(chunk) < 16000 or float(np.sqrt(np.mean(chunk**2))) < .001:
                    continue
                embedding = encoder(chunk)
                write_json(cache, {'embedding': embedding})
            samples.append({'id': sample_id, 'source_id': sid, **span, 'embedding': embedding})
            if (j+1) % 100 == 0:
                print(f'{video.name}：{j+1}/{len(spans)}', flush=True)
    groups = group_samples(samples, threshold)
    dataset_id = digest({**settings, 'lines': line_sets})
    target = root/'manifest.json'
    if target.exists() and read_json(target)['dataset_id'] != dataset_id:
        raise ValueError('此目录已有其他批次；请使用新的输出目录以保留人工确认')
    manifest = {**settings, 'dataset_id': dataset_id, 'sources': source_rows, 'samples': samples, 'groups': groups}
    write_json(target, manifest)
    print(f'候选分为 {len(groups)} 组，均待人工确认。资料保存在 {root}', flush=True)
    return manifest


def empty_review(manifest):
    return {'dataset_id': manifest['dataset_id'], 'revision': 0, 'decisions': {}}


def validate_review(manifest, review, changes):
    if review['dataset_id'] != manifest['dataset_id']:
        raise ValueError('确认记录不属于当前批次')
    ids = {s['id'] for s in manifest['samples']}
    if not isinstance(changes, dict) or not changes or len(changes) > 100:
        raise ValueError('每次需提交 1–100 个片段')
    decisions = dict(review['decisions'])
    for sid, decision in changes.items():
        if sid not in ids or not isinstance(decision, dict):
            raise ValueError('未知的声音片段')
        status, name = decision.get('status'), decision.get('name', '')
        if status not in {'named', 'unnamed', 'excluded', 'pending'} or not isinstance(name, str):
            raise ValueError('确认状态无效')
        name = name.strip()
        if len(name) > 60 or (status == 'named' and not name):
            raise ValueError('请填写 1–60 字角色名')
        if status == 'named':
            canonical = {target['name'] for target in manifest.get('coverage_targets', [])
                         if name == target['name'] or name in target.get('aliases', [])}
            if len(canonical) > 1:
                raise ValueError('这个称呼对应多个目标人物，请填写具体角色名')
            if canonical:
                name = canonical.pop()
        decisions[sid] = {'status': status, 'name': name if status == 'named' else ''}
    return {**review, 'revision': review['revision']+1, 'decisions': decisions,
            'enrollment': {'status': 'active'}}


def registry(manifest, review):
    """Only individually reviewed named samples enter the reusable library."""
    characters = {}
    source_map = {s['id']: s['source'] for s in manifest['sources']}
    for sample in manifest['samples']:
        decision = review['decisions'].get(sample['id'], {})
        if decision.get('status') != 'named':
            continue
        name = decision['name']
        character = characters.setdefault(name, {'id': digest([manifest['series'], name]), 'name': name, 'samples': []})
        character['samples'].append({**sample, 'source': source_map[sample['source_id']], 'status': 'human_confirmed'})
    return {'version': VERSION, 'series': manifest['series'], 'model': manifest['model'],
            'dataset_id': manifest['dataset_id'], 'revision': review['revision'],
            'characters': list(characters.values())}


def match_embedding(embedding, library, threshold=.65, margin=.10):
    """Return a suggestion, never a human-confirmed identity or calibrated probability."""
    scores = []
    for person in library['characters']:
        values = sorted((similarity(embedding, s['embedding']) for s in person['samples']), reverse=True)
        if values:
            scores.append((sum(values[:3])/len(values[:3]), person['name']))
    scores.sort(reverse=True)
    if not scores:
        return {'status': 'unknown', 'name': None, 'candidates': []}
    gap = scores[0][0]-scores[1][0] if len(scores) > 1 else None
    accepted = scores[0][0] >= threshold and (gap is None or gap >= margin)
    return {'status': 'suggested' if accepted else 'unknown', 'name': scores[0][1] if accepted else None,
            'score': scores[0][0], 'margin': gap,
            'candidates': [{'name': name, 'score': score} for score, name in scores[:3]]}


def enrollment_progress(manifest, review, baseline=None):
    """Small optional discovery queue; inferred matches never become reference samples."""
    bank = registry(manifest, review)
    if baseline is not None:
        bank = merge_libraries([baseline, bank])
    decisions = review['decisions']
    characters = [{'name': person['name'], 'count': len(person['samples']),
                   'target': 3, 'ready': len(person['samples']) >= 3,
                   'sample_ids': [s['id'] for s in person['samples']]}
                  for person in bank['characters']]
    matches = {}
    for sample in manifest['samples']:
        if decisions.get(sample['id'], {}).get('status', 'pending') == 'pending':
            matches[sample['id']] = match_embedding(sample['embedding'], bank)
    queue = []
    for group in manifest['groups']:
        # Already visited groups stay available for auditing, not repeated enrollment chores.
        visited = any(decisions.get(sid, {}).get('status') in {'named', 'unnamed'} for sid in group['sample_ids'])
        unknown = [sid for sid in group['sample_ids'] if sid in matches and matches[sid]['status'] == 'unknown']
        if unknown and not visited:
            queue.append({'group_id': group['id'], 'sample_ids': unknown})
    queue.sort(key=lambda row: -len(row['sample_ids']))
    coverage = []
    for target in manifest.get('coverage_targets', []):
        aliases = {target['name'], *target.get('aliases', [])}
        matching = [p for p in characters if p['name'] in aliases]
        count = sum(p['count'] for p in matching)
        pending = [s['id'] for s in manifest['samples'] if s.get('target_id') == target['id']
                   and decisions.get(s['id'], {}).get('status', 'pending') == 'pending']
        coverage.append({'id': target['id'], 'name': target['name'], 'count': count,
                         'status': 'confirmed' if count else 'candidate' if pending else 'not_located',
                         'sample_ids': pending})
    complete = bool(coverage) and all(p['status'] == 'confirmed' for p in coverage)
    voice_discovery = []
    discovery_total = len(manifest.get('discovery_groups', []))
    for group in manifest.get('discovery_groups', []):
        visited = any(decisions.get(sid, {}).get('status') in {'named', 'unnamed'} for sid in group['preview_ids'])
        pending = [sid for sid in group['preview_ids'] if sid in matches and matches[sid]['status'] == 'unknown']
        if pending and not visited:
            voice_discovery.append({**group, 'preview_ids': pending})
    return {'characters': characters, 'coverage': coverage, 'coverage_complete': complete,
            'voice_discovery': voice_discovery, 'discovery_total': discovery_total,
            'ready_count': sum(p['ready'] for p in characters),
            'can_finish': bool(characters), 'all_ready': bool(characters) and all(p['ready'] for p in characters),
            'suggested_count': sum(m['status'] == 'suggested' for m in matches.values()),
            'unknown_count': sum(m['status'] == 'unknown' for m in matches.values()),
            'discovery': queue[:5], 'matches': matches,
            'finished': review.get('enrollment', {}).get('status') == 'finished'}


def evaluate_library(library, thresholds=(.65, .60), margin=.10):
    """Diagnostics on human references, never an estimate of unseen-episode accuracy."""
    if not thresholds or any(not 0 < t < 1 for t in thresholds) or not 0 <= margin <= 2:
        raise ValueError('验证阈值或分差无效')
    experiments = []
    for threshold in thresholds:
        modes = {}
        for mode in ('sample_holdout', 'episode_holdout', 'speaker_holdout'):
            trials = []
            skipped = 0
            for person in library['characters']:
                for sample in person['samples']:
                    heldout = []
                    for candidate in library['characters']:
                        refs = []
                        for ref in candidate['samples']:
                            if mode == 'sample_holdout':
                                # Exclude the test clip and overlapping/adjacent extracts from the same audio.
                                keep = ref['id'] != sample['id'] and not (
                                    ref['source_id'] == sample['source_id'] and
                                    ref['start'] < sample['end']+8 and ref['end'] > sample['start']-8)
                            elif mode == 'episode_holdout':
                                keep = ref['source_id'] != sample['source_id']
                            else:
                                keep = candidate['name'] != person['name']
                            if keep:
                                refs.append(ref)
                        if refs:
                            heldout.append({**candidate, 'samples': refs})
                    if mode != 'speaker_holdout' and not any(p['name'] == person['name'] for p in heldout):
                        skipped += 1
                        continue
                    predicted = match_embedding(sample['embedding'], {'characters': heldout}, threshold, margin)
                    if mode == 'speaker_holdout':
                        outcome = 'correct_rejection' if predicted['name'] is None else 'false_acceptance'
                    else:
                        outcome = ('unknown' if predicted['name'] is None else
                                   'correct' if predicted['name'] == person['name'] else 'wrong')
                    trials.append({'id': sample['id'], 'expected_name': person['name'], 'outcome': outcome,
                                   'reference_ids': [r['id'] for p in heldout for r in p['samples']],
                                   **predicted})
            from collections import Counter
            modes[mode] = {'tested': len(trials), 'skipped': skipped,
                           'characters': sorted({r['expected_name'] for r in trials}),
                           'counts': dict(Counter(r['outcome'] for r in trials)), 'trials': trials}
        experiments.append({'threshold': threshold, 'margin': margin, 'modes': modes})
    return {'series': library['series'], 'model': library['model'],
            'library_dataset_id': library['dataset_id'], 'library_revision': library['revision'],
            'experiments': experiments,
            'limitations': ['样本来自人工选择的参考片段，数量较少且存在选择偏差。',
                            '参数对比属于开发诊断，不能视为独立测试集的准确率。',
                            '整个人留出只能模拟陌生声音，不能代表真实路人及配乐分布。',
                            '第三集需要独立核对；候选数量不等于正确数量。']}


def merge_libraries(libraries):
    """Combine independently reviewed episodes without copying inferred identities."""
    if not libraries:
        raise ValueError('至少需要一个人工声纹库')
    first = libraries[0]
    parents, characters, seen = {}, {}, {}
    for bank in libraries:
        if bank['series'] != first['series'] or bank['model'] != first['model']:
            raise ValueError('剧名或声纹模型版本不匹配')
        dataset_id = bank['dataset_id']
        if dataset_id in parents and parents[dataset_id] != bank['revision']:
            raise ValueError('同一批次有不同版本，请只保留最新人工确认')
        parents[dataset_id] = bank['revision']
        for person in bank['characters']:
            for sample in person['samples']:
                if sample.get('status') != 'human_confirmed':
                    raise ValueError('只有人工确认样本可以合并入库')
                signature = {'name': person['name'], **{k: sample[k] for k in
                             ('source', 'source_id', 'start', 'end', 'embedding')}}
                if sample['id'] in seen:
                    if seen[sample['id']] != signature:
                        raise ValueError('同一语音样本存在冲突的角色或来源')
                    continue
                seen[sample['id']] = signature
                target = characters.setdefault(person['name'],
                         {'id': digest([first['series'], person['name']]), 'name': person['name'], 'samples': []})
                target['samples'].append(dict(sample))
    provenance = [{'dataset_id': key, 'revision': value} for key, value in sorted(parents.items())]
    return {'version': VERSION, 'series': first['series'], 'model': first['model'],
            'dataset_id': digest([first['series'], sorted(parents)]), 'revision': digest(provenance),
            'parent_libraries': provenance, 'characters': [characters[n] for n in sorted(characters)]}


def match_batch(library, batch, threshold=.65, margin=.10):
    if library['series'] != batch['series'] or library['model'] != batch['model']:
        raise ValueError('剧名或声纹模型版本不匹配')
    if not 0 < threshold < 1 or not 0 <= margin <= 2:
        raise ValueError('匹配阈值或候选分差无效')
    reviewed = {s['id']: (person['name'], s) for person in library['characters']
                for s in person['samples'] if s.get('status') == 'human_confirmed'}
    rows = []
    for sample in batch['samples']:
        if sample['id'] in reviewed:
            name, ref = reviewed[sample['id']]
            if any(ref[k] != sample[k] for k in ('source_id', 'start', 'end', 'embedding')):
                raise ValueError('已确认片段与待比对素材不一致')
            result = {'status': 'human_confirmed', 'name': name, 'candidates': []}
        else:
            result = match_embedding(sample['embedding'], library, threshold, margin)
        rows.append({k: sample[k] for k in ('id', 'source_id', 'start', 'end')} | result)
    return {'dataset_id': batch['dataset_id'], 'library_dataset_id': library['dataset_id'],
            'library_revision': library['revision'], 'threshold': threshold, 'margin': margin, 'samples': rows}
