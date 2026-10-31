"""B8 Weaviate 写入：embed → 落库（REST 直连）。

**为什么不用 weaviate-client v4**：它的数据读写全部走 gRPC，
而本机（Windows 宿主 → WSL2 内 docker）的 gRPC 转发不通
（`50061` DEADLINE_EXCEEDED），REST 却正常。P0 只用 REST 就够：

    建 schema   POST /v1/schema
    写入       PUT  /v1/objects/{class}/{uuid}   （upsert，天然幂等）
    读回       GET  /v1/objects/{class}/{uuid}
    条件查询   POST /v1/graphql（M3 召回再补）

三条硬约束（ADR D1 / A25 / A26 / A34）：

1. **namespace 强制注入**：写入与查询都带，业务代码不手写。
2. **混模型拒绝**：向量维度与 `EMBEDDING_DIM` 不符直接拒绝。
3. **服务挂了抛 WeaviateStoreError**：由工具层转 503 / isError。

幂等：`event_id` 是内容哈希，映射成确定性 UUID，PUT 覆盖同一条记录。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import httpx

from memory.config import Settings
from memory.models.event_card import EventCard

#: 单 collection（ADR D1：namespace 用属性隔离，不拆 collection）
COLLECTION = "MemoryEvent"


class WeaviateStoreError(RuntimeError):
    """Weaviate 不可用或写入被拒。工具层应转 503 / isError。"""


class MixedModelError(WeaviateStoreError):
    """向量维度与配置不一致（A26）。"""


def deterministic_uuid(namespace: str, event_id: str) -> str:
    """把内容哈希映射成确定性 UUID，让重复写入等价于 upsert。

    Weaviate 主键必须是 UUID 格式，而 `event_id` 是 `evt_` 前缀的 16 位 hex。
    **namespace 参与**，与 event_id 的防撞设计一致。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{event_id}"))


def _rfc3339(value: date | datetime) -> str:
    """Weaviate 的 date 属性要求 RFC3339；纯日期补成当天零点 UTC。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return f"{value.isoformat()}T00:00:00Z"


#: REST schema 的属性定义（dataType 用 REST 的字符串形式）
SCHEMA_PROPERTIES: list[dict[str, Any]] = [
    {"name": "namespace", "dataType": ["text"]},
    {"name": "event_id", "dataType": ["text"]},
    {"name": "summary", "dataType": ["text"]},
    {"name": "date_start", "dataType": ["date"]},
    {"name": "date_end", "dataType": ["date"]},
    {"name": "types", "dataType": ["text[]"]},
    {"name": "entity_names", "dataType": ["text[]"]},
    {"name": "conclusion", "dataType": ["text"]},
    {"name": "unresolved", "dataType": ["text[]"]},
    {"name": "resolves", "dataType": ["text[]"]},
    {"name": "emotion", "dataType": ["text"]},
    {"name": "emotion_intensity", "dataType": ["number"]},
    {"name": "importance", "dataType": ["number"]},
    {"name": "importance_reason", "dataType": ["text"]},
    {"name": "retention", "dataType": ["text"]},
    {"name": "pinned", "dataType": ["boolean"]},
    {"name": "pinned_source", "dataType": ["text"]},
    {"name": "user_directive", "dataType": ["text"]},
    {"name": "recall_count", "dataType": ["int"]},
    {"name": "confirmed_by_use", "dataType": ["int"]},
    {"name": "ingested_at", "dataType": ["date"]},
    {"name": "observed_at", "dataType": ["date"]},
    # 护栏降级留痕（A33）
    {"name": "anomaly", "dataType": ["text"]},
    # 派生片段层软删标记（事件卡本身永不软删）
    {"name": "deleted_at", "dataType": ["date"]},
]


def to_properties(card: EventCard, *, anomaly: str | None = None) -> dict[str, Any]:
    """EventCard → Weaviate 属性字典。

    `anomaly` 是护栏降级的留痕（A33 要求可查），不属于 EventCard 模型。
    """
    props: dict[str, Any] = {
        "namespace": card.namespace,
        "event_id": card.event_id,
        "summary": card.summary,
        "date_start": _rfc3339(card.date_start),
        "types": list(card.types),
        "entity_names": [e.name for e in card.entities],
        "unresolved": list(card.unresolved),
        "resolves": list(card.resolves),
        "importance": card.importance,
        "retention": card.retention,
        "pinned": card.pinned,
        "recall_count": card.recall_count,
        "confirmed_by_use": card.confirmed_by_use,
        "ingested_at": _rfc3339(card.ingested_at),
    }
    if card.date_end is not None:
        props["date_end"] = _rfc3339(card.date_end)
    if card.conclusion is not None:
        props["conclusion"] = card.conclusion
    if card.emotion is not None:
        props["emotion"] = card.emotion
    if card.emotion_intensity is not None:
        props["emotion_intensity"] = card.emotion_intensity
    if card.importance_reason is not None:
        props["importance_reason"] = card.importance_reason
    if card.pinned_source is not None:
        props["pinned_source"] = card.pinned_source
    if card.user_directive is not None:
        props["user_directive"] = card.user_directive
    if card.observed_at is not None:
        props["observed_at"] = _rfc3339(card.observed_at)
    if anomaly is not None:
        props["anomaly"] = anomaly
    return props


class WeaviateStore:
    """Weaviate REST 的最小封装：建 schema + upsert + 读回。"""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._base = settings.WEAVIATE_URL.rstrip("/")
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=20.0)
        self._ready = False

    # ---------- 连接与 schema ----------

    def ping(self) -> None:
        """探活；失败抛 WeaviateStoreError。"""
        try:
            resp = self._http.get(f"{self._base}/v1/.well-known/ready")
        except httpx.HTTPError as exc:
            raise WeaviateStoreError(f"连接 Weaviate 失败: {exc}") from exc
        if resp.status_code != 200:
            raise WeaviateStoreError(f"Weaviate 未就绪: HTTP {resp.status_code}")

    def ensure_collection(self) -> None:
        """collection 不存在则创建；已存在则直接返回。"""
        if self._ready:
            return
        self.ping()
        try:
            resp = self._http.get(f"{self._base}/v1/schema")
            resp.raise_for_status()
            classes = {c.get("class") for c in resp.json().get("classes", [])}
        except httpx.HTTPError as exc:
            raise WeaviateStoreError(f"读取 schema 失败: {exc}") from exc

        if COLLECTION not in classes:
            body = {
                "class": COLLECTION,
                "vectorizer": "none",
                "properties": SCHEMA_PROPERTIES,
            }
            try:
                created = self._http.post(f"{self._base}/v1/schema", json=body)
            except httpx.HTTPError as exc:
                raise WeaviateStoreError(f"创建 collection 失败: {exc}") from exc
            if created.status_code not in (200, 201):
                raise WeaviateStoreError(
                    f"创建 collection 失败: HTTP {created.status_code} {created.text[:200]}"
                )
        self._ready = True

    # ---------- 写入 ----------

    def insert_event(
        self,
        card: EventCard,
        vector: list[float],
        *,
        anomaly: str | None = None,
    ) -> str:
        """upsert 一条事件卡，返回 Weaviate 对象 UUID。

        Raises:
            MixedModelError: 向量维度不符（A26）。
            WeaviateStoreError: 其他写入失败。
        """
        expected = self._settings.EMBEDDING_DIM
        if len(vector) != expected:
            raise MixedModelError(
                f"向量维度 {len(vector)} 与 EMBEDDING_DIM={expected} 不一致，"
                "拒绝写入（混模型会让检索结果失去可比性）"
            )

        self.ensure_collection()
        uid = deterministic_uuid(card.namespace, card.event_id)
        body = {
            "class": COLLECTION,
            "id": uid,
            "properties": to_properties(card, anomaly=anomaly),
            "vector": vector,
        }
        # POST /v1/objects 是「创建」；PUT /v1/objects/{class}/{id} 是「更新」，
        # 对象不存在时会 500，所以这里必须用 POST。
        try:
            resp = self._http.post(f"{self._base}/v1/objects", json=body)
        except httpx.HTTPError as exc:
            raise WeaviateStoreError(f"写入事件卡失败: {exc}") from exc

        if resp.status_code in (200, 201):
            return uid
        # 幂等兜底：并发写入时同 id 已存在，视为成功
        if resp.status_code == 422 and "already exists" in resp.text:
            return uid
        raise WeaviateStoreError(
            f"写入事件卡失败: HTTP {resp.status_code} {resp.text[:300]}"
        )

    # ---------- 读回 ----------

    def get_by_event_id(self, namespace: str, event_id: str) -> dict[str, Any] | None:
        """按 event_id 取回属性（幂等断言与图对账用）。"""
        self.ensure_collection()
        uid = deterministic_uuid(namespace, event_id)
        url = f"{self._base}/v1/objects/{COLLECTION}/{uid}"
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as exc:
            raise WeaviateStoreError(f"查询事件卡失败: {exc}") from exc

        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise WeaviateStoreError(
                f"查询事件卡失败: HTTP {resp.status_code} {resp.text[:200]}"
            )
        payload = resp.json()
        props = dict(payload.get("properties") or {})
        props["id"] = payload.get("id")
        # namespace 必须一致，否则说明 uuid5 映射被污染
        if props.get("namespace") != namespace:
            raise WeaviateStoreError(
                f"namespace 不一致：期望 {namespace}，实际 {props.get('namespace')}"
            )
        return props

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> WeaviateStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
