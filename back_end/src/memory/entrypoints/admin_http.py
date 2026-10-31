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
from datetime import date, datetime, timezone
from typing import Any

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
    sweep,
)
from memory.services.forget import ALLOWED_REASONS, ForgetError, forget_event
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


# ---------------------------------------------------------------- 应用工厂


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用。settings 可注入（测试用）。"""
    cfg = settings or load_settings()

    app = FastAPI(
        title="amarantine memory admin",
        version="0.1.0",
        docs_url=None,      # 不给未认证的 Swagger
        redoc_url=None,
        openapi_url=None,
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

    app = create_app(settings)
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
