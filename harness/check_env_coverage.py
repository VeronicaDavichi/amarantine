"""校验 `.env.example` 与 `Settings` 字段的覆盖关系。

两种漂移都能抓到：
- config 有字段但 .env.example 没写 → 用户不知道有这个开关
- .env.example 有条目但 config 没这个字段 → 拼写错，设了也不生效

用法：
    ./harness/dev-linux.sh python ../harness/check_env_coverage.py
"""

from __future__ import annotations

import pathlib
import re

from memory.config import Settings

ENV_PATH = pathlib.Path(__file__).resolve().parent.parent / "back_end" / ".env.example"


def main() -> int:
    env_text = ENV_PATH.read_text()
    keys = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", env_text, re.M))
    fields = set(Settings.model_fields)

    present = sorted(fields & keys)
    missing = sorted(fields - keys)
    extra = sorted(keys - fields)

    print(f"config 字段 {len(fields)} / .env.example 条目 {len(keys)}")
    print(f"  已覆盖: {len(present)}")
    print()

    if extra:
        print(f"⚠️  .env.example 有但 Settings 没有（拼写错？设了不生效）:")
        for k in extra:
            print(f"     {k}")
        print()

    if missing:
        print(f"⚠️  Settings 有但 .env.example 没写（用户不知道这个开关）: {len(missing)}")
        for k in missing:
            print(f"     {k}")
        print()

    if not missing and not extra:
        print("✓ .env.example 与 Settings 完全对齐")
        return 0

    # 缺失不一定是错（有些字段故意不暴露），但至少要人看一眼
    return 0 if not extra else 1


if __name__ == "__main__":
    raise SystemExit(main())
