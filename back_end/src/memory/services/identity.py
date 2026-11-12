"""spec 043：身份来源分层。

一个「人」在图上有两层：

```
(:Account {uid})  --OPERATED_BY-->  (:Person {name})
     ↑ 账号，硬事实，跨群/改名都不变        ↑ 显示名，来自权威来源
```

**取值优先级**（高优先级命中即停止下探）：

```
uid > 备注 > 通讯录真名 > 昵称 > jieba 词形
```

为什么要有这一层：`周大福`/`王老吉` 被 jieba 标成 `nr` 就变成了人（035 修掉了这一半），
但**同一句里的 `周大福` 到底是不是人，词形本身永远答不了** —— 只有账号数据能答。

这个模块只做三件事，不做抽取：

1. 把备注/昵称解析成可用的人名（`王晓虹 - 加班问题人事` → `王晓虹`）
2. 把明显不是汉族姓名的形态挡在 `Person` 之外（REQ-6，暂不解析）
3. 写 `Account` 节点与 `OPERATED_BY` 边（REQ-4/5）
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from memory.core.entity_key import key_for

logger = logging.getLogger(__name__)

#: 备注里的说明分隔符：`王晓虹 - 加班问题人事` / `王晓虹（合规）`
_REMARK_SEPARATORS = (" - ", " — ", "－", "（", "(", "/")

#: 昵称里的公司前缀：`蓝网-韩晓迪` → `韩晓迪`
_NICK_PREFIX_SEPARATORS = ("-", "－", "_")

#: 账号节点：`uid` 是身份，昵称/显示名都是可改的显示字段。
#:
#: `nicks` 用**数组**记历史昵称（Neo4j 属性不支持嵌套 map，但支持基本类型数组）。
#: 改名时不覆盖旧值，只追加 —— 历史昵称仍要能被名册命中，否则旧卡片上的人名
#: 就再也对不上真人了。
ENSURE_IDENTITY_CYPHER = """
MERGE (a:Account {namespace: $namespace, uid: $uid})
ON CREATE SET a.platform = $platform, a.created_at = $now
SET a.nick = $nick,
    a.display_name = $display_name,
    a.nicks = CASE
        WHEN $nick IN coalesce(a.nicks, []) THEN coalesce(a.nicks, [])
        ELSE coalesce(a.nicks, []) + $nick
    END,
    a.last_seen = $now
WITH a
// spec 045：Person 的键用 uid，不随名字变
MERGE (p:Person {namespace: $namespace, key: $key})
ON CREATE SET p.name = $display_name, p.first_seen = $now, p.previous_names = []
// 改名：旧名进 previous_names，名字换成新的。查询侧靠 previous_names 兼容旧名
ON MATCH SET p.previous_names = CASE
        WHEN p.name IS NOT NULL AND p.name <> $display_name
             AND NOT p.name IN coalesce(p.previous_names, [])
        THEN coalesce(p.previous_names, []) + p.name
        ELSE coalesce(p.previous_names, [])
    END,
    p.name = $display_name,
    p.last_seen = $now
SET p:Person
MERGE (a)-[r:OPERATED_BY]->(p)
ON CREATE SET r.created_at = $now, r.source = $source
RETURN a.uid AS uid
"""


@dataclass(frozen=True)
class Identity:
    """一条账号身份记录。字段全是可空的 —— 数据源给不全很常见。"""

    uid: str
    nick: str = ""
    remark: str = ""
    real_name: str = ""
    platform: str = "dingtalk"
    source: str = "account_table"

    @property
    def remark_name(self) -> str:
        return parse_remark(self.remark)[0]

    @property
    def remark_note(self) -> str:
        return parse_remark(self.remark)[1]

    @property
    def display_name(self) -> str:
        """优先级：备注里的姓名 > 通讯录真名 > 去前缀的昵称。"""
        return (
            (self.remark_name or "").strip()
            or (self.real_name or "").strip()
            or clean_nick(self.nick)
        )

    def candidate_names(self) -> list[str]:
        """这个人可能被写成的所有名字（用于名册）。

        备注姓名排最前；昵称同时收**原文**和**去前缀版** —— 群里既有
        `蓝网-韩晓迪` 这种全称，也有 `韩晓迪` 这种简称。
        """
        out: list[str] = []
        raw = [self.remark_name, self.real_name.strip(), clean_nick(self.nick), self.nick.strip()]
        for name in raw:
            name = (name or "").strip()
            if name and name not in out:
                out.append(name)
        # `BOB洪亚军` 这类带修饰的昵称，剥出来的中文名才是能进名册的形态
        kind, core = classify_identity_name(self.nick)
        if kind == KIND_PERSON and core and core not in out:
            out.append(core)
        return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_remark(remark: str) -> tuple[str, str]:
    """`姓名 - 说明` → `(姓名, 说明)`；没有分隔符时整条当姓名。

    分隔符分两类，处理不同：

    - **横线类**（` - ` / ` — ` / `－`）: 说明原样保留 ——
      `韩晓迪 - 反洗钱对接人（外包）` 的括号是说明的一部分，不能削。
    - **括号类**（`（` / `(`）: 说明在括号内，右括号要剥掉。

    >>> parse_remark("王晓虹 - 加班问题人事")
    ('王晓虹', '加班问题人事')
    >>> parse_remark("王晓虹（合规）")
    ('王晓虹', '合规')
    >>> parse_remark("王晓虹")
    ('王晓虹', '')
    """
    text = (remark or "").strip()
    if not text:
        return "", ""

    for sep in (" - ", " — ", "－"):
        if sep in text:
            head, _, tail = text.partition(sep)
            return head.strip(), tail.strip()

    for sep in ("（", "("):
        if sep in text:
            head, _, tail = text.partition(sep)
            tail = tail.strip()
            if tail.endswith("）") or tail.endswith(")"):
                tail = tail[:-1]
            return head.strip(), tail.strip()

    if "/" in text:
        head, _, tail = text.partition("/")
        return head.strip(), tail.strip()

    return text, ""


def clean_nick(nick: str) -> str:
    """去掉 `蓝网-韩晓迪` 这类公司前缀，取最后一段（太短则不动）。"""
    text = (nick or "").strip()
    for sep in _NICK_PREFIX_SEPARATORS:
        if sep in text:
            tail = text.split(sep)[-1].strip()
            if len(tail) >= 2:
                return tail
    return text


#: 服务号 / 机器人昵称里的标记词。命中即判定「这不是一个人」。
#:
#: 为什么单列一类：`钉钉小秘书`、`工资条管家`、`AI助理` 与「汉族姓名」,
#: 本质是**两种不同的判断**。把它们混进「非汉族姓名」里，
#: 就会顺带把 `BOB洪亚军`、`Gavin` 这类真人英文昵称一起扫出去。
_SERVICE_MARKERS = (
    "助手", "机器人", "小秘书", "管家", "工资条", "应用中心", "文档助手",
    "听记", "智能助理", "业务助手", "小钉", "小智", "助理",
)

#: 身份分类结果。用字符串而不是布尔 —— 报告要能说清「为什么」。
KIND_PERSON = "person"            # 可用的人名（可能带了修饰，已剥干净）
KIND_SERVICE = "service"          # 服务号 / 机器人，不是人
KIND_ETHNIC = "ethnic_name"       # 少数民族姓名，暂不解析
KIND_PLACEHOLDER = "placeholder"  # 纯占位/无意义昵称，不入名册

_HAN_LO = "\u4e00"
_HAN_HI = "\u9fa5"


def _is_han(ch: str) -> bool:
    return _HAN_LO <= ch <= _HAN_HI


def strip_decorations(name: str) -> str:
    """剥掉昵称里的人名前缀/后缀修饰，尽量还原出中文姓名。

    实测样本：`BOB洪亚军` → `洪亚军`、`王政博Yves` → `王政博`、
    `22传32马雯卓` → `马雯卓`；剥不出汉字的原样返回。

    只剥**首尾**的修饰，中间的汉字保留 —— 它们才是姓名本体。
    """
    text = (name or "").strip()
    if not text:
        return ""
    # 取**最长**的连续汉字段：`22传32马雯卓` 里 `传` 和 `马雯卓` 两段，
    # 长的那段才是姓名本体。只剥首尾会漏掉夹在中间的修饰。
    runs = re.findall(r"[\u4e00-\u9fa5]+", text)
    if not runs:
        return text
    return max(runs, key=len)


def classify_identity_name(name: str) -> tuple[str, str]:
    """判断一个昵称/备注该归哪一类，返回 `(kind, 处理后的名字)`。

    四类（spec 043 A6 返工）：

    - ``service``       `钉钉小秘书` `AI助理` `工资条管家` —— 不入名册，不是人
    - ``ethnic_name``   `阿卜杜拉·买买提` —— 不入名册，暂不解析
    - ``placeholder``   `aaa` `123` —— 不入名册，无信息量
    - ``person``        `王泳`；`BOB洪亚军`→`洪亚军`；`Gavin` —— 入名册

    **与旧版的关键差别**：旧版把「含字母」等同于「非汉族」，于是
    `BOB洪亚军`、`王政博Yves`、`Gavin` 全被当成少数民族姓名挡掉 ——
    8 个真人因此失去了成为 `Person` 的资格。新顺序：

    1. 先看是不是**服务号**（标记词 → 不是人）
    2. 再看能不能**剥出中文姓名**（`BOB洪亚军` → `洪亚军`，是真人）
    3. 纯英文昵称**默认是真人**（`Gavin` 确实可能是同事）
    4. 只有「含 `·`」或「≥5 字且首字非百家姓」才算少数民族姓名
    """
    from memory.services.labeler import _COMMON_SURNAMES, COMPOUND_SURNAMES

    text = (name or "").strip()
    if not text:
        return KIND_PLACEHOLDER, ""

    # 1) 服务号 / 机器人：按标记词判，不看字符构成
    if any(marker in text for marker in _SERVICE_MARKERS):
        return KIND_SERVICE, text

    # 1.5) 家长号 / 职务号：`五一班和子轩同学家长` 是真人，只是昵称长
    if any(w in text for w in ("家长", "爸爸", "妈妈", "妈妈", "同学家长")):
        return KIND_PERSON, strip_decorations(text)

    # 2) 少数民族姓名：`·` 分隔，或 ≥5 个汉字且首字非百家姓（汉族姓名最多 4 字）
    if "·" in text or "•" in text:
        return KIND_ETHNIC, text
    han_only = "".join(c for c in text if _is_han(c))
    if len(text) == len(han_only) and len(han_only) >= 5:
        if han_only[0] not in _COMMON_SURNAMES and han_only[:2] not in COMPOUND_SURNAMES:
            return KIND_ETHNIC, text

    # 3) 剥首尾修饰，能还原出中文姓名就是真人
    core = strip_decorations(text)
    if core != text and len(core) >= 2 and all(_is_han(c) for c in core):
        return KIND_PERSON, core

    # 4) 纯英文/数字昵称：默认真人，明显无信息量的才丢
    if not han_only:
        letters = "".join(c for c in text if c.isalpha())
        if len(letters) < 2 or len(set(letters.lower())) <= 2:
            return KIND_PLACEHOLDER, text
        return KIND_PERSON, text

    # 5) 其余含汉字但没剥出纯中文名的，原样当人
    if len(han_only) >= 2:
        return KIND_PERSON, text
    return KIND_PLACEHOLDER, text


def is_non_han(name: str) -> bool:
    """向后兼容的布尔壳：**只有少数民族姓名**返回 True。

    `service` / `placeholder` 不算「非汉族姓名」—— 它们不是人，也不是姓名问题。
    新代码请直接用 `classify_identity_name`，它能说清是哪一类。
    """
    return classify_identity_name(name)[0] == KIND_ETHNIC


def split_roster(
    items: Iterable[Any] | None,
) -> tuple[list[str], list[Identity], list[dict[str, str]]]:
    """把 `IngestRequest.roster` 归一成（名字列表, 身份记录, 被挡掉的记录）。

    兼容两种入参（REQ-3 向后兼容）：

    - `["王泳", "韩晓迪"]`          —— 老格式，纯名字
    - `[{"uid": "1", "nick": "王泳"}]` —— 新格式，带账号信息

    **挡掉的东西带原因**（spec 043 A6 返工）：返回的不是裸名字列表，而是
    `[{"name": ..., "kind": "service|ethnic_name|placeholder"}]` ——
    「为什么没进名册」必须能回答，否则报告没法复核。
    """
    names: list[str] = []
    identities: list[Identity] = []
    rejected: list[dict[str, str]] = []

    for item in items or []:
        if isinstance(item, str):
            _add_name(item, names, rejected)
            continue
        if isinstance(item, dict):
            uid = str(item.get("uid") or "").strip()
            ident = Identity(
                uid=uid,
                nick=str(item.get("nick") or ""),
                remark=str(item.get("remark") or ""),
                real_name=str(item.get("real_name") or ""),
                platform=str(item.get("platform") or "dingtalk"),
                source=str(item.get("source") or "account_table"),
            )
            identities.append(ident)
            for name in ident.candidate_names():
                _add_name(name, names, rejected)
    return names, identities, rejected


def _add_name(name: str, names: list[str], rejected: list[dict[str, str]]) -> None:
    text = (name or "").strip()
    if len(text) < 2:
        return  # 单字会误伤大量普通词（035 的既有约束）

    kind, core = classify_identity_name(text)
    if kind != KIND_PERSON:
        entry = {"name": text, "kind": kind}
        if entry not in rejected:
            rejected.append(entry)
        return
    # 剥出来的中文名（`洪亚军`）连同原文一起收：群里可能两种叫法都有
    for candidate in (core, text):
        if candidate and len(candidate) >= 2 and candidate not in names:
            names.append(candidate)


def ensure_identities(
    *,
    namespace: str,
    identities: Iterable[Identity],
    settings: Any,
    driver: Any | None = None,
) -> dict[str, int]:
    """写 `Account` 节点与 `OPERATED_BY` 边（幂等）。

    **失败只告警，不抛**：图是派生层（ADR D2），账号写入失败不能让整批 ingest 失败 ——
    和事件卡写图的容错口径一致。

    Returns:
        `{"accounts": n, "linked": n, "failed": n}`
    """
    result = {"accounts": 0, "linked": 0, "failed": 0}
    idents = [i for i in identities if i.uid and i.display_name]
    if not idents:
        return result

    owns = driver is None
    drv = driver
    try:
        if drv is None:
            from neo4j import GraphDatabase

            drv = GraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            )
        now = _now()
        with drv.session() as session:
            for ident in idents:
                name = ident.display_name
                try:
                    session.run(
                        ENSURE_IDENTITY_CYPHER,
                        namespace=namespace,
                        uid=ident.uid,
                        nick=ident.nick,
                        display_name=name,
                        key=f"uid:{ident.uid}",
                        platform=ident.platform,
                        source=ident.source,
                        now=now,
                    ).consume()
                    result["accounts"] += 1
                    result["linked"] += 1
                    _register_identity_names(ident, name)
                except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                    result["failed"] += 1
                    logger.warning("账号写入失败 | uid=%s name=%s | %s", ident.uid, name, exc)
    except Exception as exc:  # noqa: BLE001 - 图不可用不影响写入主链路
        logger.warning("账号层不可用，跳过 | %s", exc)
        result["failed"] += len(idents)
    finally:
        if owns and drv is not None:
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                pass
    return result


def _register_identity_names(ident: Identity, display_name: str) -> None:
    """把「这个名字属于这个 uid」登记进进程级表（spec 045）。

    之后同批次的卡片写入会走 `resolve_person_key(name)` → ``uid:<uid>``，
    不会再用名字键另起一个节点。
    """
    from memory.services.person_key import register_name_uid

    mapping = {name: ident.uid for name in ident.candidate_names()}
    mapping[display_name] = ident.uid
    register_name_uid(mapping)


def to_identity_payload(identities: Iterable[Identity]) -> list[dict[str, str]]:
    """身份记录 → 可 JSON 化的列表（harness 脚本与响应复用）。"""
    return [
        {
            "uid": i.uid,
            "nick": i.nick,
            "remark": i.remark,
            "real_name": i.real_name,
            "platform": i.platform,
            "display_name": i.display_name,
        }
        for i in identities
    ]


def from_json_file(path: str) -> list[Identity]:
    """读 `harness/export_identity.py` 产出的身份文件。"""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("identities") if isinstance(data, dict) else data
    out: list[Identity] = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("uid"):
            continue
        out.append(
            Identity(
                uid=str(row["uid"]),
                nick=str(row.get("nick") or ""),
                remark=str(row.get("remark") or ""),
                real_name=str(row.get("real_name") or ""),
                platform=str(row.get("platform") or "dingtalk"),
                source=str(row.get("source") or "identity_file"),
            )
        )
    return out

# ============================================================ spec 047：名字标记

#: 写 `(:Account)-[:DISPLAYED_AS {scope}]->(:Alias)`
#:
#: 名字标记（昵称/群名片/备注）都是**同一个人身上的标记**，差别只在 scope。
#: 同一 uid 在不同群有不同名片 → 多条边共存，互不覆盖（REQ-4）。
ENSURE_DISPLAY_NAME_CYPHER = """
UNWIND $items AS item
MERGE (a:Account {namespace: $namespace, uid: item.uid})
ON CREATE SET a.created_at = $now
SET a.last_seen = $now
WITH a, item
MERGE (al:Alias {namespace: $namespace, name: item.name})
ON CREATE SET al.first_seen = $now
MERGE (a)-[r:DISPLAYED_AS {scope: $scope}]->(al)
ON CREATE SET r.created_at = $now
SET r.source = item.source, r.updated_at = $now
RETURN count(r) AS n
"""

#: 按名字 + scope 反查账号（whois）
WHOIS_CYPHER = """
MATCH (a:Account {namespace: $namespace})-[r:DISPLAYED_AS {scope: $scope}]->(al:Alias {name: $name})
RETURN a.uid AS uid, a.nick AS nick, r.source AS source
ORDER BY a.uid
"""



#: 某 scope 下的全部名字标记（`base=true` 的 whois 用，Python 侧按基础名过滤）
WHOIS_SCOPE_CYPHER = """
MATCH (a:Account {namespace: $namespace})-[r:DISPLAYED_AS {scope: $scope}]->(al:Alias)
RETURN a.uid AS uid, al.name AS name, r.source AS source
ORDER BY a.uid
"""

def ensure_display_names(
    *,
    namespace: str,
    scope: str,
    records: list[dict],
    settings: Any,
    driver: Any | None = None,
) -> dict[str, int]:
    """写名字标记（幂等）。失败只告警 —— 图是派生层。"""
    items = [
        {"uid": str(r.get("uid") or "").strip(), "name": str(r.get("name") or "").strip(),
         "source": str(r.get("source") or "group_card")}
        for r in records
        if str(r.get("uid") or "").strip() and str(r.get("name") or "").strip()
    ]
    if not items:
        return {"written": 0, "failed": 0}

    owns = driver is None
    drv = driver
    written = 0
    try:
        if drv is None:
            from neo4j import GraphDatabase

            drv = GraphDatabase.driver(
                settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
            )
        with drv.session() as session:
            res = session.run(
                ENSURE_DISPLAY_NAME_CYPHER,
                namespace=namespace, scope=scope, items=items, now=_now(),
            ).single()
            written = int(res["n"]) if res else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("写名字标记失败（已降级）| %s", exc)
        return {"written": 0, "failed": len(items)}
    finally:
        if owns and drv is not None:
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                pass
    return {"written": written, "failed": 0}

