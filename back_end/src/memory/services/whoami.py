# -*- coding: utf-8 -*-
"""spec 050 延伸：whoami 服务（身份 + 我与别人的关系）。

给两处共用：HTTP `/admin/whoami` 与 MCP `memory_whoami`。
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.request
from typing import Any

logger = logging.getLogger("memory.whoami")

#: 关系层谓词（与 spec 039 一致）
RELATION_TYPES = ("OWNS", "MAINTAINS", "COLLABORATES_WITH", "DEPENDS_ON")


def _neo4j_http(settings: Any) -> str:
    uri = str(settings.NEO4J_URI)
    return uri.replace("bolt://", "http://").replace(":7687", ":7474")


def _cy(settings: Any, statement: str, params: dict | None = None) -> list[list]:
    stmt: dict[str, Any] = {"statement": statement}
    if params:
        stmt["parameters"] = params
    token = base64.b64encode(
        f"{settings.NEO4J_USER}:{settings.NEO4J_PASSWORD}".encode()).decode()
    req = urllib.request.Request(
        f"{_neo4j_http(settings)}/db/neo4j/tx/commit",
        data=json.dumps({"statements": [stmt]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Basic {token}"},
    )
    data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    if data.get("errors"):
        raise RuntimeError(f"Cypher 失败: {data['errors'][:1]}")
    return data["results"][0]["data"]


def relations_of_self(settings: Any, namespace: str, uid: str,
                      *, limit: int = 20) -> tuple[list[dict], str]:
    """我和别人的关系（出边）+ 说明。

    Returns:
        `(relations, reason)`；`reason` 非空表示图不可用/没有关系。
    """
    if not uid:
        return [], "没有解析出本人 uid"
    try:
        rows = _cy(
            settings,
            "MATCH (p:Person {namespace:$ns, key:$key})-[r]->(t) "
            "WHERE type(r) IN $types "
            "RETURN type(r) AS pred, coalesce(t.name,t.uid) AS target, "
            "       coalesce(r.label,'') AS label, coalesce(r.confidence,0.0) AS conf "
            "ORDER BY conf DESC LIMIT $limit",
            {"ns": namespace, "key": f"acct_{uid}", "types": list(RELATION_TYPES),
             "limit": int(limit)},
        )
    except Exception as exc:  # noqa: BLE001 - 图挂了不该让 whoami 失败
        logger.warning("查本人关系失败（降级为空）: %s", exc)
        return [], f"图不可用：{type(exc).__name__}"

    out = [{"predicate": r["row"][0], "target": r["row"][1],
            "label": r["row"][2], "confidence": r["row"][3]} for r in rows]
    if not out:
        return [], "关系层还没有以我为起点的边（陈述通道产量本来就少）"
    return out, ""


def build_whoami(settings: Any, *, namespace: str = "") -> dict:
    """组装「我是谁」：身份 + 我与别人的关系。

    Returns:
        含 `identity`（resolve_self 的结果）与 `relations` / `relations_note`。
    """
    from memory.services.self_identity import resolve_self

    identity = resolve_self(settings, namespace=namespace)
    ns = namespace or identity.get("namespace") or str(
        getattr(settings, "MEMORY_NAMESPACE", "") or "")
    relations, note = relations_of_self(settings, ns, identity.get("uid") or "")
    return {
        "identity": identity,
        "namespace": ns,
        "relations": relations,
        "relations_note": note,
    }
