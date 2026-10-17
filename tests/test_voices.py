import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from autoclip.models import Line
from autoclip.util import read_json, run, source_identity, write_json
from autoclip.voice_review import ReviewStore, make_handler
from autoclip.voices import candidate_spans, empty_review, group_samples, match_embedding, normalize, registry, validate_review


def data(tmp_path):
    video = tmp_path/'source.mp4'
    video.write_bytes(b'original source')
    samples = [{'id': str(i)*16, 'source_id': 'source1', 'start': i*3., 'end': i*3.+2.5,
                'text': '测试台词', 'embedding': vector, 'group_id': 'voice-001'}
               for i, vector in [(1, [1., 0.]), (2, [0., 1.]), (3, [1., 0.])]]
    return {'dataset_id': 'test-dataset', 'series': '测试剧', 'model': {'sha256': 'abc'},
            'sources': [{'id': 'source1', 'label': '01', 'source': source_identity(video), 'duration': 20.}],
            'samples': samples, 'groups': [{'id': 'voice-001', 'sample_ids': [s['id'] for s in samples],
                                           'preview_ids': [s['id'] for s in samples]}]}


def test_only_reviewed_samples_enter_bank_and_same_name_merges(tmp_path):
    m = data(tmp_path)
    review = validate_review(m, empty_review(m), {'1'*16: {'status': 'named', 'name': '甲'}})
    bank = registry(m, review)
    assert len(bank['characters']) == 1
    assert [s['id'] for s in bank['characters'][0]['samples']] == ['1'*16]
    review = validate_review(m, review, {'2'*16: {'status': 'unnamed'}, '3'*16: {'status': 'named', 'name': '甲'}})
    assert len(registry(m, review)['characters'][0]['samples']) == 2
    review = validate_review(m, review, {'1'*16: {'status': 'pending'}, '3'*16: {'status': 'excluded'}})
    assert not registry(m, review)['characters']


def test_group_does_not_assign_names_and_preserves_all_samples(tmp_path):
    pytest.importorskip('sklearn')
    samples = data(tmp_path)['samples']
    groups = group_samples(samples)
    assert sorted(len(g['sample_ids']) for g in groups) == [1, 2]
    assert not any('name' in s for s in samples)
    assert len(group_samples(samples[:1])) == 1


def test_match_rejects_ambiguous_and_out_of_library(tmp_path):
    m = data(tmp_path)
    r = validate_review(m, empty_review(m), {'1'*16: {'status': 'named', 'name': '甲'},
                                           '2'*16: {'status': 'named', 'name': '乙'}})
    bank = registry(m, r)
    assert match_embedding([1, 0], bank)['name'] == '甲'
    assert match_embedding(normalize([1, 1]), bank)['status'] == 'unknown'
    assert match_embedding([-1, 0], bank)['name'] is None
    assert match_embedding([1, 0], {'characters': []})['status'] == 'unknown'


def test_bad_review_and_embedding_rejected(tmp_path):
    m = data(tmp_path)
    for change in [{'missing': {'status': 'named', 'name': '甲'}}, {'1'*16: {'status': 'named', 'name': ''}},
                   {'1'*16: {'status': 'automatic', 'name': '甲'}}]:
        with pytest.raises(ValueError):
            validate_review(m, empty_review(m), change)
    for vector in [[0, 0], [float('nan'), 1], [float('inf'), 1]]:
        with pytest.raises(ValueError):
            normalize(vector)


def test_subtitle_boundaries_never_join_speakers():
    lines = [Line(id=0, start=0, end=3, text='这是甲说的'), Line(id=1, start=3, end=6, text='这是乙说的'),
             Line(id=2, start=6, end=6.4, text='是'), Line(id=3, start=8, end=25, text='过长混杂字幕')]
    spans = candidate_spans(lines, 30)
    assert [(s['start'], s['end']) for s in spans] == [(0, 3), (3, 6)]


def test_store_revisions_persistence_undo_and_source_change(tmp_path):
    m = data(tmp_path)
    write_json(tmp_path/'manifest.json', m)
    store = ReviewStore(tmp_path)
    payload = {'dataset_id': m['dataset_id'], 'revision': 0, 'changes': {'1'*16: {'status': 'named', 'name': '甲'}}}
    store.save(payload)
    with pytest.raises(FileExistsError):
        store.save(payload)
    assert ReviewStore(tmp_path).review()['decisions']['1'*16]['name'] == '甲'
    assert read_json(tmp_path/'registry.json')['characters'][0]['name'] == '甲'
    assert read_json(tmp_path/'history'/'review-000000.json')['revision'] == 0
    store.save({**payload, 'revision': 1, 'changes': {'1'*16: {'status': 'pending'}}})
    assert not read_json(tmp_path/'registry.json')['characters']
    (tmp_path/'source.mp4').write_bytes(b'changed')
    with pytest.raises(ValueError, match='原片版本'):
        ReviewStore(tmp_path)


def test_http_playback_range_and_save(tmp_path):
    m = data(tmp_path)
    video = tmp_path/'source.mp4'
    run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=black:s=160x90:d=12',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=12', '-c:v', 'libx264', '-c:a', 'aac', video])
    m['sources'][0]['source'] = source_identity(video)
    m['sources'][0]['duration'] = 12
    write_json(tmp_path/'manifest.json', m)
    store = ReviewStore(tmp_path)
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
            assert '声音确认台' in client.get('/').text
            state = client.get('/api/state').json()
            assert 'embedding' not in state['manifest']['samples'][0]
            assert client.get('/', headers={'Host': 'malicious.invalid'}).status_code == 403
            assert client.post('/api/review', json={}).status_code == 403
            audio = client.get('/audio/'+'1'*16, headers={'Range': 'bytes=0-99'})
            assert audio.status_code == 206 and len(audio.content) == 100 and audio.content.startswith(b'RIFF')
            assert client.get('/audio/'+'1'*16, headers={'Range': 'bytes=90-10'}).status_code == 416
            assert client.get('/context/'+'1'*16).headers['content-type'] == 'video/mp4'
            payload = {'dataset_id': m['dataset_id'], 'revision': 0, 'changes': {'1'*16: {'status': 'named', 'name': '甲'}}}
            response = client.post('/api/review', headers={'X-Review-Token': store.token}, json=payload)
            assert response.status_code == 200
            assert client.post('/api/review', headers={'X-Review-Token': store.token}, json=payload).status_code == 409
            assert client.get('/api/registry').json()['characters'][0]['name'] == '甲'
            assert client.get('/api/matches').status_code == 409
            finish = client.post('/api/finish', headers={'X-Review-Token': store.token},
                                 json={'dataset_id': m['dataset_id'], 'revision': 1})
            assert finish.status_code == 200 and finish.json()['progress']['finished']
            assert client.get('/api/matches').status_code == 200
            edited = client.post('/api/review', headers={'X-Review-Token': store.token},
                                 json={**payload, 'revision': 2})
            assert edited.status_code == 200
            assert client.get('/api/matches').status_code == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_progress_is_about_people_not_all_groups(tmp_path):
    from autoclip.voices import enrollment_progress
    m = data(tmp_path)
    # Three human-confirmed references suffice for the initial count recommendation.
    r = validate_review(m, empty_review(m), {s['id']: {'status': 'named', 'name': '甲'} for s in m['samples']})
    original = dict(r['decisions'])
    for i in range(4, 14):
        sid = f'{i:016x}'
        m['samples'].append({'id': sid, 'source_id': 'source1', 'start': i*3., 'end': i*3.+2.5,
                             'embedding': [-1., 0.], 'group_id': f'voice-{i:03}'})
        m['groups'].append({'id': f'voice-{i:03}', 'sample_ids': [sid], 'preview_ids': [sid]})
    progress = enrollment_progress(m, r)
    assert progress['all_ready'] and progress['can_finish'] and progress['ready_count'] == 1
    assert len(progress['discovery']) == 5
    assert progress['unknown_count'] == 10
    assert r['decisions'] == original
    assert len(registry(m, r)['characters'][0]['samples']) == 3


def test_known_matches_and_visited_groups_not_recommended(tmp_path):
    from autoclip.voices import enrollment_progress
    m = data(tmp_path)
    m['groups'] = [{'id': f'voice-{i}', 'sample_ids': [s['id']], 'preview_ids': [s['id']]}
                   for i, s in enumerate(m['samples'])]
    r = validate_review(m, empty_review(m), {'1'*16: {'status': 'named', 'name': '甲'},
                                           '2'*16: {'status': 'unnamed'}})
    progress = enrollment_progress(m, r)
    assert progress['suggested_count'] == 1
    assert not progress['discovery']
    assert not progress['all_ready']
    assert progress['can_finish']  # Soft count recommendation must not become a mandatory chore.
    assert len(registry(m, r)['characters'][0]['samples']) == 1


def test_finish_preserves_decisions_and_edits_invalidate_completion(tmp_path):
    m = data(tmp_path)
    write_json(tmp_path/'manifest.json', m)
    store = ReviewStore(tmp_path)
    payload = {'dataset_id': m['dataset_id'], 'revision': 0}
    with pytest.raises(ValueError, match='至少一位'):
        store.finish(payload)
    before = store.save({**payload, 'changes': {'1'*16: {'status': 'named', 'name': '甲'}}})
    done = store.finish({**payload, 'revision': before['revision']})
    assert done['decisions'] == before['decisions']
    assert store.state()['progress']['finished']
    report = read_json(tmp_path/'匹配候选.json')
    assert report['library_revision'] == done['revision']
    assert {s['id'] for s in report['samples']} == {'2'*16, '3'*16}
    assert len(read_json(tmp_path/'registry.json')['characters'][0]['samples']) == 1
    with pytest.raises(FileExistsError):
        store.finish({**payload, 'revision': before['revision']})
    store.save({**payload, 'revision': done['revision'], 'changes': {'1'*16: {'status': 'pending'}}})
    assert not store.state()['progress']['finished']
    assert not store.state()['progress']['can_finish']


def test_evaluation_removes_test_clip_neighbours_episode_and_identity(tmp_path):
    from autoclip.voices import evaluate_library
    m = data(tmp_path)
    r = validate_review(m, empty_review(m), {'1'*16: {'status': 'named', 'name': '甲'},
                                           '2'*16: {'status': 'named', 'name': '乙'},
                                           '3'*16: {'status': 'named', 'name': '甲'}})
    bank = registry(m, r)
    # Make the second reference independent in time and episode.
    bank['characters'][0]['samples'][1]['source_id'] = 'episode2'
    bank['characters'][0]['samples'][1]['start'] = 40
    bank['characters'][0]['samples'][1]['end'] = 43
    original = __import__('copy').deepcopy(bank)
    report = evaluate_library(bank)
    assert bank == original
    for experiment in report['experiments']:
        modes = experiment['modes']
        assert modes['sample_holdout']['tested'] == 2
        assert modes['sample_holdout']['skipped'] == 1
        for trial in modes['sample_holdout']['trials']:
            assert trial['id'] not in trial['reference_ids']
        for trial in modes['episode_holdout']['trials']:
            expected_other = ['3'*16] if trial['id'] == '1'*16 else ['1'*16, '2'*16]
            assert sorted(trial['reference_ids']) == expected_other
        for trial in modes['speaker_holdout']['trials']:
            if trial['expected_name'] == '甲':
                assert trial['reference_ids'] == ['2'*16]
            else:
                assert trial['reference_ids'] == ['1'*16, '3'*16]
        assert modes['speaker_holdout']['counts'] == {'correct_rejection': 3}


def test_evaluation_nearby_crops_do_not_leak(tmp_path):
    from autoclip.voices import evaluate_library
    m = data(tmp_path)
    r = validate_review(m, empty_review(m), {s['id']: {'status': 'named', 'name': '甲'} for s in m['samples']})
    report = evaluate_library(registry(m, r))
    for experiment in report['experiments']:
        assert experiment['modes']['sample_holdout']['tested'] == 0
        assert experiment['modes']['sample_holdout']['skipped'] == 3
        assert experiment['modes']['episode_holdout']['tested'] == 0


def test_merge_only_human_samples_deduplicates_and_rejects_conflicts(tmp_path):
    import copy
    from autoclip.voices import merge_libraries
    m = data(tmp_path)
    r = validate_review(m, empty_review(m), {'1'*16: {'status': 'named', 'name': '甲'}})
    a = registry(m, r)
    b = copy.deepcopy(a)
    b['dataset_id'] = 'new-episode'
    merged = merge_libraries([a, b])
    assert len(merged['characters'][0]['samples']) == 1
    assert len(merged['parent_libraries']) == 2
    b['characters'][0]['name'] = '乙'
    with pytest.raises(ValueError, match='冲突'):
        merge_libraries([a, b])
    b = copy.deepcopy(a)
    b['revision'] += 1
    with pytest.raises(ValueError, match='不同版本'):
        merge_libraries([a, b])
    b = copy.deepcopy(a)
    b['characters'][0]['samples'][0]['status'] = 'suggested'
    with pytest.raises(ValueError, match='人工确认'):
        merge_libraries([b])


def test_matching_does_not_count_known_reference_as_automatic_success(tmp_path):
    import copy
    from autoclip.voices import match_batch
    m = data(tmp_path)
    r = validate_review(m, empty_review(m), {'1'*16: {'status': 'named', 'name': '甲'}})
    bank = registry(m, r)
    report = match_batch(bank, m)
    assert report['samples'][0]['status'] == 'human_confirmed'
    assert 'score' not in report['samples'][0]
    assert report['samples'][1]['status'] == 'unknown'
    assert report['samples'][2]['status'] == 'suggested'
    altered = copy.deepcopy(m)
    altered['samples'][0]['start'] += 1
    with pytest.raises(ValueError, match='素材不一致'):
        match_batch(bank, altered)


def test_coverage_counts_baseline_aliases_and_preserves_missing_people(tmp_path):
    from autoclip.voices import enrollment_progress
    m = data(tmp_path)
    baseline = registry(m, validate_review(m, empty_review(m), {'1'*16: {'status': 'named', 'name': '甲爷'}}))
    m['dataset_id'] = 'coverage-dataset'
    m['coverage_targets'] = [{'id': 'a', 'name': '甲', 'aliases': ['甲爷']},
                             {'id': 'b', 'name': '乙', 'aliases': []},
                             {'id': 'c', 'name': '丙', 'aliases': []}]
    m['samples'] = [dict(m['samples'][1], target_id='b')]
    review = empty_review(m)
    p = enrollment_progress(m, review, baseline)
    assert [(t['name'], t['status']) for t in p['coverage']] == [('甲', 'confirmed'), ('乙', 'candidate'), ('丙', 'not_located')]
    assert not p['coverage_complete']
    review = validate_review(m, review, {'2'*16: {'status': 'named', 'name': '乙'}})
    p = enrollment_progress(m, review, baseline)
    assert p['coverage'][1]['count'] == 1
    assert not p['coverage_complete']  # Missing people cannot disappear because existing samples suffice.


def test_coverage_finish_blocked_until_every_target_has_human_reference(tmp_path):
    from autoclip.util import digest
    m = data(tmp_path)
    baseline = registry(m, validate_review(m, empty_review(m), {'1'*16: {'status': 'named', 'name': '甲'}}))
    m['dataset_id'] = 'coverage-dataset'
    m['coverage_targets'] = [{'id': 'a', 'name': '甲', 'aliases': []}, {'id': 'b', 'name': '乙', 'aliases': []}]
    m['samples'] = [dict(m['samples'][1], target_id='b')]
    m['baseline_library_digest'] = digest(baseline)
    write_json(tmp_path/'manifest.json', m)
    write_json(tmp_path/'baseline-library.json', baseline)
    store = ReviewStore(tmp_path)
    assert len(read_json(tmp_path/'registry.expanded.json')['characters']) == 1
    payload = {'dataset_id': m['dataset_id'], 'revision': 0}
    with pytest.raises(ValueError, match='未确认人物'):
        store.finish(payload)
    saved = store.save({**payload, 'changes': {'2'*16: {'status': 'named', 'name': '乙'}}})
    assert store.state()['progress']['coverage_complete']
    assert len(read_json(tmp_path/'registry.expanded.json')['characters']) == 2
    store.finish({**payload, 'revision': saved['revision']})
    assert store.state()['progress']['finished']
    baseline['revision'] += 1
    write_json(tmp_path/'baseline-library.json', baseline)
    with pytest.raises(ValueError, match='基础声纹库'):
        ReviewStore(tmp_path)


def test_coverage_alias_confirmation_uses_the_roster_name(tmp_path):
    m=data(tmp_path)
    m['coverage_targets']=[{'id':'hongli','name':'弘历','aliases':['乾隆']}]
    r=validate_review(m,empty_review(m),{'1'*16:{'status':'named','name':'乾隆'}})
    assert r['decisions']['1'*16]['name']=='弘历'
    m['coverage_targets'].append({'id':'other','name':'另一个人','aliases':['乾隆']})
    with pytest.raises(ValueError,match='多个目标人物'):
        validate_review(m,empty_review(m),{'1'*16:{'status':'named','name':'乾隆'}})
