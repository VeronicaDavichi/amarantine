#!/usr/bin/env bash
# env.sh — harness 变量集中出口。只放路径类变量，**不放任何密钥**。
#
# 需要密钥/地址时在调用前覆盖，不要写进仓库：
#   export E2E_BASE_URL="http://127.0.0.1:3000"
#   export VISUAL_ROUTES_FILE="specs/001-x/assets/routes.json"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT_DIR
