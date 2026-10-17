#!/bin/zsh
cd -- "${0:A:h}"
voice_review_root='output/声音确认/雍正王朝'
voice_review_port=8766
if [[ -f output/声音确认/人物补录/manifest.json ]]; then
  voice_review_root='output/声音确认/人物补录'
  voice_review_port=8768
fi
if [[ ! -f "$voice_review_root/manifest.json" ]]; then
  print '尚未准备试听资料，请按 docs/人工声纹确认.md 运行准备命令。'
  read '?按回车关闭'
  exit 1
fi
.venv/bin/python - "$voice_review_root" "$voice_review_port" <<'PY'
import json
import sys
import webbrowser
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
from autoclip.voice_review import serve
root, port = Path(sys.argv[1]), int(sys.argv[2])
url = f'http://127.0.0.1:{port}'
expected = json.loads((root/'manifest.json').read_text())['dataset_id']
try:
    with build_opener(ProxyHandler({})).open(url+'/api/state', timeout=2) as response:
        active = json.load(response)
    if active['manifest']['dataset_id'] == expected:
        webbrowser.open(url)
        sys.exit(0)
except (OSError, ValueError, KeyError):
    pass
serve(root, port)
PY
