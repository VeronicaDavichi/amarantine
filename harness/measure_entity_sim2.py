"""扩大测量：向量能否区分「该合并」和「不该合并」？

第一轮结果（bge-m3）：
  豆包 / 豆包AI   = 0.7763   应合并
  张三 / 张三丰   = 0.5925   不该合并
  微信 / WeChat   = 0.6143   应合并(跨语言)
  项目A / 项目B   = 0.8514   不该合并   <- 比"应合并"的还高

如果扩大样本后仍是「该合并的普遍低于不该合并的」，那 0.92/0.85
这对阈值不是「需要标定」，是**方向错了**。
"""

from __future__ import annotations

import math

from memory.config import load_settings
from memory.services.embedding import embed_texts

EMBED_BATCH = 4

#: (a, b, 是否应合并, 类型说明)
CASES: list[tuple[str, str, bool, str]] = [
    # ---- 应合并：同一实体的不同写法 ----
    ("豆包", "豆包AI", True, "加后缀"),
    ("豆包", "豆包助手", True, "加后缀"),
    ("腾讯", "腾讯公司", True, "加后缀"),
    ("阿里", "阿里巴巴", True, "全称/简称"),
    ("北京", "北京市", True, "地名加后缀"),
    ("周会", "每周例会", True, "同义表达"),
    ("微信", "WeChat", True, "跨语言"),
    ("张三", "张三哥", True, "加称呼"),
    # ---- 不该合并：不同实体但字面极像 ----
    ("张三", "张三丰", False, "单字前缀(不同人)"),
    ("李四", "李四光", False, "单字前缀(不同人)"),
    ("项目A", "项目B", False, "只差一个字母"),
    ("项目A", "项目A组", False, "加后缀但不同实体"),
    ("北京", "南京", False, "同后缀不同地名"),
    ("张三", "李四", False, "完全不同"),
]


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def embed_all(texts: list[str], settings) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(embed_texts(texts[i : i + EMBED_BATCH], settings))
    return out


def main() -> None:
    s = load_settings()
    pool = list(dict.fromkeys([x for c in CASES for x in (c[0], c[1])]))
    print(f"{len(pool)} 个名字 / {len(CASES)} 组对照，模型 = {s.EMBEDDING_MODEL}")
    print()

    vecs = embed_all(pool, s)
    arr = dict(zip(pool, vecs))

    should_merge: list[float] = []
    should_not: list[float] = []

    print(f"{'余弦':>7}  {'应合并':^6}  {'类型':<18} pair")
    print("-" * 72)
    for a, b, merge, kind in CASES:
        sim = cosine(arr[a], arr[b])
        (should_merge if merge else should_not).append(sim)
        mark = "是" if merge else "否"
        print(f"{sim:7.4f}  {mark:^8}  {kind:<18} {a} / {b}")

    print()
    print("=== 分组统计 ===")
    sm = sorted(should_merge)
    sn = sorted(should_not)
    print(f"  应合并 ({len(sm)} 组): min={sm[0]:.4f}  中位={sm[len(sm)//2]:.4f}  max={sm[-1]:.4f}")
    print(f"  不该合并 ({len(sn)} 组): min={sn[0]:.4f}  中位={sn[len(sn)//2]:.4f}  max={sn[-1]:.4f}")
    print()
    print(f"  应合并的最低值  = {sm[0]:.4f}")
    print(f"  不该合并的最高值 = {sn[-1]:.4f}")

    print()
    print("=== 关键问题：两组是否可分？ ===")
    if sm[0] > sn[-1]:
        print(f"  ✓ 完全可分：应合并全部 > {sn[-1]:.4f} > 不该合并全部")
        print(f"    合理阈值区间 ({sn[-1]:.4f}, {sm[0]:.4f})")
    else:
        overlap = [x for x in sm if x <= sn[-1]]
        print(f"  ✗ **不可分**：{len(overlap)}/{len(sm)} 组「应合并」的余弦")
        print(f"    ≤ 不该合并的最高值 {sn[-1]:.4f}")
        print(f"    任何单一阈值都会同时造成误并和误挡：")
        for t in (0.75, 0.80, 0.85, 0.92):
            wrong_merge = sum(1 for x in sn if x >= t)   # 不该合并却被合并
            miss = sum(1 for x in sm if x < t)           # 该合并却没合并
            print(f"      阈值 {t}: 误并 {wrong_merge} 组 / 漏并 {miss} 组")

    print()
    print("=== 对 ADR 阈值 0.92 / 0.85 的检验 ===")
    for t, label in ((0.92, "②直接采用线"), (0.85, "灰区下限")):
        passed_should = sum(1 for x in should_merge if x >= t)
        passed_shouldnt = sum(1 for x in should_not if x >= t)
        print(f"  {label} = {t}:")
        print(f"    应合并的能过线: {passed_should}/{len(should_merge)}")
        print(f"    不该合并的也过线(误并): {passed_shouldnt}/{len(should_not)}")


if __name__ == "__main__":
    main()
