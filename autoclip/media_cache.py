"""Content-addressed shot reuse across scripts; subtitles are assembled afterward."""

import fcntl
import hashlib
import tempfile
from pathlib import Path

from .util import digest, duration, read_json, write_json


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def shot_inputs(source, beat, target, voice, cfg):
    data = {'version': 1, 'source': source, 'start': beat.start, 'end': beat.end,
            'mode': beat.mode, 'target_seconds': target, 'portrait': cfg.portrait,
            'encoding': 'x264-veryfast-crf21-fps25-aac160k-48000-stereo'}
    if voice is not None:
        data.update(voice_sha256=file_digest(voice), original_volume=cfg.original_volume)
    return data


def cached_shot(folder, inputs, encode):
    """Publish only complete shots; validate bytes and serialize same-key writers."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    key = digest(inputs)
    path, marker = folder/f'{key}.mp4', folder/f'{key}.json'
    with (folder/f'{key}.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and marker.exists():
            try:
                record = read_json(marker)
                if record['inputs'] == inputs and record['sha256'] == file_digest(path) and record['duration'] > 0:
                    return path, record['duration'], True
            except (OSError, ValueError, KeyError):
                pass
        with tempfile.NamedTemporaryFile(dir=folder, prefix=key+'.', suffix='.mp4', delete=False) as stream:
            partial = Path(stream.name)
        try:
            encode(partial)
            seconds = duration(partial)
            if seconds <= 0 or abs(seconds-inputs['target_seconds']) > .5:
                raise ValueError('片段缓存时长与目标不符')
            partial.replace(path)
            write_json(marker, {'inputs': inputs, 'sha256': file_digest(path), 'duration': seconds})
            return path, seconds, False
        finally:
            partial.unlink(missing_ok=True)
