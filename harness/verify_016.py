#!/usr/bin/env python3
"""016 T5 candidate 池真库验证：冲突项真的进 alias_reject 表、可裁决。

用法（需容器/服务在跑）：

    python3 harness/verify_016.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.config import load_settings  # noqa: E402
from memory.services.alias_reject import queue_from_settings  # noqa: E402

NS = "verify016b"


def main() -> int:
    settings = load_settings()
    q = queue_from_settings(settings)
    before = q.count_pending(NS)

    # 直接走 apply_aliases 的 LLM 分支不方便（要真实 LLM），
    # 这里验证**池子本身**：record_safe 写入 → 可列出 → 可裁决。
    from memory.services.alias_reject import record_safe

    n = record_safe(
        settings,
        namespace=NS,
        event_id="evt_verify016",
        rejects=[{
            "alias": "张三丰",
            "canonical": "张三",
            "side": "candidate",
            "reason": "candidate_ambiguous",
            "detail": "single_char_prefix",
        }],
        source_text="candidate_pool:evt_verify016",
    )
    print(f"写入 {n} 条（此前待办 {before}）")

    items = q.list_pending(NS, limit=10)
    print(f"待办 {len(items)} 条：")
    for it in items:
        print(f"  {it.alias} vs {it.canonical} | side={it.side} | "
              f"reason={it.reason} | detail={it.detail}")

    ok_pool = any(i.alias == "张三丰" and i.reason == "candidate_ambiguous"
                  for i in items)

    # 裁决为 accepted → 应从待办里消失
    done = q.resolve(NS, event_id="evt_verify016", alias="张三丰",
                     canonical="张三", side="candidate", resolution="accepted")
    left = q.list_pending(NS, limit=10)
    ok_resolve = done and not any(i.alias == "张三丰" for i in left)

    print(f"\n池子写入+可列出: {'✓' if ok_pool else '✗'}")
    print(f"裁决后移出待办: {'✓' if ok_resolve else '✗'}")
    return 0 if (ok_pool and ok_resolve) else 1


if __name__ == "__main__":
    sys.exit(main())
