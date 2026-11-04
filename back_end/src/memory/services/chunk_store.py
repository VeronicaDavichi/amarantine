"""A16：chunk 向量层（独立 collection，不动 `MemoryEvent`）。

**为什么单独一个 collection，而不是往 `MemoryEvent` 里塞 `is_chunk=true`**：

现有代码里到处都是「`list_all` 拿全部卡」的假设 —— 计数、软删、归档、
`check_rebuild.py`、`forget` 扫描。往同一个 collection 混入 chunk 会让
**每一处**都得加 `is_chunk == false` 过滤，漏掉一处就静默算错。

独立 collection 的代价是「检索要多查一次」，收益是**现有路径一行不用改**。
对一个已经跑起来的系统，后者重要得多。

开关是 `CHUNK_ENABLED`，默认 **false**：不开时写入路径完全不变，
连这个模块都不会被 import。所以上线零风险。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from memory.config import Settings
from memory.services.weaviate_store import deterministic_uuid

logger = logging.getLogger(__name__)

CHUNK_COLLECTION = "MemoryChunk"

CHUNK_SCHEMA_PROPERTIES: list[dict[str, Any]] = [
    {"name": "namespace", "dataType": ["text"]},
    #: 父卡的 `event_id` —— 聚合与去重都靠它
    {"name": "event_id", "dataType": ["text"]},
    {"name": "chunk_index", "dataType": ["int"]},
    {"name": "text", "dataType": ["text"]},
    {"name": "date_start", "dataType": ["text"]},
    {"name": "is_deleted", "dataType": ["boolean"]},
]


class ChunkStoreError(RuntimeError):
    """chunk 层不可用。"""


def chunk_uid(namespace: str, event_id: str, index: int) -> str:
    """chunk 对象的主键。带上 index 才能一卡多块。"""
    return deterministic_uuid(namespace, f"{event_id}#chunk{index}")


class ChunkStore:
    """`MemoryChunk` collection 的读写。接口风格对齐 `WeaviateStore`。"""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._base = settings.WEAVIATE_URL.rstrip("/")
        self._http = client or httpx.Client(timeout=20.0)
        self._ready = False

    # ---------- 生命周期 ----------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ChunkStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def ping(self) -> None:
        try:
            resp = self._http.get(f"{self._base}/v1/.well-known/ready")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ChunkStoreError(f"Weaviate 不可达: {exc}") from exc

    def ensure_collection(self) -> None:
        """不存在则建；已存在则补齐缺失属性（与 `ensure_collection` 同策略）。"""
        if self._ready:
            return
        self.ping()
        try:
            resp = self._http.get(f"{self._base}/v1/schema")
            resp.raise_for_status()
            classes = {c.get("class"): c for c in resp.json().get("classes", [])}
        except httpx.HTTPError as exc:
            raise ChunkStoreError(f"读取 schema 失败: {exc}") from exc

        if CHUNK_COLLECTION not in classes:
            body = {
                "class": CHUNK_COLLECTION,
                "vectorizer": "none",
                "properties": CHUNK_SCHEMA_PROPERTIES,
            }
            try:
                self._http.post(f"{self._base}/v1/schema", json=body).raise_for_status()
            except httpx.HTTPError as exc:
                raise ChunkStoreError(f"创建 {CHUNK_COLLECTION} 失败: {exc}") from exc
        else:
            existing = {
                p.get("name") for p in classes[CHUNK_COLLECTION].get("properties", [])
            }
            for prop in CHUNK_SCHEMA_PROPERTIES:
                if prop["name"] in existing:
                    continue
                try:
                    self._http.post(
                        f"{self._base}/v1/schema/{CHUNK_COLLECTION}/properties",
                        json=prop,
                    ).raise_for_status()
                except httpx.HTTPError as exc:
                    raise ChunkStoreError(
                        f"补属性 {prop['name']} 失败: {exc}"
                    ) from exc
        self._ready = True

    # ---------- 写 ----------

    def insert_chunks(
        self,
        namespace: str,
        event_id: str,
        chunks: list[Any],
        vectors: list[list[float]],
        *,
        date_start: str | None = None,
    ) -> int:
        """写入一条卡的 chunk 向量。返回写入条数。

        `chunks` 是 `core.chunker.Chunk`，只用到 `index` / `text`。
        """
        if len(chunks) != len(vectors):
            raise ValueError(f"chunks({len(chunks)}) 与 vectors({len(vectors)}) 数量不一致")
        if not chunks:
            return 0

        # `EventCard.date_start` 是 `date` 对象，直接塞进 JSON body 会报
        # "Object of type date is not JSON serializable" —— 第一次真跑 e2e
        # 就踩到了，而且被 remember 的 except 吞掉，表现为「chunk 一条都没写」。
        # 在服务层统一转，调用方不用关心。
        date_start_str = (
            date_start.isoformat()
            if date_start is not None and hasattr(date_start, "isoformat")
            else date_start
        )

        self.ensure_collection()
        dim = self._settings.EMBEDDING_DIM
        for v in vectors:
            if len(v) != dim:
                raise ChunkStoreError(f"向量维度 {len(v)} != EMBEDDING_DIM={dim}")

        written = 0
        for chunk, vector in zip(chunks, vectors):
            uid = chunk_uid(namespace, event_id, chunk.index)
            body = {
                "class": CHUNK_COLLECTION,
                "id": uid,
                "vector": vector,
                "properties": {
                    "namespace": namespace,
                    "event_id": event_id,
                    "chunk_index": chunk.index,
                    "text": chunk.text,
                    "date_start": date_start_str,
                    "is_deleted": False,
                },
            }
            try:
                self._http.post(f"{self._base}/v1/objects", json=body).raise_for_status()
            except httpx.HTTPError as exc:
                raise ChunkStoreError(f"写入 chunk 失败（{event_id}#{chunk.index}）: {exc}") from exc
            written += 1
        return written

    def delete_by_event(self, namespace: str, event_id: str) -> int:
        """把一条卡的所有 chunk 标 `is_deleted`（软删，与卡口径一致）。"""
        self.ensure_collection()
        uid = None
        deleted = 0
        # 先按 event_id 查出所有 chunk 的 uid
        try:
            resp = self._http.post(
                f"{self._base}/v1/graphql",
                json={
                    "query": (
                        "{ Get { " + CHUNK_COLLECTION + "(where: {"
                        'path: ["event_id"], operator: Equal, valueText: "' + event_id + '"'
                        "}, limit: 500) { chunk_index } } }"
                    )
                },
            )
            resp.raise_for_status()
            rows = (
                resp.json()
                .get("data", {})
                .get("Get", {})
                .get(CHUNK_COLLECTION, [])
            ) or []
        except httpx.HTTPError as exc:
            raise ChunkStoreError(f"查询 chunk 失败: {exc}") from exc

        for row in rows:
            idx = int(row.get("chunk_index", 0))
            uid = chunk_uid(namespace, event_id, idx)
            try:
                self._http.put(
                    f"{self._base}/v1/objects/{CHUNK_COLLECTION}/{uid}",
                    json={
                        "class": CHUNK_COLLECTION,
                        "id": uid,
                        "properties": {"is_deleted": True},
                    },
                ).raise_for_status()
            except httpx.HTTPError:
                logger.warning("软删 chunk 失败 uid=%s", uid, exc_info=True)
                continue
            deleted += 1
        return deleted

    # ---------- 读 ----------

    def search_near_vector(
        self, vector: list[float], namespace: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """向量检索 chunk，返回 `[{event_id, chunk_index, text, score}]`。

        **返回的是 chunk 级的命中**，聚合到卡是调用方（recall）的事。
        """
        self.ensure_collection()
        dim = self._settings.EMBEDDING_DIM
        if len(vector) != dim:
            raise ChunkStoreError(f"查询向量维度 {len(vector)} != {dim}")

        query = (
            "{ Get { "
            + CHUNK_COLLECTION
            + "("
            'nearVector: {vector: ' + _vec_literal(vector) + "}, "
            'where: {operator: And, operands: ['
            '{path: ["namespace"], operator: Equal, valueText: "' + namespace + '"}, '
            '{path: ["is_deleted"], operator: Equal, valueBoolean: false}'
            "]}, "
            f"limit: {int(limit)}"
            ") { event_id chunk_index text date_start _additional { distance } } } }"
        )
        try:
            resp = self._http.post(f"{self._base}/v1/graphql", json={"query": query})
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise ChunkStoreError(f"chunk 检索失败: {exc}") from exc

        rows = (payload.get("data", {}).get("Get", {}) or {}).get(CHUNK_COLLECTION) or []
        out: list[dict[str, Any]] = []
        for row in rows:
            distance = ((row.get("_additional") or {}).get("distance")) or 0.0
            out.append(
                {
                    "event_id": row.get("event_id"),
                    "chunk_index": int(row.get("chunk_index") or 0),
                    "text": row.get("text"),
                    "date_start": row.get("date_start"),
                    # distance → 相似度，与 WeaviateStore 的口径一致
                    "score": 1.0 - float(distance),
                }
            )
        return out

    def count(self, namespace: str) -> int:
        """该域下未删除的 chunk 数（运维/校验用）。"""
        self.ensure_collection()
        query = (
            "{ Aggregate { " + CHUNK_COLLECTION + "(where: {"
            'path: ["namespace"], operator: Equal, valueText: "' + namespace + '"'
            "}) { meta { count } } } }"
        )
        try:
            resp = self._http.post(f"{self._base}/v1/graphql", json={"query": query})
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise ChunkStoreError(f"统计 chunk 失败: {exc}") from exc
        agg = (payload.get("data", {}).get("Aggregate", {}) or {}).get(CHUNK_COLLECTION) or []
        if not agg:
            return 0
        return int(((agg[0].get("meta") or {}).get("count")) or 0)


def _vec_literal(vector: list[float]) -> str:
    """把向量拼成 GraphQL 字面量（`[0.1, 0.2]`）。

    不用参数化是因为 GraphQL 里 `nearVector.vector` 是标量字面量，
    Weaviate 的 REST 不支持 GraphQL 变量传数组（支持但写法更绕）。
    """
    return "[" + ", ".join(f"{float(x):.6f}" for x in vector) + "]"
