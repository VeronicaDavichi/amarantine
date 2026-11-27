#!/usr/bin/env bash
# amarantine MCP 启动器 —— 给 MCP 宿主（Claude Desktop / DSH / Cherry Studio…）用。
#
# ## 为什么要有这个文件
#
# 直接在宿主配置里写 `bash -lc "cd … && MEMORY_NAMESPACE=… exec python -m …"`
# 有两个坑，实测都踩过：
#
# 1. **引号**：`bash -lc` 的 `-c` 只吃**一个**字符串参数。宿主如果把命令按空格
#    拆成多个 args，`-c` 只拿到 `cd`，后面的 `&& …` 全被丢掉 —— 进程启动后立刻
#    退出，宿主表现为「已连接但未注册任何工具」。
# 2. **stdout 只能是协议**：日志必须落文件，否则宿主解析失败。
#
# 所以把复杂命令收进这个脚本，宿主配置只需要：
#
#     wsl -d <发行版> --exec bash /mnt/<盘>/<路径>/amarantine/harness/mcp-start.sh
#
# 没有任何空格、`&&`、引号需要转义。
#
# ## 用法
#
# 用法
#
#   mcp-start.sh                 # 用 back_end/.env 里配的 MEMORY_NAMESPACE
#   mcp-start.sh qq-2751722562   # **命令行指定记忆域**（优先级最高）
#
# 给运行的 MCP 换域用第二种：宿主配置里加一个 arg 就行，不用改 .env。
#
# 环境变量（都可覆盖）：
#   MEMORY_NAMESPACE  记忆域；命令行参数优先于它
#   AMARANTINE_ROOT   仓库根，默认从本脚本位置推断
#   AMARANTINE_PYTHON 解释器，默认 back_end/.venv-linux/bin/python3
#   LOG_FILE          MCP 日志（不会写 stdout）

set -u

# 第一个位置参数 = 记忆域（优先级最高）
CLI_NAMESPACE="${1:-}"

# 仓库根：本脚本在 <root>/harness/ 下
ROOT="${AMARANTINE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKEND="$ROOT/back_end"

if [ ! -d "$BACKEND" ]; then
  echo "amarantine mcp: 找不到 $BACKEND" >&2
  exit 1
fi

PY="${AMARANTINE_PYTHON:-$BACKEND/.venv-linux/bin/python3}"
if [ ! -x "$PY" ]; then
  echo "amarantine mcp: 解释器不存在：$PY" >&2
  echo "  先建虚拟环境：cd $BACKEND && uv sync --frozen" >&2
  exit 1
fi

# 日志：**绝不能走 stdout**（stdout 只承载 JSON-RPC）
export LOG_FILE="${LOG_FILE:-/tmp/amarantine-mcp.log}"

# 记忆域优先级：命令行参数 > 环境变量 > back_end/.env
if [ -n "$CLI_NAMESPACE" ]; then
  MEMORY_NAMESPACE="$CLI_NAMESPACE"
  export MEMORY_NAMESPACE
elif [ -z "${MEMORY_NAMESPACE:-}" ] && [ -f "$BACKEND/.env" ]; then
  MEMORY_NAMESPACE=$(grep -E '^MEMORY_NAMESPACE=' "$BACKEND/.env" | head -1 | cut -d= -f2-)
  export MEMORY_NAMESPACE
fi

if [ -z "${MEMORY_NAMESPACE:-}" ]; then
  # **必须吵**：namespace 为空时服务会以退出码 1 结束、原因只在日志里，
  # 宿主看到的是「已连接但没有工具」，极难排查（实测踩过）。
  # 启动阶段协议还没建立，往 stderr 写不会污染 JSON-RPC。
  {
    echo "amarantine mcp: MEMORY_NAMESPACE 未设置，无法启动。"
    echo "  三种修法任选其一："
    echo "    1) 启动时传参：mcp-start.sh <你的域>"
    echo "    2) 写进 $BACKEND/.env：MEMORY_NAMESPACE=<你的域>"
    echo "    3) 在宿主配置里注入环境变量 MEMORY_NAMESPACE=<你的域>"
    echo "  可用域：python3 $ROOT/harness/purge_namespace.py --list"
  } >&2
  exit 1
fi

cd "$BACKEND" || exit 1
exec "$PY" -m memory.entrypoints.mcp_stdio
