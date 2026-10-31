#!/usr/bin/env bash
# 在 WSL 里跑后端命令的统一入口。
#
# 用法：
#   ./harness/dev-linux.sh pytest -q
#   ./harness/dev-linux.sh python -c "print('hi')"
#   ./harness/dev-linux.sh ./harness/seed.py
#
# 为什么需要它：
#   - Windows 的 .venv 是 Scripts/ 布局，Linux 用 bin/，两者不能混用
#   - 所以后端在 WSL 里用独立的 .venv-linux（由 uv 管理）
#   - 这个脚本把 PATH / venv / cwd 一次设对，免得每次手敲
set -euo pipefail

# uv 装在 ~/.local/bin
export PATH="${HOME}/.local/bin:${PATH}"

# 独立 venv：不覆盖 Windows 的 .venv
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-.venv-linux}"

# 用 UTF-8，避免中文 summary 在某些 locale 下乱码
export PYTHONIOENCODING="utf-8"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}/back_end"

if ! command -v uv >/dev/null 2>&1; then
    echo "FATAL: uv 未安装。装一个：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

exec uv run "$@"
