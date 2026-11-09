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
# 继承不到那条路由，直连必然超时。
#
# `harness/vpn_relay.py` 是 **Windows 侧** 的裸 TCP 转发：它监听 3001，
# 转发到 VPN 后的 3000。WSL 这边连「默认网关:3001」即可。
#
# 端口别搞混：3001 是 relay，3000 是网关。WSL 连 3000 会一直超时
# （2026-11-09 实测踩过一次：切分 63s 超时降级成整段原文卡）。
#
# WSL 网关 IP 每次 `wsl --shutdown` 会变，所以每次动态取。
RELAY_PORT="${RELAY_PORT:-3001}"
RELAY_TARGET="${RELAY_TARGET:-192.168.0.58:3000}"
_wsl_gw="$(ip route show default 2>/dev/null | awk '{print $3; exit}')"

if [[ -n "${_wsl_gw}" ]]; then
    if [[ "${MEMORY_KEEP_LLM_BASE_URL:-0}" != "1" ]]; then
        export LLM_BASE_URL="http://${_wsl_gw}:${RELAY_PORT}"
    fi

    # relay 活着吗？死了就尝试拉起（Windows 侧进程，退出 WSL 也不受影响）
    if ! timeout 2 bash -c ":</dev/tcp/${_wsl_gw}/${RELAY_PORT}" 2>/dev/null; then
        echo "[dev-linux] relay 未监听 ${_wsl_gw}:${RELAY_PORT}，尝试在 Windows 侧拉起…" >&2
        _repo_win="$(wslpath -w "${REPO_ROOT}" 2>/dev/null || true)"
        _py_win="${_repo_win}\\back_end\\.venv\\Scripts\\python.exe"
        _relay_win="${_repo_win}\\harness\\vpn_relay.py"
        if command -v powershell.exe >/dev/null 2>&1 && [[ -n "${_repo_win}" ]]; then
            powershell.exe -NoProfile -WindowStyle Hidden -Command \
                "Start-Process -WindowStyle Hidden -FilePath '${_py_win}' -ArgumentList @('${_relay_win}','--listen','${RELAY_PORT}','--target','${RELAY_TARGET}')" \
                >/dev/null 2>&1 || true
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                if timeout 2 bash -c ":</dev/tcp/${_wsl_gw}/${RELAY_PORT}" 2>/dev/null; then
                    echo "[dev-linux] relay 已就绪 ${_wsl_gw}:${RELAY_PORT} → ${RELAY_TARGET}" >&2
                    break
                fi
                sleep 1
            done
        else
            echo "[dev-linux] 无法自动拉起（缺 powershell.exe）。请在 Windows 执行：" >&2
            echo "  .\\back_end\\.venv\\Scripts\\python.exe harness\\vpn_relay.py --listen ${RELAY_PORT} --target ${RELAY_TARGET}" >&2
        fi
        if ! timeout 2 bash -c ":</dev/tcp/${_wsl_gw}/${RELAY_PORT}" 2>/dev/null; then
            echo "[dev-linux] ⚠ relay 仍不可达：LLM 切分会超时降级成整段原文卡" >&2
        fi
    fi
fi
unset _wsl_gw

if ! command -v uv >/dev/null 2>&1; then
    echo "FATAL: uv 未安装。装一个：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

exec uv run "$@"
