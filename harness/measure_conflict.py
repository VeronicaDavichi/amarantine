"""实测：规则层里有多少「震荡」（多条规则给出矛盾结论）。

现行实现是「首个命中返回」，所以震荡被顺序掩盖了。
这里把每条规则单独跑一遍，看它们是否互相矛盾。
"""

from __future__ import annotations

import itertools

from memory.config import Settings
from memory.services.entity_match import (
    RULING_DIFFERENT,
    RULING_SAME,
    _parse_synonyms,
    _prefix_conflict_excluding_affix,
    _split_csv,
    _strip_honorifics,
    _suffix_expansion,
    match_entities,
)

s = Settings()

#: 覆盖各类情形的对照集
PAIRS = [
    # 地名 + 行政区后缀
    ("上海", "上海市"), ("成都", "成都市"), ("北京", "北京市"),
    ("苏州", "苏州市"), ("杭州", "杭州市"), ("广州", "广州市"),
    # 人名 + 称呼
    ("张三", "张三哥"), ("李四", "李四老师"), ("王五", "王五经理"),
    # 人名 + 真名字用字（应该不同）
    ("张三", "张三丰"), ("李四", "李四光"),
    # 组织 + 后缀
    ("腾讯", "腾讯公司"), ("阿里", "阿里巴巴"),
    ("豆包", "豆包AI"), ("豆包", "豆包助手"),
    # 同义词表里的
    ("周会", "每周例会"), ("月会", "每月例会"), ("腾讯", "Tencent"),
    # 完全无关
    ("张三", "李四"), ("北京", "南京"),
    # 026 决策：组 不算后缀
    ("项目A", "项目A组"), ("部门A", "部门A组"),
    # 单字名
    ("王总", "王"),
]


def votes(a: str, b: str, settings: Settings) -> dict[str, list[str]]:
    """把每条规则单独跑一遍，返回 {结论: [规则名...]}。"""
    out: dict[str, list[str]] = {}
    honorifics = _split_csv(getattr(settings, "ENTITY_HONORIFICS", ""))
    suffixes = _split_csv(getattr(settings, "ENTITY_SUFFIXES", ""))

    # 规则 1：单字名前缀冲突（双向）
    if _prefix_conflict_excluding_affix(
        a, b, honorifics, suffixes
    ) or _prefix_conflict_excluding_affix(b, a, honorifics, suffixes):
        out.setdefault(RULING_DIFFERENT, []).append("prefix")
        # 注意：不加 return，继续跑后面的规则 —— 这就是「投票」和「首个命中」的区别

    if not getattr(settings, "ENTITY_RULE_ENABLED", True):
        return out

    # 规则 2：称呼剥离
    if honorifics:
        sa, ra = _strip_honorifics(a, honorifics)
        sb, rb = _strip_honorifics(b, honorifics)
        if (ra or rb) and sa == sb:
            out.setdefault(RULING_SAME, []).append("honorific")

    # 规则 3：后缀展开
    if suffixes:
        for hit in (_suffix_expansion(a, b, suffixes), _suffix_expansion(b, a, suffixes)):
            if hit:
                out.setdefault(RULING_SAME, []).append("suffix")
                break

    # 规则 4：同义词
    synonyms = _parse_synonyms(getattr(settings, "ENTITY_SYNONYMS", ""))
    if synonyms and frozenset((a, b)) in synonyms:
        out.setdefault(RULING_SAME, []).append("synonym")

    return out


print("=" * 78)
print("规则震荡实测")
print("=" * 78)
conflicts = []
for a, b in PAIRS:
    v = votes(a, b, s)
    ruling = match_entities(a, b, settings=s)
    current = "同" if ruling.is_same else (
        "不同" if ruling.ruling == RULING_DIFFERENT else "未知"
    )
    flag = ""
    if len(v) > 1:
        conflicts.append((a, b, v, current))
        flag = "  <== 震荡"
    detail = " / ".join(f"{k.split('/')[-1]}←{','.join(r)}" for k, r in v.items())
    print(f"  {a:6} vs {b:8}  现行={current:3}  投票=[{detail}]{flag}")

print()
print("=" * 78)
print(f"震荡 {len(conflicts)} / {len(PAIRS)} 组")
print("=" * 78)
for a, b, v, cur in conflicts:
    same = v.get(RULING_SAME, [])
    diff = v.get(RULING_DIFFERENT, [])
    print(f"  {a} / {b}")
    print(f"    说同的: {same}   说不同的: {diff}")
    print(f"    现行按顺序取到: {cur}")
