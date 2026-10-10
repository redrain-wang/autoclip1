#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo '请先按 README.md 安装项目依赖。'
  exit 1
fi
video_source="${1:-$(.venv/bin/python -c 'import os; from dotenv import load_dotenv; load_dotenv(); print(os.getenv("AUTOCLIP_SOURCE_DIR", "videos/雍正王朝"))')}"
.venv/bin/python -m autoclip batch "$video_source" --clips 0
.venv/bin/python -m autoclip export
.venv/bin/python -m autoclip gallery --open-browser
