"""账号族（spec 042）：账号 ≠ 主体。

三层模型：

```
(:Account {uid, nick, platform})   ← 账号，硬事实（有 uid）
(:Bot     {name, scope})           ← 主体（bot 实例），群级
(:Person  {name})                  ← 真人
```

关系一律**带证据的弱关联**，且**只加边不并节点**：

- `Account -[:SAME_AS]-> Account`    同名不同 uid 的候选（强）
- `Account -[:INSTANCE_OF]-> Bot`    账号属于某主体（弱）
- `Account -[:OPERATED_BY]-> Person` 账号由谁操作（043 已有）

## 为什么不能自动合并

账号有 uid，是硬事实；「主体」是推断出来的。合并节点不可逆，
而词形族（`xxxx的大肥鱼`）只能说明「可能同一主体」，不能证明。

## 与 041 的关系

归属断言复用 041 的 `Claim` 语义（assert/question/deny 三态 + 同样的升降级规则），
本模块只多两样：**词形族 blocking** 与 **账号层节点/边**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 主体变体的后缀：`xxxx的大肥鱼` / `xxx的ds` / `xxx的鱼`
#:
#: 放在代码里而不是配置：规则只有三条，配置化反而容易漂
#: （改了配置没人知道为什么少了一批候选）。
FAMILY_SUFFIXES = ("的大肥鱼", "的ds", "的鱼", "的bot", "的机器人")

#: 主体名的别名：`大肥鱼` 与 `鱼` 视为同一主体的写法
FAMILY_ALIASES: dict[str, str] = {
    "大肥鱼": "鱼",
    "小鱼": "鱼",
}

#: 同名前缀长度（`智小安业务助手` → `智小安`）
PREFIX_LEN = 3


@dataclass(frozen=True)
class AccountRecord:
    """一条账号记录。"""

    uid: str
    nick: str
    platform: str = "dingtalk"
    namespace: str = ""


@dataclass(frozen=True)
class FamilyCandidate:
    """一个账号族的候选（**不建边**，只出候选）。"""

    family: str
    kind: str              # same_nick / suffix / prefix
    members: tuple[str, ...]
    label: str             # probable / uncertain
    detail: str = ""


@dataclass
class AccountClaim:
    """一条归属/身份断言（复用 041 的三态语义）。"""

    subject_uid: str
    predicate: str         # INSTANCE_OF / OPERATED_BY / SAME_AS
    object_name: str = ""  # 空串 = 空槽（「是谁的鱼」，主体待填）
    mode: str = "assert"   # assert / question / deny
    quote_id: str | None = None
    evidence: list[str] = field(default_factory=list)


def family_of(nick: str) -> str | None:
    """`xxxx的大肥鱼` → `鱼`；不是变体命名返回 None。"""
    text = (nick or "").strip()
    for suffix in FAMILY_SUFFIXES:
        if text.endswith(suffix):
            tail = text[: -len(suffix)]
            core = suffix[1:]  # 去掉「的」
            return FAMILY_ALIASES.get(core, core)
    return None


def find_families(accounts: list[AccountRecord]) -> list[FamilyCandidate]:
    """词形族 blocking：只按**字面形状**分组，不判语义。

    三档强度：

    | 形态 | kind | label |
    |---|---|---|
    | 昵称完全相同、uid 不同 | `same_nick` | `probable` |
    | 昵称是 `xx的<主体>` | `suffix` | `uncertain` |
    | 昵称前 3 字相同 | `prefix` | `uncertain` |
    """
    out: list[FamilyCandidate] = []

    # 1) 同名不同 uid
    by_nick: dict[str, list[AccountRecord]] = {}
    for acc in accounts:
        nick = (acc.nick or "").strip()
        if nick:
            by_nick.setdefault(nick, []).append(acc)
    for nick, group in by_nick.items():
        uids = sorted({a.uid for a in group})
        if len(uids) > 1:
            out.append(FamilyCandidate(
                family=nick, kind="same_nick", members=tuple(uids),
                label="probable",
                detail=f"{len(uids)} 个账号同名，可能是同一主体的多个实例",
            ))

    # 2) `xx的<主体>`
    by_family: dict[str, list[str]] = {}
    for acc in accounts:
        fam = family_of(acc.nick)
        if fam:
            by_family.setdefault(fam, []).append(acc.uid)
    for fam, uids in by_family.items():
        if len(uids) >= 1:
            out.append(FamilyCandidate(
                family=fam, kind="suffix", members=tuple(sorted(uids)),
                label="uncertain", detail="昵称变体，可能是同一主体",
            ))

    # 3) 同前缀
    by_prefix: dict[str, list[str]] = {}
    for acc in accounts:
        nick = (acc.nick or "").strip()
        if len(nick) >= PREFIX_LEN + 1:
            by_prefix.setdefault(nick[:PREFIX_LEN], []).append(acc.uid)
    for prefix, uids in by_prefix.items():
        if len(uids) >= 2:
            out.append(FamilyCandidate(
                family=prefix, kind="prefix", members=tuple(sorted(set(uids))),
                label="uncertain", detail="前缀相同，弱候选",
            ))

    return out


#: 归属断言：`X这个账号是鱼吧？` / `X账号是谁的鱼` / `不是那个鱼`
_OWN_QUESTION = re.compile(r"(?P<subj>[\w\u4e00-\u9fa5]{2,16})\s*这个账号是\s*(?P<obj>[\w\u4e00-\u9fa5]{1,12})\s*吧")
_OWN_EMPTY = re.compile(r"(?P<subj>[\w\u4e00-\u9fa5]{2,16})\s*账号是谁的\s*(?P<obj>[\w\u4e00-\u9fa5]{0,12})")
_OWN_DENY = re.compile(r"不是那个\s*(?P<obj>[\w\u4e00-\u9fa5]{1,12})")


def extract_account_claims(text: str, *, quote_id: str | None = None) -> list[AccountClaim]:
    """从文本抽归属断言（三态）。

    - `X这个账号是鱼吧？` → `question`（**不建边**）
    - `X账号是谁的鱼` → `assert` 但 **object 为空槽**（主体待填，不建边）
    - `不是那个鱼` → `deny`
    """
    body = text or ""
    out: list[AccountClaim] = []

    for m in _OWN_QUESTION.finditer(body):
        out.append(AccountClaim(
            subject_uid="", predicate="INSTANCE_OF",
            object_name=m.group("obj").strip(), mode="question",
            quote_id=quote_id,
        ))

    for m in _OWN_EMPTY.finditer(body):
        obj = (m.group("obj") or "").strip()
        # `是谁的鱼` 里的「鱼」是主体；没有则留空槽
        out.append(AccountClaim(
            subject_uid="", predicate="OPERATED_BY",
            object_name="" if not obj else "",   # 空槽：等后文补
            mode="assert", quote_id=quote_id,
        ))

    for m in _OWN_DENY.finditer(body):
        out.append(AccountClaim(
            subject_uid="", predicate="INSTANCE_OF",
            object_name=m.group("obj").strip(), mode="deny",
            quote_id=quote_id,
        ))

    return out


def claim_can_build_edge(claim: AccountClaim) -> bool:
    """能不能据此建边。

    - `question` → 否（只记证据）
    - 空槽（没有 object 或没有 subject）→ 否（等后文补）
    - 其余 assert/deny → 是
    """
    if claim.mode == "question":
        return False
    if not claim.subject_uid or not claim.object_name:
        return False
    return True


#: 写 SAME_AS（同名不同 uid 的候选）
MERGE_SAME_AS_CYPHER = """
UNWIND $items AS item
MATCH (a:Account {namespace: $namespace, uid: item.a})
MATCH (b:Account {namespace: $namespace, uid: item.b})
MERGE (a)-[r:SAME_AS]->(b)
ON CREATE SET r.created_at = $now, r.evidence = []
SET r.label = CASE WHEN r.label = 'exact' THEN 'exact' ELSE item.label END,
    r.confidence = item.confidence,
    r.evidence = reduce(acc = coalesce(r.evidence, []), x IN item.evidence |
        CASE WHEN x IN acc THEN acc ELSE acc + x END)
RETURN count(r) AS n
"""

#: 写 INSTANCE_OF（账号 → 主体）
MERGE_INSTANCE_CYPHER = """
UNWIND $items AS item
MATCH (a:Account {namespace: $namespace, uid: item.uid})
MERGE (b:Bot {namespace: $namespace, name: item.bot})
ON CREATE SET b.created_at = $now
MERGE (a)-[r:INSTANCE_OF]->(b)
ON CREATE SET r.created_at = $now, r.evidence = []
SET r.label = CASE WHEN r.label = 'exact' THEN 'exact' ELSE item.label END,
    r.confidence = item.confidence
RETURN count(r) AS n
"""

#: 按 tier 查账号族
QUERY_FAMILY_CYPHER = """
MATCH (a:Account {namespace: $namespace})
RETURN a.uid AS uid, a.nick AS nick
"""
