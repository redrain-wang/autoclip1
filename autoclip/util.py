import hashlib
import json
import subprocess
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def run(args: list[str], timeout=1800):
    p = subprocess.run([str(x) for x in args], capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"{args[0]} failed: {p.stderr[-2500:]}")
    return p.stdout


def probe(path: Path) -> dict:
    return json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path]))


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    h, milliseconds = divmod(milliseconds, 3600000)
    m, milliseconds = divmod(milliseconds, 60000)
    s, milliseconds = divmod(milliseconds, 1000)
    return f"{h:02}:{m:02}:{s:02},{milliseconds:03}"


def source_identity(path: Path) -> dict:
    stat = path.stat()
    # Hash first and last MiB as well as stat metadata; don't reread entire multi-GB episodes.
    with path.open("rb") as f:
        head = f.read(1048576)
        f.seek(max(0, stat.st_size - 1048576))
        tail = f.read(1048576)
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "sample_sha256": hashlib.sha256(head + tail).hexdigest()}
