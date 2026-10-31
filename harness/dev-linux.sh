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

# ---------- LLM 网关走 Windows 侧转发 ----------
# 网关（如 192.168.0.58:3000）只在 Windows 的 aTrust VPN 后面，WSL 的 NAT 网络
# 继承不到那条路由，直连必然超时。harness/vpn_relay.py 在 Windows 上把
# 3000 端口转发过去，WSL 这里连默认网关即可。
#
# 注意 WSL 网关 IP 在每次 `wsl --shutdown` 之后会变，所以**每次动态取**，
# 不要写死到 .env 里（.env 里同时留了注释说明）。
_wsl_gw="$(ip route show default 2>/dev/null | awk '{print $3; exit}')"
if [[ -n "${_wsl_gw}" ]]; then
    # 只有 .env 没显式指定时才覆盖，方便临时切回直连排查
    if [[ "${MEMORY_KEEP_LLM_BASE_URL:-0}" != "1" ]]; then
        export LLM_BASE_URL="${LLM_BASE_URL:-http://${_wsl_gw}:3000}"
    fi
fi
unset _wsl_gw

if ! command -v uv >/dev/null 2>&1; then
    echo "FATAL: uv 未安装。装一个：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

exec uv run "$@"
