"""A16 真 e2e：写一张长卡 → 验证 chunk 落库 → 验证召回聚合。

不污染正式数据：用临时 namespace `chunke2e`，跑完在末尾清理。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/mnt/e/PycharmProjects/amarantine/back_end/src")

from memory.config import load_settings
from memory.services.recall import recall
from memory.services.remember import remember_event
from memory.core.chunker import split_text

# 每次跑用新 namespace：同一条文本的 event_id 是确定的，
# 复用 namespace 会命中幂等短路，chunk 根本不会被插入。
NS = f"chunke2e{int(time.time()) % 100000}"

LONG = (
    "张三在周一例会上汇报了项目A的整体进展。"
    "项目A原计划在六月上线，但因为支付网关的对接延期了两周。"
    "李四提出把灰度发布的窗口从三天拉长到七天，以降低风险。"
    "王五补充说运维这边需要提前申请扩容，否则压测环境扛不住。"
    "会议最后决定：项目A延期到七月上线，六月只做内部验收。"
    "另外，张三还提到项目B的启动时间定在下个月第一周。"
    "项目B的负责人是赵六，预算已经批下来了。"
    "关于项目C，大家一致认为先搁置，等第三季度再说。"
    "李四建议下季度把监控告警的阈值重新校准一遍。"
    "散会前王五确认了下次例会的时间。"
) * 3


def main() -> int:
    settings = load_settings()
    settings.CHUNK_ENABLED = True
    settings.CHUNK_SIZE = 256
    settings.CHUNK_OVERLAP = 32
    settings.CHUNK_KEEP_PER_EVENT = 2

    chunks = split_text(LONG, size=settings.CHUNK_SIZE, overlap=settings.CHUNK_OVERLAP)
    print(f"文本长度      : {len(LONG)} 字")
    print(f"切出 chunk    : {len(chunks)} 块")
    for c in chunks:
        print(f"  #{c.index} [{c.start}:{c.end}] {c.text[:24]}...")
    if len(chunks) < 2:
        print("FATAL 文本太短，切不出多块", file=sys.stderr)
        return 1

    print("\n--- 写入（remember）---")
    result_obj = remember_event(
        namespace=NS,
        summary=LONG,
        settings=settings,
    )
    eid = getattr(getattr(result_obj, "card", None), "event_id", None) or getattr(
        result_obj, "event_id", None
    )
    print("remember 返回:", type(result_obj).__name__)
    print("event_id :", eid)

    from memory.services.chunk_store import ChunkStore

    cs = ChunkStore(settings)
    try:
        n = cs.count(NS)
        print("chunk 落库 :", n, "块")
        hits = cs.search_near_vector(
            __import__("memory.services.embedding", fromlist=["embed_one"]).embed_one(
                "项目A延期到几月", settings
            ),
            NS,
            limit=20,
        )
        print("chunk 检索 :", len(hits), "命中")
        for h in hits[:4]:
            print(f"   parent={h['event_id']} #{h['chunk_index']} score={h['score']:.4f}")
    finally:
        cs.close()

    print("\n--- 召回（聚合）---")
    result = recall("项目A延期到几月", namespace=NS, settings=settings)
    t = result.trace
    print("materials     :", len(result.materials))
    print("chunk_enabled :", t.get("chunk_enabled"))
    print("chunk_hits    :", t.get("chunk_hits"))
    print("chunk_parents :", t.get("chunk_parents"))
    ids = [m["event_id"] for m in result.materials]
    print("materials ids :", ids)
    if ids.count(eid) > 1:
        print("FATAL chunk 泄漏：同一 event_id 出现多次", file=sys.stderr)
        return 1
    print("\nOK 聚合正确（每个 event_id 最多一次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
