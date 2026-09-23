#!/bin/sh
set -eu
CLAUDE_AO3_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$CLAUDE_AO3_ROOT"
if command -v uv >/dev/null 2>&1; then
  uv --no-cache sync --frozen --no-dev --no-python-downloads
else
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "需要 Python 3.11 或更高版本")'
  python3 -m venv .venv
  .venv/bin/python -m pip install --no-cache-dir .
fi
.venv/bin/python scripts/install_command.py "$@"
printf '\n%s\n' '运行 claude-ao3 启动；claude-ao3 --doctor 检查书源。旧 mini-notes 仅作兼容入口。'
