#!/usr/bin/env python3
"""spec 036 T1：复现「切分超限静默截断」，并验证 `split_all()` 不丢。

用法（仓库根目录）：

    ./harness/dev-linux.sh python ../harness/verify_split_truncation.py

预期输出（退出码 0）：

    raw=65 kept=40            ← split_events，旧接口：截断丢 25 条
    raw=65 kept=65 dropped=0  ← split_all，新接口：二分后全保住

为什么用假 LLM：这个脚本要证明的是**切分层的截断语义**，与模型质量无关。
真调一次 LLM 反而会把「65 条」这个前提变成运气。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.config import load_settings  # noqa: E402
from memory.services import llm_split  # noqa: E402

#: 实测最坏一批切出 65 条（spec 036 进展记录：10-30 丢 25–29 条）
RAW = 65


def _fake_chat(
    messages, settings, *, system=None, max_tokens=1024, client=None, timeout_s=None
):
    """假 LLM：送进来几行就吐几条，模拟「这批本来就该切出这么多」。"""
    body = messages[0]["content"].split("\n", 1)[1]
    return json.dumps([line.strip() for line in body.splitlines() if line.strip()])


def main() -> int:
    text = "\n".join(
        f"user: 第{i}轮，讨论项目A延期与联调排期" for i in range(RAW)
    )
    llm_split.chat = _fake_chat  # 不碰网络

    old = llm_split.split_events(text, load_settings())
    new = llm_split.split_all(text, load_settings())

    print(f"raw={RAW} kept={len(old)}"
          f"  ← split_events（旧：截断，丢 {RAW - len(old)} 条）")
    print(f"raw={RAW} kept={len(new.parts)} dropped={new.dropped} rounds={new.rounds}"
          f"  ← split_all（新：二分）")

    ok = (
        len(old) == llm_split.MAX_EVENTS
        and len(new.parts) == RAW
        and new.dropped == 0
        and new.rounds >= 2
    )
    print("OK: 现象复现 + 修复生效" if ok else "FAIL: 与预期不符")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
