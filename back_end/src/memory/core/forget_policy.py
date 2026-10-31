"""D7 遗忘策略：三桶分流的纯函数层。

ADR 的立场很硬：**LRU 只作用于派生层，事实层永不物理删除**（N4）。
所以这里的"遗忘"只是**分类**，不是删除——分类结果交给人工或后续流程。

三桶优先级（顺序即安全等级）：

1. **PROTECTED** —— 五类硬信号、宽限期、活跃条目
2. **SAFE_TO_DELETE** —— 淘汰条件同时满足
3. **NEED_REVIEW** —— importance 偏高但没被硬信号保护

两个容易写错的点：

- **A11 宽限期优先于淘汰**：新条目即使没被召回也不进删除桶，
  否则刚写入的卡会被立刻扫掉。
- **`importance` 不自动豁免**（D7）：importance 是写入时 LLM 的自评，
  不可靠，只能进 NEED_REVIEW 让人看。不能拿它做一票否决。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

#: 桶名（与 ADR 4.6/4.7 的返回结构一致）
BUCKET_PROTECTED = "PROTECTED"
BUCKET_NEED_REVIEW = "NEED_REVIEW"
BUCKET_SAFE_TO_DELETE = "SAFE_TO_DELETE"

#: retention 取值里视为"永不淘汰"的两档
PERMANENT_RETENTIONS = frozenset({"protected", "permanent"})


@dataclass(frozen=True)
class ForgetCandidate:
    """参与遗忘分类的一条事件卡。字段够用即可，不引入 ORM 模型。"""

    event_id: str
    importance: float = 0.0
    recall_count: int = 0
    created_at: date | None = None
    last_recalled_at: date | None = None
    retention: str = "normal"
    pinned: bool = False
    unresolved: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ForgetVerdict:
    """单条卡的分桶结论。`reason` 必须可解释，进周报。"""

    event_id: str
    bucket: str
    reason: str
    idle_days: int
    age_days: int

    @property
    def deletable(self) -> bool:
        return self.bucket == BUCKET_SAFE_TO_DELETE


@dataclass(frozen=True)
class SweepReport:
    """一次扫描的汇总。P0 只出报告，`dry_run=false` 直接拒绝。"""

    buckets: dict[str, int]
    verdicts: list[ForgetVerdict]
    dry_run: bool

    def by_bucket(self, bucket: str) -> list[ForgetVerdict]:
        return [v for v in self.verdicts if v.bucket == bucket]


def _days_between(later: date, earlier: date | None) -> int:
    if earlier is None:
        return 0
    return max(0, (later - earlier).days)


def classify(
    candidate: ForgetCandidate,
    *,
    today: date,
    idle_days: int,
    min_recalls: int,
    grace_days: int,
    protect_importance: float,
) -> ForgetVerdict:
    """把一条卡分进三桶之一。

    Args:
        candidate: 待分类的卡。
        today: 今天。
        idle_days: 空闲阈值（`FORGET_IDLE_DAYS`）。
        min_recalls: 召回次数阈值（`FORGET_MIN_RECALLS`）。
        grace_days: 宽限期（`FORGET_GRACE_DAYS`）。
        protect_importance: importance 灰区阈值（`PROTECT_IMPORTANCE_REVIEW`）。

    Returns:
        ForgetVerdict。
    """
    if idle_days < 0 or min_recalls < 0 or grace_days < 0:
        raise ValueError("idle_days / min_recalls / grace_days 不能为负")
    if not 0.0 <= protect_importance <= 1.0:
        raise ValueError(f"protect_importance 必须在 [0,1]：{protect_importance}")

    created = candidate.created_at or today
    age = _days_between(today, created)
    # 从没被召回过 → 空闲从创建那天算起
    idle = _days_between(today, candidate.last_recalled_at or created)

    def verdict(bucket: str, reason: str) -> ForgetVerdict:
        return ForgetVerdict(
            event_id=candidate.event_id,
            bucket=bucket,
            reason=reason,
            idle_days=idle,
            age_days=age,
        )

    # ---- 1) 五类硬信号 → 绝对保护 ----
    if candidate.pinned:
        return verdict(BUCKET_PROTECTED, "pinned")
    if candidate.retention in PERMANENT_RETENTIONS:
        return verdict(BUCKET_PROTECTED, f"retention={candidate.retention}")
    if candidate.unresolved:
        return verdict(BUCKET_PROTECTED, "unresolved")

    # ---- 2) 宽限期（A11）：新卡不进任何删除桶 ----
    # 注意这里**同时**要求 recall_count == 0：已经被召回过的新卡是活跃的，
    # 走后面的分支也一样安全，但这里提前返回能省一次判断。
    if age <= grace_days and candidate.recall_count == 0:
        return verdict(BUCKET_PROTECTED, "grace_period")

    # ---- 3) importance 灰区 → 人工看 ----
    # **必须在淘汰条件之前判**：否则 importance 高的卡会被直接扫进 SAFE_TO_DELETE，
    # 一个不可靠的自评分就沉默地导致删除——正是 D7 要避免的。
    # 注意这不是"豁免"（豁免=自动保留），而是升级为人工裁决。
    if candidate.importance >= protect_importance:
        return verdict(BUCKET_NEED_REVIEW, "high_importance")

    # ---- 4) 淘汰条件：四个必须**同时**满足 ----
    if (
        idle >= idle_days
        and candidate.recall_count < min_recalls
        and age > grace_days
        and candidate.retention == "normal"
    ):
        return verdict(BUCKET_SAFE_TO_DELETE, "idle_and_unrecalled")

    # ---- 5) 其余：活跃但不够老 → 保护 ----
    return verdict(BUCKET_PROTECTED, "active")


def sweep(
    candidates: list[ForgetCandidate],
    *,
    today: date,
    idle_days: int,
    min_recalls: int,
    grace_days: int,
    protect_importance: float,
    dry_run: bool = True,
) -> SweepReport:
    """批量分类，产出分桶报告。

    P0 阶段 `dry_run=False` 会被**拒绝**（A12：误删率闸门未过之前不允许自动删）。
    这里用 ValueError 表达，调用方映射成 HTTP 400。
    """
    if not dry_run:
        raise ValueError(
            "P0 不允许 dry_run=false：误删率闸门（A12）未通过前只出报告"
        )

    verdicts = [
        classify(
            c,
            today=today,
            idle_days=idle_days,
            min_recalls=min_recalls,
            grace_days=grace_days,
            protect_importance=protect_importance,
        )
        for c in candidates
    ]
    buckets = {BUCKET_PROTECTED: 0, BUCKET_NEED_REVIEW: 0, BUCKET_SAFE_TO_DELETE: 0}
    for v in verdicts:
        buckets[v.bucket] = buckets.get(v.bucket, 0) + 1
    return SweepReport(buckets=buckets, verdicts=verdicts, dry_run=True)
