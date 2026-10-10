#!/bin/zsh
set -e
cd "$(dirname "$0")"
.venv/bin/python -m autoclip gallery --open-browser
