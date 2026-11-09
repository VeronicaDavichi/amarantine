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
MERGE (p:Person {namespace: $namespace, key: $key})
ON CREATE SET p.name = $display_name, p.first_seen = $now
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
        for name in (self.remark_name, self.real_name.strip(), clean_nick(self.nick), self.nick.strip()):
            name = (name or "").strip()
            if name and name not in out:
                out.append(name)
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


def is_non_han(name: str) -> bool:
    """是不是「明显不像汉族姓名」的形态（REQ-6：只检出，不解析）。

    三条判据（满足任一）：

    1. 含 `·`（`阿卜杜拉·买买提`）
    2. 去掉 `-`/`_`/空格这类分隔符后，仍有非汉字字符（字母、数字、其他符号）
    3. 长度 ≥ 5 且首字不在百家姓里

    **分隔符先剥掉再判**：`蓝网-刘佳` 是「公司-姓名」的昵称写法，
    不是少数民族姓名；不剥的话它会因为一个连字符被误挡。

    **盲区（已知）**：`买买提江` 这种「4 字、全汉字、首字非百家姓」的形态
    判不出来 —— 汉族姓名最多 4 字（复姓 + 双名），长度门槛只能设在 5。
    宁可漏，也不能把复姓名误伤成非汉族。
    """
    from memory.services.labeler import _COMMON_SURNAMES, COMPOUND_SURNAMES

    text = (name or "").strip()
    if not text:
        return True
    if "·" in text or "•" in text:
        return True

    stripped = text.replace("-", "").replace("_", "").replace(" ", "")
    if not all("\u4e00" <= c <= "\u9fa5" for c in stripped):
        return True

    if len(stripped) >= 5:
        first = stripped[0]
        if first not in _COMMON_SURNAMES and stripped[:2] not in COMPOUND_SURNAMES:
            return True
    return False


def split_roster(items: Iterable[Any] | None) -> tuple[list[str], list[Identity], list[str]]:
    """把 `IngestRequest.roster` 归一成（名字列表, 身份记录, 被挡掉的名字）。

    兼容两种入参（REQ-3 向后兼容）：

    - `["王泳", "韩晓迪"]`          —— 老格式，纯名字
    - `[{"uid": "1", "nick": "王泳"}]` —— 新格式，带账号信息

    **非汉族姓名在这里就被挡掉**：不进名册、不建 `Person`，
    但会原样返回给调用方写报告（REQ-6）。
    """
    names: list[str] = []
    identities: list[Identity] = []
    rejected: list[str] = []

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


def _add_name(name: str, names: list[str], rejected: list[str]) -> None:
    text = (name or "").strip()
    if len(text) < 2:
        return  # 单字会误伤大量普通词（035 的既有约束）
    if is_non_han(text):
        if text not in rejected:
            rejected.append(text)
        return
    if text not in names:
        names.append(text)


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
                        key=key_for(name),
                        platform=ident.platform,
                        source=ident.source,
                        now=now,
                    ).consume()
                    result["accounts"] += 1
                    result["linked"] += 1
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
