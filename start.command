#!/bin/zsh
# Mac双击入口：切换到脚本所在目录，准备虚拟环境，再启动程序。
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd -- "$(dirname -- "$0")"

if [[ ! -x .venv/bin/python ]]; then
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "请先安装 Python 3.10 或以上版本。")'
    python3 -m venv .venv
fi

if ! .venv/bin/python -c 'import openai_codex, mcp, reportlab, pypdf' 2>/dev/null; then
    print "首次运行：正在安装项目依赖，需要联网……"
    .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python main.py
