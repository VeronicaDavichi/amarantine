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

import json
import logging
import uuid
from datetime import date, datetime
from typing import Any

import httpx

from memory.config import Settings
from memory.core.tokenize import tokenize_for_index, tokenize_for_query
from memory.models.event_card import EventCard

logger = logging.getLogger("memory.weaviate")

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
    # 检索分词字段（BM25 中文召回靠它，见 core/tokenize.py 的说明）
    # `tokenization: word` + 空格分隔的 bigram → 2 字查询也能命中
    {
        "name": "summary_tokens",
        "dataType": ["text"],
        "tokenization": "word",
    },
    {
        "name": "conclusion_tokens",
        "dataType": ["text"],
        "tokenization": "word",
    },
    # 派生片段层软删标记（事件卡本身永不软删）
    # 用 `is_deleted` 布尔而不是靠 `deleted_at IsNull`：Weaviate 的 property 级
    # `invertedIndexConfig`（indexNullState）在 1.28 的建表请求里会被静默忽略，
    # 而 `IsNull` 过滤又强制要求它。布尔字段走普通倒排索引，没有这个坑。
    {"name": "is_deleted", "dataType": ["boolean"]},
    # 保留时间戳供审计展示（不参与过滤）
    {"name": "deleted_at", "dataType": ["date"]},
    # 人工裁决留痕（D5 resolve）。旧对象没有这两个属性，读出来是 None，
    # 语义正好是「还没人看过」——所以加字段不需要回填。
    {"name": "review_verdict", "dataType": ["text"]},
    {"name": "reviewer", "dataType": ["text"]},
    # 别名声明的留痕。存成 JSON 字符串而不是嵌套对象：
    # Weaviate 的嵌套属性要单独定义 nested 类型，为一个留痕字段不值得。
    # 图上的 `(:Alias)` 才是别名的主存储，这里只是「哪句话说的」的证据。
    {"name": "alias_claims", "dataType": ["text"]},
]


#: 重复上报时**允许回填**的属性：内容字段。
#:
#: 判据是「这个值是不是纯粹由输入决定的」。标注规则升级了（比如这次加了
#: `alias_claims`），老对象该补上新字段；输入没变，补上不会改变事实。
#:
#: **运行时字段一个都不在里面**：
#:
#: - `recall_count` / `confirmed_by_use`：记忆被用过多少次，重放输入不能清零
#: - `is_deleted`：删了的卡重报同一条内容**不能自己复活**（复活走 `revive()`）
#: - `retention` / `pinned` / `review_verdict` / `reviewer`：
#:   遗忘策略和人工裁决的当前状态，不是输入的一部分
#: - `ingested_at`：首次入库时间，重写会抹掉真实时间
#:
#: 这条界线是 upsert 语义的核心：**重放输入可以修正事实，不能改写历史。**
_BACKFILL_FIELDS = frozenset(
    {
        "summary",
        "summary_tokens",
        "types",
        "entity_names",
        "unresolved",
        "resolves",
        "alias_claims",
        "date_start",
        "date_end",
        "conclusion",
        "conclusion_tokens",
        "emotion",
        "emotion_intensity",
        "importance",
        "importance_reason",
        "user_directive",
    }
)


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
        # 检索分词：BM25 中文召回靠它（见 core/tokenize.py）
        "summary_tokens": tokenize_for_index(card.summary),
        # 新对象一律未删 —— 检索侧的 `is_deleted = false` 过滤依赖它存在且为 false
        "is_deleted": False,
    }
    if card.date_end is not None:
        props["date_end"] = _rfc3339(card.date_end)
    if card.conclusion is not None:
        props["conclusion"] = card.conclusion
        props["conclusion_tokens"] = tokenize_for_index(card.conclusion)
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
    if card.review_verdict is not None:
        props["review_verdict"] = card.review_verdict
    if card.reviewer is not None:
        props["reviewer"] = card.reviewer
    # 别名声明留痕。JSON 字符串，读回时解析。
    # **描述性声明也存**：`糖包就是那个AI里最弱智的` 不建图边，
    # 但这句话本身要留着 —— LLM 层（P2）靠它去解析出真正的等价对象。
    if card.alias_claims:
        import json

        props["alias_claims"] = json.dumps(
            [
                {
                    "alias": c.alias,
                    "canonical": c.canonical,
                    "label": c.label,
                    "source": c.source,
                }
                for c in card.alias_claims
            ],
            ensure_ascii=False,
        )
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
        """collection 不存在则创建；已存在则补齐缺失属性。

        **为什么不是「已存在就返回」**：往 `SCHEMA_PROPERTIES` 加字段后，
        老 collection 不会自动获得新属性，查询会因为「字段不存在」而报错。
        重建 collection 能解决但会丢数据。

        Weaviate 支持往已有 class 追加属性，所以这里做增量补齐：
        对比远端 schema 与本地的差集，逐个 `POST /v1/schema/{class}/properties`。
        旧对象读新字段得到 `None`，语义正好是「还没裁决过」。
        """
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
        else:
            self._add_missing_properties(classes=classes)
        self._ready = True

    def _add_missing_properties(self, *, classes: set[str]) -> None:
        """给已存在的 collection 补上 `SCHEMA_PROPERTIES` 里新增的属性。"""
        try:
            detail = self._http.get(f"{self._base}/v1/schema/{COLLECTION}")
            detail.raise_for_status()
            existing = {
                p.get("name") for p in detail.json().get("properties", []) or []
            }
        except httpx.HTTPError as exc:
            raise WeaviateStoreError(f"读取 {COLLECTION} 属性失败: {exc}") from exc

        for prop in SCHEMA_PROPERTIES:
            name = prop["name"]
            if name in existing:
                continue
            try:
                added = self._http.post(
                    f"{self._base}/v1/schema/{COLLECTION}/properties", json=prop
                )
            except httpx.HTTPError as exc:
                raise WeaviateStoreError(f"追加属性 {name} 失败: {exc}") from exc
            if added.status_code not in (200, 201):
                raise WeaviateStoreError(
                    f"追加属性 {name} 失败: HTTP {added.status_code} {added.text[:200]}"
                )
            logger.info("Weaviate 属性已补齐: %s.%s", COLLECTION, name)

    # ---------- 写入 ----------

    def insert_event(
        self,
        card: EventCard,
        vector: list[float],
        *,
        anomaly: str | None = None,
    ) -> str:
        """upsert 一条事件卡，返回 Weaviate 对象 UUID。

        已存在时**只回填内容字段**，绝不覆盖运行时状态 —— 详见
        `_backfill_content_fields` 的说明。

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
        props = to_properties(card, anomaly=anomaly)
        body = {
            "class": COLLECTION,
            "id": uid,
            "properties": props,
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
        # 已存在：幂等成功，但要把**新增的内容字段**回填回去。
        #
        # 早先这里的注释是「并发写入时同 id 已存在，视为成功」就完事了，
        # 那是个真 bug：`event_id` 是内容哈希，同一条事件重写永远拿同一个 id，
        # 于是这条分支成了常态。给 schema 加新字段（alias_claims 等）之后，
        # 老对象永远补不上 —— 实测重跑验证脚本，新字段读出来是空的。
        if resp.status_code == 422 and "already exists" in resp.text:
            self._backfill_content_fields(uid, props)
            return uid
        raise WeaviateStoreError(
            f"写入事件卡失败: HTTP {resp.status_code} {resp.text[:300]}"
        )

    # ---------- 读回 ----------

    def backfill_event(self, card: EventCard, *, anomaly: str | None = None) -> int:
        """把**缺失的内容字段**补到已存在的对象上，返回补了几个。

        为什么需要这个公开入口：`remember` 的幂等检查在 `insert_event`
        **之前**就短路了（同 event_id 直接返回），所以 `insert_event` 里的
        422 分支在正常流程中根本走不到。schema 升级后要给老对象补字段，
        只能由调用方在短路处显式调一次。

        只补内容字段，运行时状态（`recall_count` / `is_deleted` …）不碰 ——
        详见 `_BACKFILL_FIELDS` 的说明。
        """
        self.ensure_collection()
        uid = deterministic_uuid(card.namespace, card.event_id)
        return self._backfill_content_fields(
            uid, to_properties(card, anomaly=anomaly)
        )

    def _backfill_content_fields(self, uid: str, props: dict[str, Any]) -> int:
        """把已有对象**缺失的**内容字段补上，返回补了几个。

        只补两类：
        1. 老对象根本没有这个属性（schema 后来才加的字段）
        2. 老对象的值是 `None`（旧版本写的时候还是空）

        **已有值不覆盖**：同一条事件的 `summary` 理论上不会变，
        但万一变了，覆盖会让「用户说过的原话」被悄悄改写 —— 那是事实层，
        不能由一次重复上报推翻。

        失败只告警不抛：回填是增强，主流程（幂等成功）已经达成了。
        """
        try:
            resp = self._http.get(f"{self._base}/v1/objects/{COLLECTION}/{uid}")
        except httpx.HTTPError:
            logger.warning("回填前读取对象失败，跳过 uid=%s", uid, exc_info=True)
            return 0
        if resp.status_code != 200:
            return 0

        existing = resp.json().get("properties") or {}
        patch = {
            name: value
            for name, value in props.items()
            if name in _BACKFILL_FIELDS
            and value is not None
            and existing.get(name) is None
        }
        if not patch:
            return 0

        try:
            patched = self._http.patch(
                f"{self._base}/v1/objects/{COLLECTION}/{uid}",
                json={"properties": patch},
            )
        except httpx.HTTPError:
            logger.warning("回填属性失败 uid=%s", uid, exc_info=True)
            return 0

        if patched.status_code not in (200, 204):
            logger.warning(
                "回填属性失败 uid=%s HTTP %s %s",
                uid,
                patched.status_code,
                patched.text[:200],
            )
            return 0

        logger.info("回填属性 uid=%s 字段=%s", uid, sorted(patch))
        return len(patch)

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

    # ---------- 全量列举（M5 管理接口用） ----------

    def list_all(self, namespace: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        """列出某 namespace 下所有未删除的卡。

        管理接口的数据量是「一个人多年的记忆」，几百到几千条，
        一次性拉全量足够；真到上万条再换游标分页。
        """
        self.ensure_collection()
        query = (
            "query {"
            " Get { MemoryEvent("
            f"limit: {int(limit)}, where: "
            + self._where_graphql(namespace)
            + f") {{{self._ADMIN_FIELDS}}} }} }}"
        )
        data = self._graphql(query)
        return [self._to_hit(obj, score=0.0) for obj in self._extract_hits(data)]

    # ---------- 软删与复活（D7 / A32） ----------

    def revive(self, event_id: str, namespace: str) -> None:
        """把一条软删过的卡恢复成可见（`is_deleted=False`）。

        **为什么需要它**：用户说「忘掉 X」之后又把 X 原样说了一遍，
        说明 X 又有用了。如果幂等检查只看到「卡已存在」就静默返回，
        这条记忆会永远停留在已忘状态 —— 用户说什么都没用，除非换措辞
        让 `event_id` 变掉。那是明显的 bug，不是保守。

        **不删 `deleted_at`**：留作审计（这条卡曾被忘过、又被重新提起）。
        检索侧只看 `is_deleted`，所以留着不影响正确性。
        """
        self.ensure_collection()
        uid = deterministic_uuid(namespace, event_id)
        url = f"{self._base}/v1/objects/{COLLECTION}/{uid}"
        try:
            resp = self._http.patch(
                url,
                json={"class": COLLECTION, "properties": {"is_deleted": False}},
            )
        except httpx.HTTPError as exc:
            raise WeaviateStoreError(f"复活事件卡失败: {exc}") from exc
        if resp.status_code not in (200, 204):
            raise WeaviateStoreError(
                f"复活事件卡失败: HTTP {resp.status_code} {resp.text[:200]}"
            )

    def mark_forgotten(
        self, event_id: str, namespace: str, *, deleted_at: str
    ) -> None:
        """给对象打 `deleted_at`，**不删除对象**（N4：事件卡永不物理删）。

        写完之后检索侧靠 `deleted_at IsNull` 把它过滤掉，
        但审计通道（按 id 直取）仍能读到。
        """
        self.ensure_collection()
        uid = deterministic_uuid(namespace, event_id)
        url = f"{self._base}/v1/objects/{COLLECTION}/{uid}"
        try:
            resp = self._http.patch(
                url,
                json={
                    "class": COLLECTION,
                    "properties": {"is_deleted": True, "deleted_at": deleted_at},
                },
            )
        except httpx.HTTPError as exc:
            raise WeaviateStoreError(f"标记软删失败: {exc}") from exc
        if resp.status_code not in (200, 204):
            raise WeaviateStoreError(
                f"标记软删失败: HTTP {resp.status_code} {resp.text[:200]}"
            )

    def set_confirmed_by_use(
        self, event_id: str, namespace: str, *, count: int
    ) -> None:
        """把 `confirmed_by_use` 设成**绝对值**（不是自增）。

        为什么用绝对值：Weaviate 没有原子自增，读-改-写在并发下会丢更新。
        真值在 SQLite 的 `recall_feedback` 表里（幂等键保证不重复计数），
        这里只是把真值镜像过去。下次 feedback 会用新的绝对值覆盖，
        所以偶发的丢失能自愈 —— 换成 `+1` 就永远丢掉了。
        """
        self.ensure_collection()
        uid = deterministic_uuid(namespace, event_id)
        url = f"{self._base}/v1/objects/{COLLECTION}/{uid}"
        try:
            resp = self._http.patch(
                url,
                json={
                    "class": COLLECTION,
                    "properties": {"confirmed_by_use": max(0, int(count))},
                },
            )
        except httpx.HTTPError as exc:
            raise WeaviateStoreError(f"写 confirmed_by_use 失败: {exc}") from exc
        if resp.status_code not in (200, 204):
            raise WeaviateStoreError(
                f"写 confirmed_by_use 失败: HTTP {resp.status_code} {resp.text[:200]}"
            )

    # ---------- 人工裁决（D5 resolve） ----------

    def set_review_verdict(
        self,
        event_id: str,
        namespace: str,
        *,
        verdict: str,
        reviewer: str | None = None,
        pinned: bool | None = None,
    ) -> None:
        """写人工裁决结果，可选同步 `pinned`。

        `keep` 需要同时置 `pinned=true` / `pinned_source=manual`：
        ADR 规定 keep 等价于人工钉住。反过来 `delete` **不动 pinned** ——
        人判「可删」不该悄悄解掉一条被钉的记录，那会让审计对不上。

        Raises:
            WeaviateStoreError: 写入失败（调用方映射成 503）。
        """
        if verdict not in ("keep", "delete"):
            raise ValueError(f"verdict 必须是 keep/delete：{verdict}")

        self.ensure_collection()
        uid = deterministic_uuid(namespace, event_id)
        props: dict[str, Any] = {"review_verdict": verdict}
        if reviewer is not None:
            props["reviewer"] = reviewer
        if pinned is not None:
            props["pinned"] = pinned
            if pinned:
                props["pinned_source"] = "manual"

        url = f"{self._base}/v1/objects/{COLLECTION}/{uid}"
        try:
            resp = self._http.patch(
                url, json={"class": COLLECTION, "properties": props}
            )
        except httpx.HTTPError as exc:
            raise WeaviateStoreError(f"写 review_verdict 失败: {exc}") from exc
        if resp.status_code not in (200, 204):
            raise WeaviateStoreError(
                f"写 review_verdict 失败: HTTP {resp.status_code} {resp.text[:200]}"
            )

    # ---------- 检索（M3 召回用） ----------

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行 GraphQL；失败转 WeaviateStoreError（调用方据此降级）。

        `where` 这类结构化过滤**必须走变量**：GraphQL 不认 JSON 对象字面量，
        内联会直接语法错误。
        """
        try:
            resp = self._http.post(
                f"{self._base}/v1/graphql",
                json={"query": query, "variables": variables or {}},
            )
        except httpx.HTTPError as exc:
            raise WeaviateStoreError(f"GraphQL 请求失败: {exc}") from exc
        if resp.status_code != 200:
            raise WeaviateStoreError(
                f"GraphQL 返回 {resp.status_code}: {resp.text[:300]}"
            )
        body = resp.json()
        if body.get("errors"):
            raise WeaviateStoreError(f"GraphQL 报错: {body['errors'][:1]}")
        return body.get("data") or {}

    @staticmethod
    def _where_graphql(namespace: str, *, date_from: str | None = None) -> str:
        """构造 where **GraphQL 对象字面量**。

        两个坑：
        1. Weaviate 不接受 `$where: WhereFilter` 变量声明（Unknown type）
        2. 也不接受 JSON 字面量（键带引号会语法错）
        所以只能拼 GraphQL 的对象语法（键不带引号、逗号分隔）。
        """
        operands = [
            '{path: ["namespace"], operator: Equal, valueText: '
            + json.dumps(namespace, ensure_ascii=False)
            + "}",
            # 软删的卡永不进检索结果（A32）；审计通道按 id 直取，不受影响
            '{path: ["is_deleted"], operator: Equal, valueBoolean: false}',
        ]
        if date_from:
            operands.append(
                '{path: ["date_start"], operator: GreaterThanEqual, valueDate: '
                + json.dumps(date_from, ensure_ascii=False)
                + "}"
            )
        if len(operands) == 2:
            return "{operator: And, operands: [" + ", ".join(operands) + "]}"
        return "{operator: And, operands: [" + ", ".join(operands) + "]}"

    @staticmethod
    def _to_hit(obj: dict[str, Any], *, score: float) -> dict[str, Any]:
        """GraphQL 对象 → 扁平 dict。

        **保留全部属性**，只剔除 `_additional`。以前这里是白名单，
        结果 `retention` / `recall_count` / `confirmed_by_use` / `review_verdict`
        被静默丢弃 —— 调用方 `.get()` 拿到的永远是默认值，看起来一切正常，
        实际是「遗忘策略把每张卡都当成 retention=normal、召回 0 次」。
        """
        props = dict(obj)
        extra = props.pop("_additional", {}) or {}
        props.setdefault("unresolved", [])
        props.setdefault("entity_names", [])
        props["unresolved"] = props.get("unresolved") or []
        props["entity_names"] = props.get("entity_names") or []
        props["score"] = score
        props["distance"] = extra.get("distance")
        return props

    #: 管理接口要的字段集（不能用 `_FIELDS` 拼接：它结尾是 `_additional`，
    #: GraphQL 里继续追加字段会直接语法错误）
    _ADMIN_FIELDS = """
      event_id
      namespace
      summary
      date_start
      date_end
      types
      importance
      pinned
      pinned_source
      unresolved
      entity_names
      retention
      recall_count
      confirmed_by_use
      review_verdict
      reviewer"""

    #: 两路都要请求：`distance` 给向量通道，`score` 给 BM25 通道。
    #: 缺失的那个 Weaviate 会返回 null，不影响。
    #: **踩过的坑**：只请求 `distance` 时 BM25 的分恒为 0，下游 `score > 0`
    #: 的过滤会把整个关键词通道静默清空 —— 表现是「查词命中但排序不对」。
    _FIELDS = """
      event_id
      namespace
      summary
      date_start
      importance
      pinned
      unresolved
      entity_names
      _additional { distance score }"""

    @staticmethod
    def _extract_hits(data: dict[str, Any]) -> list[dict[str, Any]]:
        return ((data.get("Get") or {}).get(COLLECTION)) or []

    def search_near_vector(
        self,
        vector: list[float],
        namespace: str,
        *,
        limit: int = 50,
        date_from: str | None = None,
    ) -> list[dict[str, Any]]:
        """向量召回。distance 转成 (0,1] 的相似度，越大越相关。"""
        if len(vector) != self._settings.EMBEDDING_DIM:
            raise MixedModelError(
                f"查询向量维度 {len(vector)} 与 EMBEDDING_DIM={self._settings.EMBEDDING_DIM} 不一致"
            )
        self.ensure_collection()
        # limit 必须内联：走变量会被 Weaviate 解析成 int64 并报类型转换错
        query = (
            "query ($vector: [Float!]!) {"
            " Get { MemoryEvent("
            f"nearVector: {{vector: $vector}}, limit: {int(limit)}, where: "
            + self._where_graphql(namespace, date_from=date_from)
            + f") {{{self._FIELDS}}} }} }}"
        )
        data = self._graphql(query, {"vector": vector})
        out = []
        for obj in self._extract_hits(data):
            distance = (obj.get("_additional") or {}).get("distance") or 0.0
            # `distance` 对 cosine 度量是 `1 - cosine`，所以余弦就是 `1 - distance`。
            # **不要用 `1/(1+d)`** —— 那会把 0.68/0.42 压成 0.757/0.632，
            # 区分度掉一半，无关查询的候选就再也过滤不掉了。
            out.append(self._to_hit(obj, score=max(0.0, 1.0 - float(distance))))
        return out

    #: BM25 检索的字段。**必须用分词字段**，不能用 `summary` 原文：
    #: `summary` 的 word tokenizer 把整句中文当单个 token，2 字查询永远 0 命中。
    _BM25_PROPERTIES = ("summary_tokens", "conclusion_tokens")

    def search_bm25(
        self,
        text: str,
        namespace: str,
        *,
        limit: int = 50,
        date_from: str | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 关键词召回。Weaviate 的 bm25 分没有上界，这里保留原分。

        查询词先过 `tokenize_for_query`（与索引侧同一套 bigram 规则），
        再只在这些 `*_tokens` 字段上做 BM25 —— 这样中文 2 字查询也能命中。
        """
        self.ensure_collection()
        query_tokens = tokenize_for_query(text)
        if not query_tokens:
            return []
        props = ", ".join(f'"{p}"' for p in self._BM25_PROPERTIES)
        query = (
            "query ($query: String!) {"
            " Get { MemoryEvent("
            f"bm25: {{query: $query, properties: [{props}]}}, "
            f"limit: {int(limit)}, where: "
            + self._where_graphql(namespace, date_from=date_from)
            + f") {{{self._FIELDS}}} }} }}"
        )
        data = self._graphql(query, {"query": query_tokens})
        return [
            self._to_hit(
                obj, score=float((obj.get("_additional") or {}).get("score") or 0.0)
            )
            for obj in self._extract_hits(data)
        ]

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> WeaviateStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
