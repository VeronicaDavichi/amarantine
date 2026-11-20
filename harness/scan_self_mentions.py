# -*- coding: utf-8 -*-
"""查：有多少卡摘要里出现「本人」的某个名字或代词。"""
import json
import sys
import urllib.request

sys.path.insert(0, "/mnt/e/PycharmProjects/amarantine/back_end/src")
sys.stdout.reconfigure(encoding="utf-8")
from memory.services.local_paths import load_settings_from_project  # noqa: E402
from memory.services.self_identity import resolve_self  # noqa: E402

NS = "qq-10000000001"
s = load_settings_from_project()
got = resolve_self(s, namespace=NS)
names = {*(got.get("names") or []), *(got.get("nicknames") or []),
         got.get("canonical") or ""} - {""}
print(f"本人名字 {len(names)} 个；示例：{sorted(names)[:4]}")


def gql(q):
    r = urllib.request.Request("http://127.0.0.1:8081/v1/graphql",
                               data=json.dumps({"query": q}).encode(),
                               headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=120).read())


# 拉全部摘要（4383 张，分页）
texts = []
offset = 0
while True:
    q = ('{ Get { MemoryEvent(where: {path:["namespace"], operator: Equal, '
         f'valueText:"{NS}"}}, limit: 500, offset: {offset}) {{ summary }} }} }}')
    rows = (((gql(q).get("data") or {}).get("Get") or {}).get("MemoryEvent") or [])
    if not rows:
        break
    texts.extend(r.get("summary") or "" for r in rows)
    if len(rows) < 500:
        break
    offset += 500
print("卡片摘要数:", len(texts))

hits = {}
for n in names:
    c = sum(1 for t in texts if n and n in t)
    if c:
        hits[n] = c
print(f"\n摘要里出现本人名字的卡（{len(hits)} 个名字命中）：")
for n, c in sorted(hits.items(), key=lambda kv: -kv[1])[:12]:
    print(f"  {n!r:<40} {c} 张")

for pron in ("我", "自己", "本人"):
    c = sum(1 for t in texts if pron in t)
    print(f"  代词 {pron!r}: {c} 张")
