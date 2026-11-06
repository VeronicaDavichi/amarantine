"""演示 njzq-dingtalk 的召回效果 —— 用真实工作场景的问法。"""

from memory.config import load_settings
from memory.services.recall import recall

s = load_settings()

QUERIES = [
    "反洗钱的风险等级字段怎么区分",
    "谁帮忙查的这些表结构",
    "未发起的流程怎么关联风险评级表",
    "测试环境什么时候能用",
    "分支机构访问反洗钱系统走什么网络",
]

for q in QUERIES:
    print("=" * 74)
    print(f"❓ {q}")
    try:
        r = recall(q, namespace="njzq-dingtalk", settings=s)
    except Exception as exc:  # noqa: BLE001
        print(f"   ✗ {exc}")
        continue
    print(f"   ⏱ {r.elapsed_ms:.0f}ms   材料 {len(r.materials)} 条")
    for m in r.materials[:4]:
        d = str(m.get("date_start") or "")[:10]
        score = m.get("score", 0)
        summ = str(m.get("summary") or "")
        print(f"   [{score:.3f}] {d}  {summ}")
    if not r.materials:
        print(f"   （空：{r.trace.get('empty_reason')}）")
