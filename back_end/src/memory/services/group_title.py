"""QQ 群头衔解析（spec 047 扩展）。

## 为什么单独做

群头衔是 QQ 独有的**角色标记**，标志性极强：

```
恭喜我是给AI大人散热的工程师，智械了别杀我获得群主授予的机仆头衔
恭喜羽生玲央获得群主授予的羽生头衔
恭喜棉棉获得群主授予的妈妈桑头衔
恭喜素笺获得群主授予的莫比乌斯车的头衔
```

实测群 `485490399` 有 **24 条**这类系统公告。

## 用途

- **消歧**：同名两个号，头衔不同 → 大概率不是同一个人
- **角色/关系**：机仆 / 妈妈桑 这类称号本身是社交信号
- **补充名字标记**：头衔里常含名字（`羽生` 对应 `羽生玲央`）

## 拿不全的现实

MCP 的 `list_group_members` **不返回头衔**；`group_info.db` 自行解密后
`page_count=0`（工具内部能读，但不暴露成员头衔列）。

所以目前只能从**系统公告**里解析「谁被授予了什么头衔」——
历史存量头衔（入群时就有、没公告过的）拿不到。这条限制写进 spec。
"""

from __future__ import annotations

import re

#: `恭喜X获得群主授予的Y头衔` / `恭喜X获得管理员授予的Y头衔`
_TITLE_NOTICE_RE = re.compile(
    r"恭喜(?P<who>.+?)(?:获得|被授予)(?:群主|管理员|管理)?(?:授予的)?(?P<title>.+?)头衔"
)

#: 头衔相关的其它系统文案（只用于识别，不解析细节）
_TITLE_MARKERS = ("头衔",)


def parse_title_notice(text: str) -> tuple[str, str] | None:
    """从一条系统公告里解析 `(成员显示名, 头衔)`。

    Args:
        text: 消息原文，如 `恭喜棉棉获得群主授予的妈妈桑头衔`。

    Returns:
        `(who, title)`；不是头衔公告返回 None。

    Note:
        名字用**显示名**（群名片优先），调用方要再拿成员表把它映射成 uid。
        头衔里可能带「的」（`莫比乌斯车的`），正则用非贪婪到「头衔」为止。
    """
    body = (text or "").strip()
    if not body or not any(m in body for m in _TITLE_MARKERS):
        return None
    m = _TITLE_NOTICE_RE.search(body)
    if not m:
        return None
    who = m.group("who").strip(" ，,：:")
    title = m.group("title").strip(" ，,：:的")
    if not who or not title:
        return None
    return who, title


def collect_title_notices(texts: list[str]) -> list[tuple[str, str]]:
    """批量解析并按 `(who, title)` 去重（语料里同一条公告会重复出现）。"""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for text in texts:
        parsed = parse_title_notice(text)
        if parsed and parsed not in seen:
            seen.add(parsed)
            out.append(parsed)
    return out
