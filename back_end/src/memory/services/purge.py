# -*- coding: utf-8 -*-
"""spec 050：命名空间清理（把某 namespace 的三处数据一起删掉）。

## 为什么需要

导入的聊天记录散在三处：SQLite 原文 / Weaviate 卡片 / Neo4j 图。
想「删掉某个域」现在只能手写三种语句 —— 实测已经手写过三次。
更关键的是隐私：**「删了」必须三处都删**，只删一处会留下对不上的残片。

## 安全约束

1. 必须显式确认语 `purge <namespace>`，写错就拒
2. 默认 dry-run，只有 `apply=True` 才动数据
3. 只接受**字面 namespace**（拒绝空串、`*`、含 `,`）
4. 删序固定：图 → 卡片 → SQLite（先拆派生层，再删事实源）
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("memory.purge")

#: 带 `namespace` 列的 SQLite 表都要删（漏一张就留残片）
SQLITE_TABLES = (
    "raw_quote",
    "ingest_job",
    "alias_reject",
    "entity_suppress",
    "recall_trace",
    "recall_feedback",
    "recall_stats",
    "label_review",
    "pending_task",
    "directive_usage",
    "vocab_registry",
    "embedding_index_meta",
)

#: 关系/索引类边：删图时按类型逐类删（`DETACH DELETE` 节点也会带走，这些只为计数）
GRAPH_EDGE_TYPES = (
    "DISPLAYED_AS", "RELATED", "AT", "MENTIONS", "INVOLVES", "ACTION",
    "TITLED", "OWNS", "MAINTAINS", "COLLABORATES_WITH", "DEPENDS_ON",
    "HAS_SENSE", "OPERATED_BY", "ALIAS_OF", "SAME_SENSE", "RESOLVES",
)


class PurgeError(ValueError):
    """参数不合法（确认语错误 / namespace 非法）。"""


def confirm_phrase(namespace: str) -> str:
    return f"purge {namespace}"


def _check_namespace(namespace: str) -> str:
    ns = (namespace or "").strip()
    if not ns:
        raise PurgeError("namespace 不能为空")
    if any(ch in ns for ch in ("*", ",", " ")):
        raise PurgeError(f"namespace 必须是字面量（不接受通配/列表）：{ns!r}")
    return ns


def _require_confirm(namespace: str, confirm: str) -> None:
    want = confirm_phrase(namespace)
    if (confirm or "").strip() != want:
        raise PurgeError(f"确认语不匹配：应传 {want!r}")


def _cy(settings: Any, statement: str, params: dict | None = None) -> list[list]:
    stmt: dict[str, Any] = {"statement": statement}
    if params:
        stmt["parameters"] = params
    body = json.dumps({"statements": [stmt]}).encode()
    token = base64.b64encode(
        f"{settings.NEO4J_USER}:{settings.NEO4J_PASSWORD}".encode()).decode()
    req = urllib.request.Request(
        str(settings.NEO4J_URI).replace("bolt://", "http://").replace(":7687", ":7474")
        + "/db/neo4j/tx/commit",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {token}"},
    )
    data = json.loads(urllib.request.urlopen(req, timeout=180).read())
    if data.get("errors"):
        raise RuntimeError(f"Cypher 失败: {data['errors'][:1]}")
    return data["results"][0]["data"]


def _gql(settings: Any, query: str) -> dict:
    req = urllib.request.Request(
        f"{settings.WEAVIATE_URL}/v1/graphql",
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=180).read())


#: `back_end/` 目录（`purge.py` → services → memory → src → back_end）
BACK_END_DIR = Path(__file__).resolve().parents[3]


def _db_path(settings: Any) -> Path:
    """从 DATABASE_URL 解析 SQLite 路径。

    **相对路径要相对 `back_end/` 解析**：`.env.example` 里写的是
    `sqlite:///./data/memory.db`，按当前工作目录解析会跑到
    `harness/data/` 之类的地方去（实测报 `unable to open database file`）。
    """
    url = str(settings.DATABASE_URL)
    if not url.startswith("sqlite"):
        raise PurgeError(f"只支持 SQLite：{url}")
    raw = url.split("///", 1)[-1]
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BACK_END_DIR / path
    return path.resolve()


def count_namespace(settings: Any, namespace: str) -> dict[str, int]:
    """数该 namespace 现在有多少（dry-run 与删后复核都用它）。"""
    ns = _check_namespace(namespace)
    out: dict[str, int] = {}

    q = ('{ Aggregate { MemoryEvent(where: {path:["namespace"], operator: Equal, '
         'valueText: "' + ns + '"}) { meta { count } } } }')
    rows = (((_gql(settings, q).get("data") or {}).get("Aggregate") or {})
            .get("MemoryEvent") or [])
    out["cards"] = int(rows[0]["meta"]["count"]) if rows else 0

    out["graph_nodes"] = int(_cy(
        settings, "MATCH (n {namespace:$ns}) RETURN count(n) AS c", {"ns": ns}
    )[0]["row"][0] or 0)
    out["graph_edges"] = int(_cy(
        settings,
        "MATCH (a {namespace:$ns})-[r]->() RETURN count(r) AS c", {"ns": ns}
    )[0]["row"][0] or 0)

    db = _db_path(settings)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for table in SQLITE_TABLES:
            try:
                cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                continue
            if "namespace" not in cols:
                continue
            out[table] = int(con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE namespace=?", (ns,)).fetchone()[0])
    finally:
        con.close()
    return out


def list_namespaces(settings: Any) -> list[dict]:
    """列出所有 namespace 与卡片数（先看清有什么，再决定删哪个）。"""
    q = ('{ Aggregate { MemoryEvent(groupBy:["namespace"]) '
         '{ groupedBy { value } meta { count } } } }')
    rows = (((_gql(settings, q).get("data") or {}).get("Aggregate") or {})
            .get("MemoryEvent") or [])
    return [{"namespace": r["groupedBy"]["value"], "cards": int(r["meta"]["count"])}
            for r in rows]


def purge_namespace(
    settings: Any,
    namespace: str,
    *,
    confirm: str,
    apply: bool = False,
) -> dict[str, Any]:
    """删掉某 namespace 的三处数据。

    Args:
        settings: 配置（三处连接的来源）。
        namespace: 字面 namespace。
        confirm: 必须是 `purge <namespace>`；**仅 `apply=True` 时校验**。
        apply: False 时只数不改（默认）。

    Returns:
        `{"namespace", "applied", "before", "deleted", "after", "ok"}`。

    Raises:
        PurgeError: namespace 或确认语不合法。
    """
    ns = _check_namespace(namespace)
    # **只有真删才要确认语**：预演不改数据，`看看会删多少` 不该被确认语挡住
    if apply:
        _require_confirm(ns, confirm)

    before = count_namespace(settings, ns)
    if not apply:
        return {"namespace": ns, "applied": False, "before": before,
                "deleted": {k: 0 for k in before}, "after": before, "ok": True}

    deleted: dict[str, int] = {}

    # 1) 图先删（派生层）
    res = _cy(settings, "MATCH (n {namespace:$ns}) DETACH DELETE n "
                        "RETURN 1 AS done", {"ns": ns})
    deleted["graph_nodes"] = before.get("graph_nodes", 0)
    logger.info("purge %s: 图节点 %s 已删（%s）", ns, deleted["graph_nodes"],
                "ok" if res else "?")

    # 2) 卡片（逐页删，Weaviate 单次删除不支持按条件批量）
    db = _db_path(settings)
    removed = 0
    while True:
        q = ('{ Get { MemoryEvent(where: {path:["namespace"], operator: Equal, '
             'valueText: "' + ns + '"}, limit: 200) { _additional { id } } } }')
        rows = (((_gql(settings, q).get("data") or {}).get("Get") or {})
                .get("MemoryEvent") or [])
        if not rows:
            break
        for row in rows:
            oid = (row.get("_additional") or {}).get("id")
            if not oid:
                continue
            req = urllib.request.Request(
                f"{settings.WEAVIATE_URL}/v1/objects/MemoryEvent/{oid}",
                method="DELETE")
            urllib.request.urlopen(req, timeout=60)
            removed += 1
    deleted["cards"] = removed

    # 3) SQLite（事实源最后删）
    con = sqlite3.connect(db, timeout=60)
    try:
        for table in SQLITE_TABLES:
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if "namespace" not in cols:
                continue
            cur = con.execute(f"DELETE FROM {table} WHERE namespace=?", (ns,))
            deleted[table] = cur.rowcount or 0
        con.commit()
    finally:
        con.close()

    after = count_namespace(settings, ns)
    nonzero = {k: v for k, v in after.items() if v}
    return {
        "namespace": ns,
        "applied": True,
        "before": before,
        "deleted": deleted,
        "after": after,
        "ok": not nonzero,
        "residual": nonzero,
    }
