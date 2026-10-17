"""Loopback-only listening desk with atomic, revision-checked human decisions."""
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .util import digest, read_json, run, source_identity, write_json
from .voices import empty_review, enrollment_progress, merge_libraries, registry, validate_review


class ReviewStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest = read_json(self.root/'manifest.json')
        self.path = self.root/'review.json'
        self.lock = threading.RLock()
        self.media_lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.samples = {s['id']: s for s in self.manifest['samples']}
        self.sources = {s['id']: s for s in self.manifest['sources']}
        self.baseline = None
        if self.manifest.get('baseline_library_digest'):
            self.baseline = read_json(self.root/'baseline-library.json')
            if digest(self.baseline) != self.manifest['baseline_library_digest']:
                raise ValueError('补录页的基础声纹库发生变化，请重新准备')
        self.verify_sources()
        review = self.review()
        if review['dataset_id'] != self.manifest['dataset_id']:
            raise ValueError('确认记录与当前声音批次不匹配')
        self.export(review)

    def combined_library(self, review):
        local = registry(self.manifest, review)
        return merge_libraries([self.baseline, local]) if self.baseline is not None else local

    def export(self, review):
        write_json(self.root/'registry.json', registry(self.manifest, review))
        if self.baseline is not None:
            write_json(self.root/'registry.expanded.json', self.combined_library(review))

    def verify_sources(self):
        references = [row['source'] for row in self.sources.values()]
        if self.baseline is not None:
            references += [s['source'] for p in self.baseline['characters'] for s in p['samples']]
        for source in {digest(r): r for r in references}.values():
            if source_identity(Path(source['path'])) != source:
                raise ValueError('原片版本已改变，请重新准备声音样本')

    def review(self):
        return read_json(self.path) if self.path.exists() else empty_review(self.manifest)

    def state(self):
        with self.lock:
            public = {**self.manifest, 'samples': [{k: v for k, v in s.items() if k != 'embedding'}
                                                 for s in self.manifest['samples']]}
            review = self.review()
            return {'manifest': public, 'review': review,
                    'progress': enrollment_progress(self.manifest, review, self.baseline)}

    def save(self, payload):
        with self.lock:
            previous = self.review()
            if payload.get('dataset_id') != self.manifest['dataset_id']:
                raise ValueError('声音批次已改变，请刷新页面')
            if payload.get('revision') != previous['revision']:
                raise FileExistsError('另一个页面已保存修改，请刷新后重试')
            updated = validate_review(self.manifest, previous, payload.get('changes'))
            self.verify_sources()
            write_json(self.root/'history'/f"review-{previous['revision']:06}.json", previous)
            write_json(self.path, updated)
            self.export(updated)
            return updated

    def finish(self, payload):
        with self.lock:
            previous = self.review()
            if payload.get('dataset_id') != self.manifest['dataset_id']:
                raise ValueError('声音批次已改变，请刷新页面')
            if payload.get('revision') != previous['revision']:
                raise FileExistsError('另一个页面已保存修改，请刷新后重试')
            progress = enrollment_progress(self.manifest, previous, self.baseline)
            if self.manifest.get('coverage_targets') and not progress['coverage_complete']:
                raise ValueError('名单仍有未确认人物；当前进度已保存，可稍后继续')
            if not progress['can_finish']:
                raise ValueError('请先确认至少一位需要识别的角色')
            self.verify_sources()
            updated = {**previous, 'revision': previous['revision']+1,
                       'enrollment': {'status': 'finished'}}
            report = {'dataset_id': self.manifest['dataset_id'], 'library_revision': updated['revision'],
                      'threshold': .65, 'margin': .10,
                      'samples': [{k: s[k] for k in ('id', 'source_id', 'start', 'end')} |
                                  progress['matches'][s['id']] for s in self.manifest['samples']
                                  if s['id'] in progress['matches']]}
            write_json(self.root/'history'/f"review-{previous['revision']:06}.json", previous)
            write_json(self.root/'匹配候选.json', report)
            write_json(self.path, updated)
            self.export(updated)
            return updated

    def media(self, sid, context=False):
        sample = self.samples[sid]
        source = self.sources[sample['source_id']]
        video = Path(source['source']['path'])
        if source_identity(video) != source['source']:
            raise ValueError('原片已改变，不能播放旧时间位置')
        target = self.root/'media'/f"{sid}{'-context.mp4' if context else '.wav'}"
        with self.media_lock:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                start = max(0, sample['start']-3) if context else sample['start']
                end = min(source['duration'], sample['end']+3) if context else sample['end']
                temp = target.with_name(target.stem+'.tmp'+target.suffix)
                args = ['ffmpeg', '-v', 'error', '-y', '-ss', start, '-i', video, '-t', end-start]
                args += (['-vf', 'scale=640:-2', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26',
                          '-c:a', 'aac', '-movflags', '+faststart'] if context else
                         ['-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le'])
                try:
                    run([*args, temp], timeout=120)
                    temp.replace(target)
                finally:
                    temp.unlink(missing_ok=True)
        return target


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def trusted_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def send_bytes(self, data, content_type, status=200):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; media-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def json_response(self, data, status=200):
            self.send_bytes(json.dumps(data, ensure_ascii=False).encode(), 'application/json; charset=utf-8', status)

        def do_GET(self):
            if not self.trusted_host():
                self.json_response({'error': '请通过本机地址访问'}, 403)
                return
            path = urlsplit(self.path).path
            try:
                if path == '/':
                    template = 'voice_coverage.html' if store.manifest.get('coverage_targets') else 'voice_review.html'
                    html = (Path(__file__).parent/'web'/template).read_text(encoding='utf-8')
                    self.send_bytes(html.replace('__TOKEN__', store.token).encode(), 'text/html; charset=utf-8')
                elif path == '/api/state':
                    self.json_response(store.state())
                elif path == '/api/registry':
                    with store.lock:
                        self.json_response(store.combined_library(store.review()))
                elif path == '/api/matches':
                    with store.lock:
                        report = store.root/'匹配候选.json'
                        review = store.review()
                        if not report.exists() or review.get('enrollment', {}).get('status') != 'finished':
                            self.json_response({'error': '请先完成本轮，生成最新比对结果'}, 409)
                        else:
                            data = read_json(report)
                            if data['library_revision'] != review['revision'] or data['dataset_id'] != review['dataset_id']:
                                self.json_response({'error': '比对结果已过期，请重新完成本轮'}, 409)
                            else:
                                self.json_response(data)
                elif match := re.fullmatch(r'/(audio|context)/([a-f0-9]{16})', path):
                    kind, sid = match.groups()
                    if sid not in store.samples:
                        self.json_response({'error': '片段不存在'}, 404)
                        return
                    file = store.media(sid, kind == 'context')
                    size, start, end, status = file.stat().st_size, 0, file.stat().st_size-1, 200
                    if requested := self.headers.get('Range'):
                        rng = re.fullmatch(r'bytes=(\d+)-(\d*)', requested)
                        if not rng or int(rng[1]) >= size or (rng[2] and int(rng[2]) < int(rng[1])):
                            self.send_response(416)
                            self.send_header('Content-Range', f'bytes */{size}')
                            self.send_header('Content-Length', '0')
                            self.end_headers()
                            return
                        start = int(rng[1])
                        end = min(int(rng[2]), end) if rng[2] else end
                        status = 206
                    self.send_response(status)
                    self.send_header('Content-Type', 'video/mp4' if kind == 'context' else 'audio/wav')
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Content-Length', str(end-start+1))
                    if status == 206:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    self.end_headers()
                    with file.open('rb') as stream:
                        stream.seek(start)
                        remaining = end-start+1
                        while remaining > 0:
                            data = stream.read(min(65536, remaining))
                            if not data:
                                break
                            self.wfile.write(data)
                            remaining -= len(data)
                else:
                    self.json_response({'error': '页面不存在'}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, OSError, RuntimeError) as exc:
                self.json_response({'error': str(exc)}, 400)

        def do_POST(self):
            if not self.trusted_host() or self.headers.get('X-Review-Token') != store.token:
                self.json_response({'error': '请求校验失败，请刷新本机页面'}, 403)
                return
            if self.path not in {'/api/review', '/api/finish'}:
                self.json_response({'error': '接口不存在'}, 404)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    raise ValueError('提交内容大小无效')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('提交内容格式无效')
                operation = store.finish if self.path == '/api/finish' else store.save
                operation(payload)
                self.json_response(store.state())
            except FileExistsError as exc:
                self.json_response({'error': str(exc)}, 409)
            except (ValueError, TypeError, OSError) as exc:
                self.json_response({'error': str(exc)}, 400)
    return Handler


def serve(root, port=8766, open_browser=True):
    import webbrowser
    store = ReviewStore(root)
    server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(store))
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'声音确认台：{url}；确认记录保存于 {store.root}', flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
