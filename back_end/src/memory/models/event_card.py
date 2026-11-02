"""A2 EventCard 模型。

与 ADR 附录 B 的 JSON Schema 一一对应：
  - 25 个字段（8 必填 + 17 可选）
  - 约束（pattern / enum / ge / le）必须与 schema 完全一致

修改本文件后，务必同步 附录 B，并跑 tests/test_event_card.py 的 schema 对拍。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

NAMESPACE_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"
EVENT_ID_RE = r"^evt_[0-9a-f]{16}$"

#: resolves 列表元素的约束（schema 里 items 带 pattern）
EventId = Annotated[str, Field(pattern=EVENT_ID_RE)]


class Entity(BaseModel):
    """事件卡中的实体。kind 为受控枚举，禁止自由生成。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    kind: Literal["person", "topic", "place", "org", "event"]


class Action(BaseModel):
    """事件里的一个动作：谁对谁做了什么。

    `types` 是粗分类（decision / work），表达不了「张三对项目A做了延期」。
    动作拆出来才能在图上聚合「哪些事被延过」「谁总在决定」。

    **`primary` 的意义**：一句话常有多个动词
    （`张三决定项目A延期到六月` 有「决定」和「延期」），
    最外层那个才是事件的主干，其余是嵌套内容。全部保留，用 `primary` 标注主次。

    **不参与 `event_id` 哈希**：`event_id` 的 canonical 串只含
    `types` / `entity_names` / `summary`。动作是派生标注，让它参与哈希
    会导致打标一抖动就换 id —— 同一件事写成两张卡。
    """

    model_config = ConfigDict(extra="forbid")

    #: 归一化后的动词（同义词已归并，如 推迟/延后 → 延期）
    verb: str = Field(min_length=1)
    #: 宾语名（可以是 Person 或 Entity 的名字），可空
    patient: str | None = None
    #: 是否为事件的主干动作
    primary: bool = True


class EventCard(BaseModel):
    """事件卡：记忆的最小语义单元。"""

    model_config = ConfigDict(extra="forbid")

    # ---------- 必填（8） ----------
    namespace: str = Field(pattern=NAMESPACE_RE)
    event_id: str = Field(pattern=EVENT_ID_RE)
    date_start: date
    types: list[str] = Field(min_length=1)
    entities: list[Entity]
    summary: str = Field(min_length=1)
    retention: Literal["normal", "sensitive"]
    ingested_at: datetime

    # ---------- 可选（17） ----------
    date_end: date | None = None
    conclusion: str | None = None
    unresolved: list[str] = Field(default_factory=list)
    resolves: list[EventId] = Field(default_factory=list)
    emotion: str | None = None
    emotion_intensity: float | None = Field(default=None, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    importance_reason: str | None = None
    user_directive: Literal["remember", "important"] | None = None
    raw_quote_ids: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    observed_at: datetime | None = None
    pinned: bool = False
    pinned_source: Literal["manual", "user_directive"] | None = None
    recall_count: int = Field(default=0, ge=0)
    last_recalled_at: datetime | None = None
    confirmed_by_use: int = Field(default=0, ge=0)

    # ---------- 人工裁决留痕（D5 `POST /admin/review/{id}/resolve`） ----------
    #: 人对 NEED_REVIEW 的裁决。`None` = 还没人看过。
    #: `keep` → 视为绝对保护；`delete` → 归入 SAFE_TO_DELETE（**不立即删**）。
    review_verdict: Literal["keep", "delete"] | None = None
    #: 谁裁的，审计用。与 `pinned_source` 分开：那个是「为什么被钉」，
    #: 这个是「谁做的决定」。
    reviewer: str | None = None

    # ---------- 动作（细粒度语义） ----------
    #: 事件里的动作列表。`types` 是粗分类，这个才是「谁对谁做了什么」。
    #: **不参与 `event_id` 哈希** —— 详见 `Action` 的文档。
    actions: list[Action] = Field(default_factory=list)

    # ---------- 业务不变量（schema 表达不了的） ----------

    def check_invariants(self) -> None:
        """校验 JSON Schema 无法覆盖的跨字段约束。

        Raises:
            ValueError: 违反下列任一约束。
        """
        if self.date_end is not None and self.date_end < self.date_start:
            raise ValueError(
                f"date_end({self.date_end}) 不能早于 date_start({self.date_start})"
            )
        if self.pinned and self.pinned_source is None:
            raise ValueError("pinned=True 时必须给出 pinned_source")
        if not self.pinned and self.pinned_source is not None:
            raise ValueError("pinned=False 时不应出现 pinned_source")
        if self.pinned_source == "manual" and self.user_directive is not None:
            raise ValueError("manual 来源不应携带 user_directive")


#: 字段名清单，供测试与 schema 对拍使用
FIELD_NAMES: tuple[str, ...] = tuple(EventCard.model_fields)

#: 必填字段（与附录 B required 一致）
REQUIRED_FIELDS: tuple[str, ...] = (
    "namespace",
    "event_id",
    "date_start",
    "types",
    "entities",
    "summary",
    "retention",
    "ingested_at",
)
