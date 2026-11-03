"""Admin HTTP 入口（人工通道）。

ADR 4.0 的双入口约定：**MCP stdio 给 LLM，Admin HTTP 给人**。
这里只放人要做的事 —— 看复核队列、手工遗忘、跑扫描报告。
LLM 的写入/召回一律走 MCP，不从这里进。

安全基线：
- 只监听 `127.0.0.1`（`ADMIN_HOST`），不暴露到局域网
- 除 `/health` 外全部要求 `Authorization: Bearer <ADMIN_TOKEN>`
- `ADMIN_TOKEN` 没配就**拒绝启动**，不给"忘了配就等于无认证"的机会

P0 边界：`POST /admin/forget/sweep` 只接受 `dry_run=true`（A12）。
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from memory.config import Settings, load_settings
from memory.services.graph_search import event_degrees
from memory.services.vocab_version import VocabRegistry, effective_vocab
from memory.core.forget_policy import (
    BUCKET_NEED_REVIEW,
    BUCKET_PROTECTED,
    BUCKET_SAFE_TO_DELETE,
    ForgetCandidate,
    classify,
    sweep,
)
from memory.services.alias_apply import (
    resolve_event_aliases,
    resolve_namespace_aliases,
)
from memory.services.entity_edit import (
    EditResult,
    merge_entities,
    rebind_event_entity,
    rename_entity,
    split_entity,
)
from memory.services.forget import (
    ADMIN_REASONS,
    ALLOWED_REASONS,
    ForgetError,
    forget_event,
)
from memory.services.feedback import FeedbackError, TraceNotFoundError, submit_feedback
from memory.services.label_review import LabelReviewQueue, queue_from_settings
from memory.services.recall import RecallUnavailableError, recall
from memory.services.recall_count import RecallCounter, db_path_from_settings
from memory.services.recall_trace import trace_store_from_settings
from memory.services.raw_quote import (
    JOB_QUEUED,
    JOB_REJECTED,
    IngestJobStore,
    RawQuoteStore,
    normalize_turn,
    quote_id_for,
    run_ingest,
)
from memory.services.weaviate_store import WeaviateStore, WeaviateStoreError

logger = logging.getLogger("memory.admin")

#: `GET /raw` 反查事件卡时一次最多扫多少条。
#:
#: 超过这个数 `event_ids` 就可能不全，响应里的 `truncated` 会为 true。
#: 与 `POST /admin/archive` 的扫描上限是同一类取舍，所以两边都用 `scanned`
#: 告诉调用方「这个结论建立在多少条之上」。
_RAW_SCAN_LIMIT = 5000

#: `GET /admin/events` 一次最多拉多少条。分页在应用层做 ——
#: Weaviate 的 `Get` 不支持 offset，与其假装支持，不如一次拉回来再切。
_EVENTS_SCAN_LIMIT = 5000

#: 前端静态页目录（`/ui`）。跟代码放一起，pip 安装时也会带上。
_WEB_DIR = Path(__file__).parent / "web"

#: 允许的 bucket 取值（A24：NEED_REVIEW 与 LABEL_REVIEW 是两条队列，禁止混用）
ALLOWED_BUCKETS = (BUCKET_NEED_REVIEW, BUCKET_PROTECTED, BUCKET_SAFE_TO_DELETE)

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------- 报文模型


class ForgetRequest(BaseModel):
    event_id: str = Field(min_length=1)
    reason: str = "user_forget"
    #: 不填则用 `MEMORY_NAMESPACE`
    namespace: str | None = None


class ForgetResponse(BaseModel):
    event_id: str
    forgotten: bool
    reason: str


class SweepRequest(BaseModel):
    dry_run: bool = True
    limit: int = Field(default=200, ge=1, le=1000)
    #: 不填则用 `MEMORY_NAMESPACE`
    namespace: str | None = None


class ReviewItem(BaseModel):
    event_id: str
    bucket: str
    reason: str
    idle_days: int
    age_days: int
    summary: str | None = None


class ReviewResponse(BaseModel):
    bucket: str
    items: list[ReviewItem]
    total: int


class SweepResponse(BaseModel):
    buckets: dict[str, int]
    dry_run: bool
    sample: list[ReviewItem]
    #: A10：图查询失败时为 true。此时 `graph_degree` 一律按 0 处理，
    #: 「图枢纽」这条硬信号**本次未生效** —— 必须显式告诉调用方，
    #: 否则报告看起来一切正常，实则少了一类保护。
    graph_degraded: bool = False


class VocabResponse(BaseModel):
    """A23：词表 + 版本号。"""

    version: str
    vocab: list[str]
    count: int


# ---- 别名推断（P2：把描述性声明解析成具体名字）----


class AliasResolveRequest(BaseModel):
    #: 不填则扫整个命名空间
    event_id: str | None = None
    namespace: str | None = None
    #: 命名空间模式下最多扫多少张卡（LLM 慢，不能无限等）
    limit: int = Field(default=20, ge=1, le=200)
    timeout_s: float | None = Field(default=30.0, gt=0, le=300)


class AliasResolveResponse(BaseModel):
    mode: Literal["event", "namespace"]
    items: list[dict[str, Any]]
    total: int


# ---- 实体纠错（改 / 拆 / 合 / 解挂重挂，**没有删除**）----


class EntityRenameRequest(BaseModel):
    from_name: str = Field(min_length=1)
    to_name: str = Field(min_length=1)
    namespace: str | None = None
    reason: str = ""


class EntityMergeRequest(BaseModel):
    from_name: str = Field(min_length=1)
    to_name: str = Field(min_length=1)
    namespace: str | None = None
    reason: str = ""
    #: 合并的可信度。用户手工合并默认 `exact`
    label: Literal["exact", "probable", "uncertain"] = "exact"


class EntitySplitRequest(BaseModel):
    from_name: str = Field(min_length=1)
    into: list[str] = Field(min_length=2)
    namespace: str | None = None
    reason: str = ""


class EntityRebindRequest(BaseModel):
    event_id: str = Field(min_length=1)
    from_name: str = Field(min_length=1)
    to_name: str = Field(min_length=1)
    #: 目标到底是什么。系统不猜 —— `三哥` 是人还是国家，只有说的人知道
    to_kind: str = "topic"
    namespace: str | None = None
    reason: str = ""


class EntityEditResponse(BaseModel):
    ok: bool
    op: str
    detail: dict[str, Any] = {}
    error: str | None = None


class EntityEditEntry(BaseModel):
    """一条纠错痕迹（`:EntityEdit` 节点）。

    「纠错是修正，不是抹除痕迹」—— 写接口一直有，读接口原来没有。
    `args` 在图上是 JSON 字符串，这里已经解析成对象。
    """

    op: str
    target: str | None = None
    args: dict[str, Any] | str = {}
    reason: str | None = None
    operator: str | None = None
    created_at: str
    #: `args` 解析失败时为 True，此时 `args` 原样是字符串 —— 不静默吞
    args_parse_error: bool = False


class EntityEditListResponse(BaseModel):
    items: list[EntityEditEntry]
    total: int


# ---- 打标复核（A24：与上面的 NEED_REVIEW 是**两条独立队列**，字段刻意不同）----


class LabelReviewEntry(BaseModel):
    """一条打标分歧。

    与 `ReviewItem` 的区别（A24 要求两者永不混用）：

    | 字段 | `LabelReviewEntry` | `ReviewItem` |
    |---|---|---|
    | 问题 | 这张卡**打得对不对** | 这张卡**该不该留** |
    | 依据 | `rules_types` vs `llm_types` | 闲置天数 / 硬信号 |
    | 动作 | 采纳哪一方的标签 | 删 / 留 |
    """

    event_id: str
    summary: str | None = None
    rules_types: list[str]
    llm_types: list[str]
    agreement: float
    created_at: str


class LabelReviewListResponse(BaseModel):
    items: list[LabelReviewEntry]
    total: int


class LabelReviewResolveRequest(BaseModel):
    #: 人工判定结果，自由文本；建议 `kept_rules` / `kept_llm` / `merged`
    resolution: str = "accepted"
    #: 不填则用 `MEMORY_NAMESPACE`
    namespace: str | None = None


class LabelReviewResolveResponse(BaseModel):
    event_id: str
    resolved: bool
    resolution: str


# ---- 别名拒绝留痕（为什么那条边没建）----


class AliasRejectEntry(BaseModel):
    """一条被拒的别名声明。

    `side` 说明是哪一头被拒：

    - ``alias``（左值）：`三哥主打的` —— 连 claim 都不生成，最隐形
    - ``canonical``（右值）：`那个ai中最弱智的` —— claim 在卡上，建边时跳过

    `reason` 取自 `labeler.NAME_REJECT_REASONS`，`detail` 是具体证据
    （命中的标记 / 开头的字）。
    """

    event_id: str
    alias: str
    canonical: str
    side: str
    reason: str
    detail: str = ""
    source_text: str | None = None
    created_at: str
    #: `None` = 真没处理过；`accept_pending` = 人已裁决但补边失败，
    #: 仍在待办里可重试。列表必须能区分这两者。
    resolution: str | None = None


class AliasRejectListResponse(BaseModel):
    items: list[AliasRejectEntry]
    total: int
    #: 按 reason 分组的未处理数 —— 判断「判据是不是该改」看这个
    by_reason: dict[str, int]


class AliasRejectResolveRequest(BaseModel):
    event_id: str
    alias: str
    canonical: str
    side: str
    #: `accepted`（判据错了 → 补建边）/ `dismissed`（判据对，就该拒）
    #: / `deferred`（先放着）
    resolution: str = "accepted"
    namespace: str | None = None


class AliasRejectResolveResponse(BaseModel):
    resolved: bool
    resolution: str
    #: `accepted` 时才有意义：别名边是否已经真的写到图上。
    #: `False` 表示图写入失败，此行的 `resolution` 被记成 `accept_pending`
    #: 并继续留在待办里 —— 不会假装成功。
    repaired: bool | None = None


class SurnameDeclareRequest(BaseModel):
    """声明姓氏（`这人姓种` 的程序化版本）。"""

    chars: list[str]
    evidence: str | None = None
    namespace: str | None = None


class SurnameEntry(BaseModel):
    char: str
    evidence: str | None = None
    event_id: str | None = None
    created_at: str


class SurnameListResponse(BaseModel):
    items: list[SurnameEntry]
    #: 进程内当前生效的（含启动时从表里灌的）
    active: list[str]


# ---- 事件管理（ADR 4.6 PATCH / 4.7 DELETE / 4.4 archive）----


class EventPatchRequest(BaseModel):
    """改事件的 `pinned` / `retention`。**白名单之外一律 400。**

    没有 `summary` / `types`：它们参与 `event_id` 的内容哈希，直接改会让
    卡与图/向量的派生关系对不上，必须走标签复核或重建。

    `extra="allow"` 是故意的：未知字段要能被端点**看见并拒绝**（400）。
    默认的 `ignore` 会让 `{"summary": "..."}` 静默通过 —— 请求 200 了、
    什么都没改，调用方以为改成功了。
    """

    model_config = ConfigDict(extra="allow")

    pinned: bool | None = None
    retention: str | None = None
    namespace: str | None = None


class EventPatchResponse(BaseModel):
    event_id: str
    pinned: bool | None = None
    pinned_source: str | None = None
    retention: str | None = None
    #: 只在**真的改了值**时更新；幂等重复置同一值不会变（ADR 4.6）
    updated_at: str | None = None


class EventDeleteRequest(BaseModel):
    """软删一条事件卡。**`hard=true` 会被 400 拒绝**，这是设计上关死的。

    不是「暂未实现物理删除」：D7 + A9 规定事实层永不物理删。
    """

    #: `user_forget`（用户开口）或 `retention_expired`（保留期到了）
    reason: str = "user_forget"
    hard: bool = False
    namespace: str | None = None


class EventDeleteResponse(BaseModel):
    event_id: str
    soft_deleted: bool = True
    #: 事实层零改动，所以恒 true。**但原文层尚未实现（005）**，
    #: 这个字段目前只表示「没删任何原文」，不代表「能查到原文」。
    raw_quote_preserved: bool = True
    deleted_at: str | None = None


class ArchiveRequest(BaseModel):
    window_days: int = 180
    dry_run: bool = True
    namespace: str | None = None


class ArchiveResponse(BaseModel):
    window_days: int
    #: `observed_at < now - window_days` 的卡数
    candidates: int
    #: 这次计数建立在多少条之上 —— 不是精确全量，别把数字当全库
    scanned: int
    #: P0 恒 0（ADR 4.4：只出候选，不搬迁）
    moved: int = 0
    mode: str = "logical"


class AdminRecallRequest(BaseModel):
    query: str
    namespace: str | None = None
    budget_ms: int | None = None


class AdminRecallResponse(BaseModel):
    materials: list[dict[str, Any]] = []
    degraded: bool = False
    partial: bool = False
    trace: dict[str, Any] = {}
    #: 非空才能调 feedback —— 4.9 两个接口的唯一耦合点
    trace_id: str | None = None


class RecallFeedbackRequest(BaseModel):
    used_material_ids: list[str]
    namespace: str | None = None


class RecallFeedbackResponse(BaseModel):
    trace_id: str
    accepted: int = 0
    deduped: int = 0
    #: `confirmed_by_use` 实际 +1 的材料
    incremented: list[str] = []
    rejected: list[str] = []


# ---- 原文层（ADR 4.2 批量导入 / 4.8 原文溯源）----


class IngestTurn(BaseModel):
    text: str
    role: str = "user"
    ts: str | None = None
    #: 标注了就不产卡（ADR 4.2 的指令拦截）
    do_not_store: bool = False
    model_config = ConfigDict(extra="ignore")


class IngestRequest(BaseModel):
    source: str = "chat"
    session_id: str | None = None
    #: 客户端用它防重复提交；同值的第二次提交返回**已有 job**，不重跑
    dedupe_key: str
    turns: list[IngestTurn]
    namespace: str | None = None


class IngestResponse(BaseModel):
    job_id: str | None = None
    status: str
    #: 只有整批被 `do_not_store` 拦下时才给 `"do_not_store"`
    reason: str | None = None
    events_created: int = 0
    turns_total: int = 0
    turns_skipped: int = 0


class IngestJobResponse(BaseModel):
    job_id: str
    status: str
    events_created: int = 0
    turns_total: int = 0
    turns_skipped: int = 0
    source: str = "chat"
    session_id: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class EventListItem(BaseModel):
    """管理台列表用的一张卡（比召回材料窄，只留列表要显示的）。"""

    event_id: str
    summary: str
    date_start: str
    date_end: str | None = None
    types: list[str] = []
    importance: str = ""
    pinned: bool = False
    #: `list_all()` 只返回未删的卡，所以这里恒为 false ——
    #: 保留字段是为了让前端知道「这个字段存在且已用上」，而不是以为漏了。
    is_deleted: bool = False
    raw_quote_ids: list[str] = []
    recall_count: int = 0


class EventListResponse(BaseModel):
    total: int
    items: list[EventListItem]


class RawQuoteResponse(BaseModel):
    quote_id: str
    text: str
    role: str
    ts: str | None = None
    source: str
    session_id: str | None = None
    #: 这条原文生成了哪些事件卡（扫 Weaviate 反查，不建反向索引）
    event_ids: list[str] = []
    #: 反查时实际扫了多少张卡。`event_ids` 只在这个范围内成立 ——
    #: 与 `POST /admin/archive` 的 `scanned` 同口径。
    scanned: int = 0
    #: 扫描触到 `_RAW_SCAN_LIMIT` 上限，`event_ids` **可能不全**。
    #: 单给 `scanned` 不够：调用方还得自己比 5000，容易漏。
    truncated: bool = False
    #: false = 反查失败，`event_ids` **不可信**。
    #: 与「确定没有关联」（`lookup_ok=true` + `event_ids=[]`）必须分开，
    #: 否则一次 Weaviate 抖动就会被读成「这张记忆没有出处」。
    lookup_ok: bool = True


# ---- NEED_REVIEW 裁决（D5：与上面的 LABEL_REVIEW 是另一条队列）----


class ReviewVerdictRequest(BaseModel):
    action: Literal["keep", "delete"]
    #: 谁做的决定，进审计
    reviewer: str = "owner"
    note: str | None = None
    #: 不填则用 `MEMORY_NAMESPACE`
    namespace: str | None = None


class ReviewVerdictResponse(BaseModel):
    event_id: str
    bucket_before: str | None
    bucket_after: str
    deleted: bool = False
    #: 幂等命中：本次没有真正改变状态
    unchanged: bool = False


# ---------------------------------------------------------------- 应用工厂


def create_app(
    settings: Settings | None = None, *, maintenance: bool = False
) -> FastAPI:
    """构造 FastAPI 应用。settings 可注入（测试用）。

    `maintenance=True` 才会起定期补跑循环 —— **测试默认关掉**，
    否则每次 TestClient 都会去连 Weaviate/Neo4j 对账，慢且无意义。
    """
    cfg = settings or load_settings()

    lifespan = None
    if maintenance and cfg.MEMORY_NAMESPACE:

        @asynccontextmanager
        async def lifespan(_app: FastAPI):  # type: ignore[misc]
            """常驻补跑循环：30s 一次补欠账，每天一次对账。

            这里是安全的：状态全在 `pending_task` 表里，
            进程被杀只是少跑一个 tick，下次启动接着做。
            """
            import asyncio

            from memory.services.maintenance import MaintenanceWorker

            worker = MaintenanceWorker(cfg.MEMORY_NAMESPACE, cfg)
            task = asyncio.create_task(worker.run_forever())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="amarantine memory admin",
        version="0.1.0",
        docs_url=None,      # 不给未认证的 Swagger
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def _namespace(requested: str | None = None) -> str:
        """确定要操作的 namespace。

        优先级：显式请求参数 > `MEMORY_NAMESPACE` 配置。
        多 namespace 场景下人可以带 `?namespace=personal` 跨域操作，
        单 namespace 部署则什么都不用填。
        """
        ns = (requested or "").strip() or cfg.MEMORY_NAMESPACE
        if not ns:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="未指定 namespace：请在 query 里带 namespace，或配置 MEMORY_NAMESPACE",
            )
        return ns

    def require_token(
        creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> None:
        expected = cfg.ADMIN_TOKEN
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ADMIN_TOKEN 未配置，管理接口已禁用",
            )
        if creds is None or creds.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="需要 Bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # 定长比较，避免时序侧信道
        if not _constant_time_eq(creds.credentials, expected):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="token 不正确"
            )

    # ------------------------------------------------------------ health

    @app.get("/health")
    def health() -> dict[str, Any]:
        """不需要认证：给探活用。只报「活着」，不报内部状态。"""
        return {"status": "ok", "service": "amarantine-memory-admin"}

    # ------------------------------------------------------------ review

    @app.get(
        "/admin/review",
        response_model=ReviewResponse,
        dependencies=[Depends(require_token)],
    )
    def list_review(
        bucket: str = Query(default=BUCKET_NEED_REVIEW),
        limit: int = Query(default=50, ge=1, le=500),
        namespace: str | None = Query(default=None),
    ) -> ReviewResponse:
        """列出某个桶的卡（ADR 4.6）。

        P0 从 Weaviate 实时拉全量再分类 —— 数据量小，先不做游标分页；
        `limit` 只是截断展示。
        """
        if bucket not in ALLOWED_BUCKETS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"bucket 必须是 {ALLOWED_BUCKETS} 之一",
            )

        ns = _namespace(namespace)
        rows = _load_all(cfg, ns)
        report = sweep(
            rows,
            today=date.today(),
            idle_days=cfg.FORGET_IDLE_DAYS,
            min_recalls=cfg.FORGET_MIN_RECALLS,
            grace_days=cfg.FORGET_GRACE_DAYS,
            protect_importance=cfg.PROTECT_IMPORTANCE_REVIEW,
        )
        picked = report.by_bucket(bucket)
        summaries = {r.event_id: r.summary for r in rows}
        items = [
            ReviewItem(
                event_id=v.event_id,
                bucket=v.bucket,
                reason=v.reason,
                idle_days=v.idle_days,
                age_days=v.age_days,
                summary=summaries.get(v.event_id),
            )
            for v in picked[:limit]
        ]
        return ReviewResponse(bucket=bucket, items=items, total=len(picked))

    @app.post(
        "/admin/review/{event_id}/resolve",
        response_model=ReviewVerdictResponse,
        dependencies=[Depends(require_token)],
    )
    def resolve_review(
        event_id: str, body: ReviewVerdictRequest
    ) -> ReviewVerdictResponse:
        """裁决一条 NEED_REVIEW（D5）：留还是删。

        语义（照 ADR 4.6）：

        - `keep`   → 归入 PROTECTED，等价人工钉住（`pinned=true`,
          `pinned_source=manual`）
        - `delete` → 归入 SAFE_TO_DELETE，**不立即删**；真正删除走 4.7 的扫描

        **幂等**：重复提交同一 action 不报错，`unchanged=true` 说明这次没改动状态。
        重复提交**相反** action 是允许的 —— 人会改主意，改主意不该被拒绝。

        为什么 `delete` 不能推翻 `pinned` / `unresolved`：
        那是 `forget_policy` 的事（判定顺序），这里只负责落库。
        一次误点不该能删掉受硬保护的卡。
        """
        ns = _namespace(body.namespace)
        rows = _load_all(cfg, ns)
        target = next((r for r in rows if r.event_id == event_id), None)
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"找不到这张卡：{event_id}",
            )

        # 裁决前先算当前桶，供响应里做前后对照
        before = classify(
            target,
            today=date.today(),
            idle_days=cfg.FORGET_IDLE_DAYS,
            min_recalls=cfg.FORGET_MIN_RECALLS,
            grace_days=cfg.FORGET_GRACE_DAYS,
            protect_importance=cfg.PROTECT_IMPORTANCE_REVIEW,
        )

        unchanged = target.review_verdict == body.action
        store = WeaviateStore(cfg)
        try:
            store.set_review_verdict(
                event_id,
                ns,
                verdict=body.action,
                reviewer=body.reviewer,
                # keep 才动 pinned；delete 不动，免得审计对不上
                pinned=True if body.action == "keep" else None,
            )
        except WeaviateStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"写入裁决失败: {exc}",
            ) from exc
        finally:
            store.close()

        # 用裁决后的状态重算一遍，保证返回的 bucket_after 是真值
        after_target = replace(
            target,
            review_verdict=body.action,
            pinned=True if body.action == "keep" else target.pinned,
        )
        after = classify(
            after_target,
            today=date.today(),
            idle_days=cfg.FORGET_IDLE_DAYS,
            min_recalls=cfg.FORGET_MIN_RECALLS,
            grace_days=cfg.FORGET_GRACE_DAYS,
            protect_importance=cfg.PROTECT_IMPORTANCE_REVIEW,
        )
        logger.info(
            "人工裁决 | event_id=%s action=%s before=%s after=%s reviewer=%s unchanged=%s",
            event_id,
            body.action,
            before.bucket,
            after.bucket,
            body.reviewer,
            unchanged,
        )
        return ReviewVerdictResponse(
            event_id=event_id,
            bucket_before=before.bucket,
            bucket_after=after.bucket,
            deleted=False,  # ADR：delete 只归档，不立即删
            unchanged=unchanged,
        )

    # ------------------------------------------------------ label review（B5）

    @app.get(
        "/label/vocab",
        response_model=VocabResponse,
        dependencies=[Depends(require_token)],
    )
    def get_label_vocab(
        version: str | None = Query(
            default=None,
            description="按版本回溯（如 v1-a1b2c3d4）；不填返回当前生效词表",
        ),
    ) -> VocabResponse:
        """查词表，以及某版词表长什么样（A23）。

        **为什么需要它**：词表是代码常量。改过之后，历史卡的 `types`
        就没人知道是按哪版打的 —— 而标签体系的价值全在可回溯。

        不带 `version` → 当前生效词表 + 它的版本号（顺带登记新版本）；
        带 `version` → 那一版的词表原文，不存在则 404。
        """
        registry = VocabRegistry(db_path_from_settings(cfg))
        if version is None:
            current = registry.current(cfg)
            vocab = effective_vocab(cfg)
            return VocabResponse(version=current, vocab=vocab, count=len(vocab))

        vocab = registry.get(version)
        if vocab is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"词表版本不存在: {version}",
            )
        return VocabResponse(version=version, vocab=vocab, count=len(vocab))

    @app.get(
        "/admin/label-review",
        response_model=LabelReviewListResponse,
        dependencies=[Depends(require_token)],
    )
    def list_label_review(
        limit: int = Query(default=50, ge=1, le=500),
        namespace: str | None = Query(default=None),
    ) -> LabelReviewListResponse:
        """列出待复核的**打标分歧**（B5）。

        注意这和 `GET /admin/review?bucket=need_review` 不是一回事（A24）：
        那边问「该不该留」，这边问「打得对不对」。两个队列不合并。

        数据来自 `LABEL_REVIEW` 表（规则层与 LLM 层不一致时才入队），
        不需要实时扫 Weaviate，所以比 `/admin/review` 便宜。
        """
        ns = _namespace(namespace)
        queue = queue_from_settings(cfg)
        items = [
            LabelReviewEntry(
                event_id=it.event_id,
                summary=it.summary,
                rules_types=it.rules_types,
                llm_types=it.llm_types,
                agreement=it.agreement,
                created_at=it.created_at,
            )
            for it in queue.list_pending(ns, limit=limit)
        ]
        return LabelReviewListResponse(items=items, total=queue.count_pending(ns))

    @app.post(
        "/admin/label-review/{event_id}/resolve",
        response_model=LabelReviewResolveResponse,
        dependencies=[Depends(require_token)],
    )
    def resolve_label_review(
        event_id: str, body: LabelReviewResolveRequest
    ) -> LabelReviewResolveResponse:
        """把一条打标分歧标记为已处理。

        **只标记，不改卡**。理由：`types` 参与 `event_id` 哈希（D1），
        改标签等于换一张卡 —— 那需要重新走写入链路，不是这里该做的事。
        这里记录「人看过并做了决定」，真正的修正由调用方重新 remember。
        """
        ns = _namespace(body.namespace)
        queue = queue_from_settings(cfg)
        ok = queue.resolve(ns, event_id, resolution=body.resolution)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"没有待复核的打标分歧：{event_id}（可能已处理或从未入队）",
            )
        return LabelReviewResolveResponse(
            event_id=event_id, resolved=True, resolution=body.resolution
        )

    # ------------------------------------------------------ 别名拒绝留痕
    #
    # 别名声明从原话到图上一条边有两道判据，两侧都可能被拒。原来两边
    # 都不留痕迹 —— `印度人`、`科学家`、`种师道` 三次都是这么藏起来的。
    # 这两个接口把「为什么那条边没建」变成可以查、可以裁决的事。

    @app.get(
        "/admin/alias-rejects",
        response_model=AliasRejectListResponse,
        dependencies=[Depends(require_token)],
    )
    def list_alias_rejects(
        limit: int = Query(default=50, ge=1, le=500),
        reason: str | None = Query(default=None),
        namespace: str | None = Query(default=None),
    ) -> AliasRejectListResponse:
        """列出被拒的别名声明。

        `by_reason` 是最有用的一栏：某个 reason 数量突然涨起来，
        通常意味着判据把一整类名字误伤了（`科学家` 就是这么被发现的）。
        """
        from memory.services.alias_reject import queue_from_settings as reject_q

        ns = _namespace(namespace)
        queue = reject_q(cfg)
        items = [
            AliasRejectEntry(
                event_id=it.event_id,
                alias=it.alias,
                canonical=it.canonical,
                side=it.side,
                reason=it.reason,
                detail=it.detail,
                source_text=it.source_text,
                created_at=it.created_at,
                resolution=it.resolution,
            )
            for it in queue.list_pending(ns, limit=limit, reason=reason)
        ]
        return AliasRejectListResponse(
            items=items,
            total=queue.count_pending(ns, reason=reason),
            by_reason=queue.stats(ns),
        )

    @app.post(
        "/admin/alias-rejects/resolve",
        response_model=AliasRejectResolveResponse,
        dependencies=[Depends(require_token)],
    )
    def resolve_alias_reject(
        body: AliasRejectResolveRequest,
    ) -> AliasRejectResolveResponse:
        """裁决一条被拒的别名声明。

        `accepted` = **判据错了**，这条是真名字 → 顺手把边补建出来。
        补建走 `repair_accepted_edge()`，它 `force=True` 绕过判据：
        否则同一道判据会把边再拦一次，人否决了判据、图上却仍然没边。

        **先补图、后落库**，所以「补建失败」本身是个可查的状态：
        失败记 `accept_pending`，该行继续留在待办里可以重试（`MERGE` 幂等），
        绝不假装成功。

        `dismissed` / `deferred` 一律不碰图。
        """
        from memory.services.alias_apply import repair_accepted_edge
        from memory.services.alias_reject import queue_from_settings as reject_q

        ns = _namespace(body.namespace)
        repaired: bool | None = None
        effective = body.resolution

        if body.resolution == "accepted":
            repaired = repair_accepted_edge(
                namespace=ns,
                event_id=body.event_id,
                alias=body.alias,
                canonical=body.canonical,
                settings=cfg,
            )
            if not repaired:
                # 图写失败：不要记 accepted，否则这行会沉底，人以为修好了
                effective = "accept_pending"

        ok = reject_q(cfg).resolve(
            ns,
            event_id=body.event_id,
            alias=body.alias,
            canonical=body.canonical,
            side=body.side,
            resolution=effective,
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="没有待处理的别名拒绝记录（可能已处理或从未入队）",
            )
        return AliasRejectResolveResponse(
            resolved=True, resolution=effective, repaired=repaired
        )

    @app.get(
        "/admin/alias-surnames",
        response_model=SurnameListResponse,
        dependencies=[Depends(require_token)],
    )
    def list_alias_surnames(
        namespace: str | None = Query(default=None),
    ) -> SurnameListResponse:
        """列出已声明的姓氏（`这人姓种`）。

        `active` 是**进程内当前生效的**（含启动时从表里灌的）——
        判据只认这个集合，所以它是「现在到底放过了什么」的真答案。
        """
        from memory.services.labeler import declared_surnames
        from memory.services.surname_decl import registry_from_settings

        ns = _namespace(namespace)
        items = [
            SurnameEntry(
                char=d.char,
                evidence=d.evidence,
                event_id=d.event_id,
                created_at=d.created_at,
            )
            for d in registry_from_settings(cfg).list_active(ns)
        ]
        return SurnameListResponse(
            items=items, active=sorted(declared_surnames())
        )

    @app.post(
        "/admin/alias-surnames",
        response_model=SurnameListResponse,
        dependencies=[Depends(require_token)],
    )
    def declare_alias_surname(
        body: SurnameDeclareRequest,
    ) -> SurnameListResponse:
        """声明姓氏。立即在**本进程**生效，并落库供下次启动灌回。"""
        from memory.services.labeler import declared_surnames, register_surnames
        from memory.services.surname_decl import registry_from_settings

        ns = _namespace(body.namespace)
        registry = registry_from_settings(cfg)
        fresh = registry.declare(
            ns, list(body.chars), evidence=body.evidence, event_id=None
        )
        register_surnames(body.chars)
        logger.info("通过管理接口声明姓氏 %s（新增 %d）", "".join(body.chars), fresh)
        items = [
            SurnameEntry(
                char=d.char,
                evidence=d.evidence,
                event_id=d.event_id,
                created_at=d.created_at,
            )
            for d in registry.list_active(ns)
        ]
        return SurnameListResponse(
            items=items, active=sorted(declared_surnames())
        )

    # ------------------------------------------------------------ forget

    @app.post(
        "/admin/forget",
        response_model=ForgetResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_forget(body: ForgetRequest) -> ForgetResponse:
        """手工遗忘一张卡（软删）。"""
        if body.reason not in ALLOWED_REASONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"reason 必须是 {ALLOWED_REASONS} 之一",
            )
        try:
            result = forget_event(
                namespace=_namespace(body.namespace),
                event_id=body.event_id,
                reason=body.reason,
                settings=cfg,
            )
        except ForgetError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        return ForgetResponse(
            event_id=result.event_id,
            forgotten=result.forgotten,
            reason=result.reason,
        )

    # ------------------------------------------------- entity edit

    def _edit_response(result: EditResult) -> EntityEditResponse:
        if not result.ok:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=result.error
            )
        return EntityEditResponse(
            ok=result.ok,
            op=result.op,
            detail=result.detail,
            error=result.error,
        )

    @app.post(
        "/admin/entities/rename",
        response_model=EntityEditResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_rename_entity(body: EntityRenameRequest) -> EntityEditResponse:
        """改名。**无条件成功** —— `key` 是身份，`name` 只是显示名。

        改完如果有另一个实体也叫 `to_name`，那是合法的重名状态，
        各有各的 key，查询时都显示原文。
        """
        return _edit_response(
            rename_entity(
                _namespace(body.namespace),
                body.from_name,
                body.to_name,
                settings=cfg,
                reason=body.reason,
            )
        )

    @app.post(
        "/admin/entities/merge",
        response_model=EntityEditResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_merge_entities(body: EntityMergeRequest) -> EntityEditResponse:
        """合并。边搬过去，被并的降级成 `(:Alias)`，**节点不删**。"""
        return _edit_response(
            merge_entities(
                _namespace(body.namespace),
                body.from_name,
                body.to_name,
                settings=cfg,
                reason=body.reason,
                label=body.label,
            )
        )

    @app.post(
        "/admin/entities/split",
        response_model=EntityEditResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_split_entity(body: EntitySplitRequest) -> EntityEditResponse:
        """拆分。原节点标 `split_into` 留痕，边两边都连并标 `from_split`。"""
        return _edit_response(
            split_entity(
                _namespace(body.namespace),
                body.from_name,
                body.into,
                settings=cfg,
                reason=body.reason,
            )
        )

    @app.post(
        "/admin/entities/rebind",
        response_model=EntityEditResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_rebind_entity(body: EntityRebindRequest) -> EntityEditResponse:
        """解挂重挂：**只改这一条事件**的指向。

        `三哥主打的就是一个有声有色有味` → 这个「三哥」其实是印度人。
        别名层不动（`三哥` 确实可以指哥哥），旧节点也不删。
        """
        return _edit_response(
            rebind_event_entity(
                _namespace(body.namespace),
                body.event_id,
                body.from_name,
                body.to_name,
                settings=cfg,
                to_kind=body.to_kind,
                reason=body.reason,
            )
        )

    @app.get(
        "/admin/entities/edits",
        response_model=EntityEditListResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_list_entity_edits(
        namespace: str | None = Query(default=None),
        op: str | None = Query(default=None),
        target: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> EntityEditListResponse:
        """查纠错痕迹（改 / 拆 / 合 / 解挂），按时间倒序。只读。

        「纠错是修正，不是抹除痕迹」—— 光有写接口不算数，痕迹得能查。
        `op` ∈ {rename, merge, split, rebind}，`target` 是当时操作的实体名。
        """
        from memory.services.entity_edit import list_edits

        result = list_edits(
            _namespace(namespace),
            settings=cfg,
            op=op,
            target=target,
            limit=limit,
        )
        return EntityEditListResponse(
            items=[EntityEditEntry(**i) for i in result["items"]],
            total=result["total"],
        )

    # ------------------------------------------------------- aliases

    @app.post(
        "/admin/aliases/resolve",
        response_model=AliasResolveResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_resolve_aliases(body: AliasResolveRequest) -> AliasResolveResponse:
        """对存疑的别名声明跑 LLM 推断。

        `糖包就是那个AI里最弱智的` 这类右边是描述的，规则层标 `uncertain`
        且不建边。这里让 LLM 从**图上已有的对象**里找「最弱智的AI」是谁。

        单卡模式（给了 `event_id`）返单条报告；否则扫命名空间。
        """
        ns = _namespace(body.namespace)
        if body.event_id:
            report = resolve_event_aliases(
                ns,
                body.event_id,
                settings=cfg,
                timeout_s=body.timeout_s,
            )
            return AliasResolveResponse(
                mode="event", items=[report.to_dict()], total=1
            )

        reports = resolve_namespace_aliases(
            ns,
            settings=cfg,
            timeout_s=body.timeout_s,
            limit=body.limit,
        )
        return AliasResolveResponse(
            mode="namespace",
            items=[r.to_dict() for r in reports],
            total=len(reports),
        )

    # ------------------------------------------------------------ sweep

    @app.post(
        "/admin/forget/sweep",
        response_model=SweepResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_sweep(body: SweepRequest) -> SweepResponse:
        """遗忘扫描报告。

        **A12：P0 传 dry_run=false 必须返回 400** —— 误删率闸门未过之前
        不允许任何自动删除。
        """
        if not body.dry_run:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="P0 只支持 dry_run=true：误删率闸门（A12）未通过前不允许自动删除",
            )

        ns = _namespace(body.namespace)
        rows = _load_all(cfg, ns)[: body.limit]

        # A10 第 4 类硬信号：图结构位置。
        # 图不可用时 event_degrees 返回 None —— 一律按 degree=0 继续，
        # 但要在响应里标 graph_degraded，不能让「少了一类保护」静默通过。
        degrees = event_degrees(
            [c.event_id for c in rows], ns, settings=cfg
        )
        graph_degraded = degrees is None
        if degrees is not None:
            rows = [
                replace(c, graph_degree=degrees.get(c.event_id, 0)) for c in rows
            ]

        report = sweep(
            rows,
            today=date.today(),
            idle_days=cfg.FORGET_IDLE_DAYS,
            min_recalls=cfg.FORGET_MIN_RECALLS,
            grace_days=cfg.FORGET_GRACE_DAYS,
            protect_importance=cfg.PROTECT_IMPORTANCE_REVIEW,
            graph_hub_degree=cfg.GRAPH_HUB_DEGREE,
            confirmed_min=cfg.CONFIRMED_MIN,
        )
        summaries = {r.event_id: r.summary for r in rows}
        # 报告只抽样 SAFE_TO_DELETE，供人工判断误删率
        sample = [
            ReviewItem(
                event_id=v.event_id,
                bucket=v.bucket,
                reason=v.reason,
                idle_days=v.idle_days,
                age_days=v.age_days,
                summary=summaries.get(v.event_id),
            )
            for v in report.by_bucket(BUCKET_SAFE_TO_DELETE)
        ]
        return SweepResponse(
            buckets=report.buckets,
            dry_run=True,
            sample=sample,
            graph_degraded=graph_degraded,
        )

    # ------------------------------------------------- events（4.6 / 4.7 / 4.4）

    def _event_store() -> WeaviateStore:
        # 每次新开一个：管理接口是低频人工操作，不值得维护长连接池，
        # 而且用完就 close 能让「忘了关」这类问题当场暴露。
        return WeaviateStore(cfg)

    @app.patch(
        "/admin/event/{event_id}",
        response_model=EventPatchResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_patch_event(
        event_id: str, body: EventPatchRequest
    ) -> EventPatchResponse:
        """改 `pinned` / `retention`（ADR 4.6）。

        **幂等**：值没变就不写，`updated_at` 保持不变。这不是省事 ——
        如果每次都写，审计里「这条被改过几次」就永久失真了。

        `pinned=true` 同时写 `pinned_source="manual"`：缺了它就没法区分
        人工钉住和规则钉住，而 D7 的保护逻辑要靠这个区分。
        解钉时**不动** `pinned_source`，保留「曾被谁钉过」的痕迹。
        """
        ns = _namespace(body.namespace)
        extra_keys = set(body.model_extra or {}) - {"namespace"}
        if extra_keys:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"这些字段不允许直接改: {sorted(extra_keys)}；"
                    "只能改 pinned / retention（改 summary/types 请走标签复核或重建）"
                ),
            )
        if body.pinned is None and body.retention is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="至少要改一个字段：pinned / retention",
            )

        store = _event_store()
        try:
            current = store.get_by_event_id(ns, event_id)
            if current is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"事件卡不存在: {event_id}",
                )

            props: dict[str, Any] = {}
            if body.pinned is not None and bool(current.get("pinned")) != body.pinned:
                props["pinned"] = body.pinned
                if body.pinned:
                    props["pinned_source"] = "manual"
            if (
                body.retention is not None
                and current.get("retention") != body.retention
            ):
                props["retention"] = body.retention

            if props:
                props["updated_at"] = datetime.now(timezone.utc).isoformat()
                try:
                    found = store.update_fields(event_id, ns, properties=props)
                except ValueError as exc:
                    # 白名单外字段：校验在发请求前，索引里不会留半截状态
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                    ) from exc
                if not found:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"事件卡不存在: {event_id}",
                    )
                current = store.get_by_event_id(ns, event_id) or {
                    **current,
                    **props,
                }

            return EventPatchResponse(
                event_id=event_id,
                pinned=current.get("pinned"),
                pinned_source=current.get("pinned_source"),
                retention=current.get("retention"),
                updated_at=current.get("updated_at"),
            )
        except WeaviateStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"索引层不可用: {exc}",
            ) from exc
        finally:
            store.close()

    @app.delete(
        "/admin/event/{event_id}",
        response_model=EventDeleteResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_delete_event(
        event_id: str, body: EventDeleteRequest | None = None
    ) -> EventDeleteResponse:
        """软删一条事件卡（ADR 4.7）。

        **`hard=true` 一律 400**。这不是「还没实现」—— D7 + A9 规定事实层
        永不物理删，所以它在入口就被关死，连降级路径都没有。

        **幂等**：已软删的直接返回原来的 `deleted_at`，不再写一次。
        否则每次重试刷新时间戳，审计里「什么时候删的」就永远是最后一次调用。
        """
        req = body or EventDeleteRequest()
        ns = _namespace(req.namespace)

        if req.hard:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="只允许软删：hard=true 被拒绝（D7/A9 事实层永不物理删）",
            )
        if req.reason not in ADMIN_REASONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"reason 必须是 {ADMIN_REASONS} 之一",
            )

        store = _event_store()
        try:
            current = store.get_by_event_id(ns, event_id)
            if current is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"事件卡不存在: {event_id}",
                )
            if current.get("is_deleted"):
                return EventDeleteResponse(
                    event_id=event_id, deleted_at=current.get("deleted_at")
                )

            forget_event(
                namespace=ns,
                event_id=event_id,
                reason=req.reason,
                settings=cfg,
                store=store,
            )
            after = store.get_by_event_id(ns, event_id) or {}
            return EventDeleteResponse(
                event_id=event_id, deleted_at=after.get("deleted_at")
            )
        except ForgetError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        except WeaviateStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"索引层不可用: {exc}",
            ) from exc
        finally:
            store.close()

    @app.post(
        "/admin/archive",
        response_model=ArchiveResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_archive(body: ArchiveRequest) -> ArchiveResponse:
        """热冷逻辑分层报告（ADR 4.4）。

        **P0 一条都不搬**：不管传什么 `dry_run`，`moved` 恒 0、`mode` 恒
        `logical`。先让「有多少卡进入冷区」这个数字可见，再决定何时开搬 ——
        自动搬迁和 `forget` 的 `dry_run=true` 强制是同一套谨慎逻辑。

        与 `forget` 的职责分离（4.7 硬约束）：`archive` 按**时间**分层，
        `forget` 按**空闲 + 召回收**淘汰三桶。两者不共用一个 service。
        """
        ns = _namespace(body.namespace)
        if body.window_days < 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="window_days 必须 >= 1",
            )

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=body.window_days)
        ).isoformat()
        store = _event_store()
        try:
            candidates, scanned = store.count_older_than(ns, cutoff=cutoff)
        except WeaviateStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"索引层不可用: {exc}",
            ) from exc
        finally:
            store.close()

        return ArchiveResponse(
            window_days=body.window_days,
            candidates=candidates,
            scanned=scanned,
        )

    # ------------------------------------------------- recall 调试（4.9）

    @app.post(
        "/admin/recall",
        response_model=AdminRecallResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_recall(body: AdminRecallRequest) -> AdminRecallResponse:
        """管理侧召回（调试用）。与 MCP `memory_recall` 走同一套 service。

        **必须带 `trace_store`**：不传就不产 `trace_id`，
        下游 `/admin/recall/{trace_id}/feedback` 永远关联不上 ——
        这是 4.9 两个接口唯一的耦合点。

        存储初始化容错（与 MCP 侧同策略）：trace 是辅助数据，
        账本写不了不该连累「查记忆」这件正事。
        """
        ns = _namespace(body.namespace)
        if not body.query or not body.query.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="query 必须是非空字符串",
            )

        try:
            trace_store = trace_store_from_settings(cfg)
        except Exception as exc:  # noqa: BLE001 - 放弃 feedback 能力，召回照常
            logger.warning("引用凭据存储不可用，本次召回不带 trace_id: %s", exc)
            trace_store = None

        try:
            result = recall(
                body.query,
                namespace=ns,
                settings=cfg,
                trace_store=trace_store,
                budget_ms=body.budget_ms,
            )
        except RecallUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"事实源不可用: {exc}",
            ) from exc
        return AdminRecallResponse(**result.to_payload())

    @app.post(
        "/admin/recall/{trace_id}/feedback",
        response_model=RecallFeedbackResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_recall_feedback(
        trace_id: str, body: RecallFeedbackRequest
    ) -> RecallFeedbackResponse:
        """回报本次引用了哪些材料（ADR 4.9 / C.B6）。

        `confirmed_by_use` 的**唯一写入通道** —— 系统不猜「提到」还是「用了」。
        幂等键 `(trace_id, event_id)`，重复上报只计一次。

        trace 不存在 → **404**（调用方传错了 id）；
        域不匹配 / id 不在返回集里 → **400**（请求本身有问题）。
        两者必须分开，否则调用方分不清「重试」还是「改参数」。
        """
        ns = _namespace(body.namespace)
        try:
            outcome = submit_feedback(
                namespace=ns,
                trace_id=trace_id,
                used_material_ids=body.used_material_ids,
                settings=cfg,
            )
        except TraceNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        except FeedbackError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        return RecallFeedbackResponse(
            trace_id=trace_id,
            accepted=outcome.accepted,
            deduped=outcome.deduped,
            incremented=list(outcome.incremented or []),
            rejected=list(outcome.rejected or []),
        )

    # ------------------------------------------------- 原文层（4.2 / 4.8）

    @app.post(
        "/admin/ingest",
        response_model=IngestResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_token)],
    )
    def admin_ingest(
        body: IngestRequest, background: BackgroundTasks, response: Response
    ) -> IngestResponse:
        """批量导入历史对话（ADR 4.2）。

        三件事按顺序发生，缺一件都会让 202 变成黑洞：

        1. **原文先同步落库**：202 返回时 `GET /raw/{quote_id}` 就该能查到。
           如果等后台再写，调用方拿到 job_id 的那一秒是查不到原文的。
        2. **dedupe_key 幂等**：同值再提交返回**已有 job**，不重跑 ——
           重跑会把 LLM 切分结果变一遍，同一批对话产出不同的卡。
        3. **整批 do_not_store 走 200 rejected**：`job_id` 为 null，
           因为它压根不会执行（ADR 4.2 的响应语义）。
        """
        ns = _namespace(body.namespace)
        dedupe_key = (body.dedupe_key or "").strip()
        if not dedupe_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="dedupe_key 不能为空（客户端靠它防重复提交）",
            )
        if not body.turns:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="turns 不能为空"
            )

        normalized = [normalize_turn(t.model_dump()) for t in body.turns]
        valid = [t for t in normalized if t is not None]
        invalid = len(body.turns) - len(valid)
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="没有一条合法的 turn（需要非空 text + 合法 role）",
            )

        job_store = IngestJobStore(db_path_from_settings(cfg))
        existing = job_store.find_by_dedupe(ns, dedupe_key)
        if existing is not None:
            # 幂等：连状态一起回，调用方不用猜上一次跑完没有
            return IngestResponse(
                job_id=existing.job_id,
                status=existing.status,
                events_created=existing.events_created,
                turns_total=existing.turns_total,
                turns_skipped=existing.turns_skipped,
            )

        usable = [t for t in valid if not t["do_not_store"]]
        skipped = (len(valid) - len(usable)) + invalid

        if not usable:
            job_store.create(
                namespace=ns,
                dedupe_key=dedupe_key,
                source=body.source,
                session_id=body.session_id,
                turns_total=len(valid),
                turns_skipped=skipped,
                status=JOB_REJECTED,
                error="do_not_store",
            )
            # ADR 4.2：整批被拦是 **200**，不是 202 —— 它根本不会执行
            response.status_code = status.HTTP_200_OK
            return IngestResponse(
                job_id=None,
                status=JOB_REJECTED,
                reason="do_not_store",
                turns_total=len(valid),
                turns_skipped=skipped,
            )

        raw_store = RawQuoteStore(db_path_from_settings(cfg))
        quote_ids: list[str] = []
        for turn in usable:
            qid = quote_id_for(
                namespace=ns,
                source=body.source,
                session_id=body.session_id,
                ts=turn["ts"],
                text=turn["text"],
            )
            raw_store.put(
                namespace=ns,
                quote_id=qid,
                text=turn["text"],
                role=turn["role"],
                source=body.source,
                ts=turn["ts"],
                session_id=body.session_id,
            )
            quote_ids.append(qid)

        job = job_store.create(
            namespace=ns,
            dedupe_key=dedupe_key,
            source=body.source,
            session_id=body.session_id,
            turns_total=len(valid),
            turns_skipped=skipped,
            status=JOB_QUEUED,
        )
        background.add_task(
            run_ingest,
            job_id=job.job_id,
            namespace=ns,
            turns=usable,
            quote_ids=quote_ids,
            settings=cfg,
        )
        return IngestResponse(
            job_id=job.job_id,
            status=job.status,
            turns_total=job.turns_total,
            turns_skipped=job.turns_skipped,
        )

    @app.get(
        "/admin/ingest/{job_id}",
        response_model=IngestJobResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_ingest_job(job_id: str) -> IngestJobResponse:
        """查导入进度（ADR 未定义，但 `202 {job_id}` 之后必须有它）。

        没有这个接口，`202` 就只是「我收到了」四个字 ——
        调用方永远不知道那批对话到底进去没有。
        """
        job = IngestJobStore(db_path_from_settings(cfg)).get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在: {job_id}"
            )
        return IngestJobResponse(**job.to_dict())

    @app.get(
        "/admin/raw/{quote_id}",
        response_model=RawQuoteResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_raw(quote_id: str, namespace: str | None = None) -> RawQuoteResponse:
        """按 quote_id 取回原文（ADR 4.8）。

        **软删事件卡后仍返回 200**（A32）—— `raw_quote` 没有删除路径，
        所以这里不需要任何 `is_deleted` 判断。不是漏了，是设计如此。

        `event_ids` 靠扫 Weaviate 反查（不建反向索引，理由见 005 Notes）。
        """
        ns = _namespace(namespace)
        raw = RawQuoteStore(db_path_from_settings(cfg)).get(ns, quote_id)
        if raw is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"原文不存在: {quote_id}"
            )

        store = _event_store()
        lookup_ok = True
        rows: list = []
        try:
            rows = store.list_all(ns, limit=_RAW_SCAN_LIMIT)
        except WeaviateStoreError as exc:
            # 原文是事实层，反查挂了不该把原文也藏起来（A32 的精神）；
            # 但**更不能返回空 event_ids 假装「这张卡没生成过事件」** ——
            # 所以降级成 `lookup_ok=false`，让「不知道」和「确定没有」分得开。
            logger.warning("原文反查事件卡失败（lookup_ok=false）: %s", exc)
            lookup_ok = False
        finally:
            store.close()

        event_ids = [
            str(r.get("event_id"))
            for r in rows
            if quote_id in (r.get("raw_quote_ids") or [])
        ]
        payload = raw.to_dict()
        payload.pop("created_at", None)
        return RawQuoteResponse(
            **payload,
            event_ids=event_ids,
            scanned=len(rows),
            truncated=len(rows) >= _RAW_SCAN_LIMIT,
            lookup_ok=lookup_ok,
        )

    @app.get(
        "/admin/events",
        response_model=EventListResponse,
        dependencies=[Depends(require_token)],
    )
    def admin_events(
        namespace: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> EventListResponse:
        """列出当前域的事件卡（分页，按 `date_start` 倒序）。

        「软删可写」的前提是**先能看见卡**。现有接口里只有 `/admin/review`
        （仅复核队列）和 `/admin/entities/edits`（仅纠错痕迹），
        没有一个「列出这个域的卡」的入口 —— 所以补这一个，只读。

        `list_all()` 本身已排除已软删的卡，这里不再过滤；
        否则「已删的看不见」这个行为会被实现两遍，将来改一处漏一处。
        """
        ns = _namespace(namespace)
        store = _event_store()
        try:
            rows = store.list_all(ns, limit=_EVENTS_SCAN_LIMIT)
        except WeaviateStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"事件库不可用: {exc}",
            ) from exc
        finally:
            store.close()

        # `Get` 不保证顺序，显式按日期倒序 —— 列表页最上面应该是最近的事
        rows.sort(key=lambda r: str(r.get("date_start") or ""), reverse=True)
        page = rows[offset : offset + limit]
        items = [
            EventListItem(
                event_id=str(r.get("event_id")),
                summary=str(r.get("summary") or ""),
                date_start=str(r.get("date_start") or ""),
                date_end=r.get("date_end"),
                types=[str(t) for t in (r.get("types") or [])],
                importance=str(r.get("importance") or ""),
                pinned=bool(r.get("pinned", False)),
                is_deleted=bool(r.get("is_deleted", False)),
                raw_quote_ids=[str(q) for q in (r.get("raw_quote_ids") or [])],
                recall_count=int(r.get("recall_count") or 0),
            )
            for r in page
        ]
        return EventListResponse(total=len(rows), items=items)

    # ------------------------------------------------- 管理台静态页（M5 / 006）

    if _WEB_DIR.is_dir():
        # `/ui` 下的 HTML 本身不鉴权 —— 它只是一个空壳，所有数据都要 token。
        # 把 token 挡在 HTML 外面没有意义（静态文件不含任何秘密），
        # 真正的边界在 `/admin/*` 的 `require_token` 上。
        app.mount("/ui", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")

    return app


# ---------------------------------------------------------------- 辅助


def _constant_time_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a.encode(), b.encode()):
        diff |= x ^ y
    return diff == 0


def _load_all(cfg: Settings, namespace: str) -> list[ForgetCandidate]:
    """从 Weaviate 拉当前 namespace 下未删除的卡，转成分类输入。

    `last_recalled_at` 不在 Weaviate schema 里（召回计数是派生数据，
    M4.2 特意放在独立 SQLite 表避免写放大），所以这里从 `RecallCounter` 补。
    """
    store = WeaviateStore(cfg)
    try:
        rows = store.list_all(namespace)
    except WeaviateStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"索引层不可用: {exc}",
        ) from exc
    finally:
        store.close()

    try:
        counter = RecallCounter(db_path_from_settings(cfg))
    except Exception:  # noqa: BLE001 - 计数表不可用不该让报告整体失败
        logger.warning("召回计数表不可用，last_recalled_at 将缺失", exc_info=True)
        counter = None

    out: list[ForgetCandidate] = []
    for r in rows:
        event_id = r["event_id"]
        count = int(r.get("recall_count") or 0)
        last_at = None
        if counter is not None:
            try:
                count, raw = counter.stats(namespace, event_id)
                last_at = _parse_date(raw)
            except Exception:  # noqa: BLE001
                pass
        out.append(
            ForgetCandidate(
                event_id=event_id,
                importance=float(r.get("importance") or 0.0),
                recall_count=count,
                created_at=_parse_date(r.get("date_start")),
                last_recalled_at=last_at,
                retention=str(r.get("retention") or "normal"),
                pinned=bool(r.get("pinned")),
                unresolved=list(r.get("unresolved") or []),
                # A10 第 5 类硬信号：卡上已存 `confirmed_by_use`，直接搬过来。
                # graph_degree 不在这里填 —— 它要查 Neo4j，属于 IO，
                # 由 sweep 端点按需批量查（见 admin_sweep）。
                confirmed_by_use=int(r.get("confirmed_by_use") or 0),
                review_verdict=r.get("review_verdict"),
                summary=r.get("summary"),
            )
        )
    return out


def _parse_date(raw: Any) -> date | None:
    if not raw:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ---------------------------------------------------------------- main


def main() -> int:
    """启动 Admin HTTP。失败只写 LOG_FILE 并退 1（与 MCP 入口同一契约）。"""
    logging.basicConfig(
        level=logging.INFO,
        filename=os.environ.get("LOG_FILE", "admin.log"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: 配置加载失败: {exc}")
        return 1

    if not settings.ADMIN_TOKEN:
        print("FATAL: ADMIN_TOKEN 未配置，拒绝启动管理接口")
        return 1

    app = create_app(settings, maintenance=True)

    # 用图上的已有名字预热 jieba 词典（与 MCP 入口同一逻辑）。
    # 不能每次写入后反哺 —— 那会改变 `entity_names`，进而改变 `event_id`，
    # 同一句话写两次会变成两条记忆。
    if settings.MEMORY_NAMESPACE:
        from memory.services.remember import warm_up_dictionary

        warmed = warm_up_dictionary(settings.MEMORY_NAMESPACE, settings=settings)
        logger.info("分词词典预热完成，新增 %d 个词", warmed)

        # 灌回已声明的姓氏（`这人姓种`）。jieba 未必认识生僻姓，
        # 但用户说过 —— 判据要靠这个才放过 `种小明` 这类名字。
        from memory.services.surname_decl import load_into_labeler

        surnames = load_into_labeler(settings)
        logger.info("姓氏声明加载完成，新增 %d 个", surnames)

        # 启动补跑：谁先起来谁补课（MCP 和 admin 各做一次，重复补是幂等的）
        # 结果由 `startup_recover` 自己记日志，这里不重复打。
        from memory.services.maintenance import startup_recover

        startup_recover(settings.MEMORY_NAMESPACE, settings=settings)

    logger.info("admin 启动 %s:%s", settings.ADMIN_HOST, settings.ADMIN_PORT)
    uvicorn.run(
        app,
        host=settings.ADMIN_HOST,
        port=settings.ADMIN_PORT,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
