#!/bin/zsh
set -e
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo '请先按 README.md 安装项目依赖。'
  exit 1
fi
.venv/bin/python -m autoclip produce "$@"
.venv/bin/python -m autoclip gallery --output 'output/新片-长版' --port 8766 --open-browser
