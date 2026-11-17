#!/usr/bin/env bash
# 常规验收：静态检查 + 真库验收（真库部分需要容器/服务在跑）。
#
#   bash harness/verify_all.sh          # 静态检查 + 真库（会先 wake）
#   bash harness/verify_all.sh --static # 只跑静态检查（不需要容器）
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

STATIC_ONLY=0
[ "${1:-}" = "--static" ] && STATIC_ONLY=1
FAIL=0

echo "=== 1. 单元测试 ==="
if ( cd back_end && PYTHONIOENCODING=utf-8 .venv-linux/bin/python3 -m pytest -q 2>&1 | tail -1 ); then
  :
else
  FAIL=1
fi

echo
echo "=== 2. spec 骨架 ==="
python3 harness/check_spec.py 2>&1 | tail -1 || FAIL=1

echo
echo "=== 3. 文档与代码一致 ==="
python3 harness/check_guide.py 2>&1 | tail -1 || FAIL=1

if [ "$STATIC_ONLY" -eq 1 ]; then
  echo
  echo "（--static：跳真库验收）"
  exit "$FAIL"
fi

echo
echo "=== 4. 唤醒依赖 ==="
bash harness/wake.sh 2>&1 | tail -3 || FAIL=1

echo
echo "=== 5. 真库验收：046 指代回溯 / 卡归并 ==="
( cd back_end && PYTHONIOENCODING=utf-8 .venv-linux/bin/python3 ../harness/verify_046.py 2>&1 | tail -6 ) || FAIL=1

echo
echo "=== 6. 真库验收：047 群名片 / 头衔 ==="
( cd back_end && PYTHONIOENCODING=utf-8 .venv-linux/bin/python3 ../harness/verify_047.py 2>&1 | tail -8 ) || FAIL=1

echo
[ "$FAIL" -eq 0 ] && echo "全部通过" || echo "有未通过项"
exit "$FAIL"
