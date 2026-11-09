"""Person 的身份键（spec 045）。

## 为什么不能再按名字派生

`key_for(name)` 把「显示名」当身份用，于是：

- 同一个人改名 → 键变了 → 图上多一个 `Person`，旧卡片挂旧的
- 两个不同的人同名 → 键相同 → 合并成一个

043 引入 `Account(uid)` 之后有了更稳的锚：**账号不随名字变**。
所以键改成 ``uid:<uid>``，名字退化成可改的显示字段。

## 三层解析

```
resolve_person_key(name, uid):
  1. 显式给了 uid            → "uid:<uid>"
  2. 本进程登记过 name → uid  → "uid:<uid>"
  3. 都没有                   → key_for(name)（无 uid 场景，行为不变）
```

第 2 步是给「只知道名字」的调用点用的 —— 别名建边（`ALIAS_OF` 只有
canonical 名字）就是典型。没有它，别名路径会用名字键再建一个节点，
迁移后立刻又分裂。

登记表是**进程级**的，和 `labeler._ROSTER` 同一种取舍：换取零 IO，
代价是每个 ingest 请求必须在使用前登记（`admin_http` 已经在做名册注册）。
"""

from __future__ import annotations

import logging

from memory.core.entity_key import key_for

logger = logging.getLogger(__name__)

#: uid 键前缀。图上肉眼可分：`uid:53660559` vs `e_ab12cd34ef56`
UID_KEY_PREFIX = "uid:"

#: 名字 → uid。进程级，随 ingest 请求登记（见模块文档第 2 步）
_NAME_TO_UID: dict[str, str] = {}


def register_name_uid(mapping: dict[str, str] | None) -> int:
    """登记「名字 → uid」。返回新增条目数（幂等）。"""
    if not mapping:
        return 0
    added = 0
    for name, uid in mapping.items():
        key = (name or "").strip()
        value = (uid or "").strip()
        if len(key) < 2 or not value:
            continue
        if _NAME_TO_UID.get(key) != value:
            _NAME_TO_UID[key] = value
            added += 1
    return added


def uid_for(name: str) -> str | None:
    """按名字查已登记的 uid（没有就 None）。"""
    return _NAME_TO_UID.get((name or "").strip())


def reset_registry() -> None:
    """清空登记表（测试用）。"""
    _NAME_TO_UID.clear()


def resolve_person_key(name: str, uid: str | None = None) -> str:
    """算出这个人的身份键。

    Args:
        name: 显示名（`王泳`）。
        uid: 账号 id。调用方知道就传，不知道就靠登记表。

    Returns:
        ``uid:<uid>`` 或 `key_for(name)`。

    Note:
        **不要在这里查图**。这个函数跑在每张卡的每个实体上，
        每次查图会让写入路径多几十次往返。需要「复用图上已有键」的场景
        （迁移后的老节点）由 `ensure_identities` 在写入时补一次
        `previous_names`，查询侧靠名字匹配，不依赖键。
    """
    text = (name or "").strip()
    explicit = (uid or "").strip()
    if explicit:
        return f"{UID_KEY_PREFIX}{explicit}"
    mapped = uid_for(text)
    if mapped:
        return f"{UID_KEY_PREFIX}{mapped}"
    return key_for(text)
