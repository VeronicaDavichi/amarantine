"""验证 njzq-dingtalk 的召回质量。"""

from memory.config import load_settings
from memory.services.recall import recall

s = load_settings()
NS = "njzq-dingtalk"

QUERIES = [
    "反洗钱系统的风险等级字段是怎么区分的",
    "未发起的流程怎么关联风险评级表",
    "分支机构访问反洗钱系统走什么网络",
    "王老师说的inner_client_id是什么",
]

for q in QUERIES:
    print("=" * 70)
    print(f"Q: {q}")
    try:
        r = recall(q, namespace=NS, settings=s)
    except Exception as exc:  # noqa: BLE001
        print(f"  召回失败: {exc}")
        continue
    print(f"  耗时 {r.elapsed_ms:.0f}ms  材料 {len(r.materials)} 条  "
          f"partial={r.partial} degraded={r.degraded}")
    for m in r.materials[:3]:
        print(f"    [{m.get('score', 0):.3f}] {str(m.get('summary'))[:60]}")
    if not r.materials:
        print(f"    empty_reason={r.trace.get('empty_reason')}")
