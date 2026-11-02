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
from datetime import date, datetime, timezone
from dataclasses import replace
from typing import Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from memory.config import Settings, load_settings
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
from memory.services.forget import ALLOWED_REASONS, ForgetError, forget_event
from memory.services.label_review import LabelReviewQueue, queue_from_settings
from memory.services.recall_count import RecallCounter, db_path_from_settings
from memory.services.weaviate_store import WeaviateStore, WeaviateStoreError

logger = logging.getLogger("memory.admin")

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
        report = sweep(
            rows,
            today=date.today(),
            idle_days=cfg.FORGET_IDLE_DAYS,
            min_recalls=cfg.FORGET_MIN_RECALLS,
            grace_days=cfg.FORGET_GRACE_DAYS,
            protect_importance=cfg.PROTECT_IMPORTANCE_REVIEW,
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
        return SweepResponse(buckets=report.buckets, dry_run=True, sample=sample)

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
