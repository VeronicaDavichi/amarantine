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


#: 别名置信度分级。
#:
#: **判据是「这句话里有几分显式的身份锚定」**：
#:
#: - ``exact``：用户明确说「A 就是 B」。直接等价声明，不需要推理。
#: - ``probable``：规则能对上（同姓 + `小X`/`老X`/`X总` 这类称呼变形）。
#: - ``uncertain``：要靠描述性内容去猜。``糖包就是那个 AI 里最弱智的``
#:   得先知道「最弱智的 AI」是谁，才能推出「糖包 = 豆包」—— 这一步会错。
#:
#: 召回时低置信度**必须带标记**，不能让用户以为那是板上钉钉的事实。
ALIAS_EXACT = "exact"
ALIAS_PROBABLE = "probable"
ALIAS_UNCERTAIN = "uncertain"

#: 各等级的数值权重（写进图的边属性，便于排序与阈值过滤）
ALIAS_CONFIDENCE: dict[str, float] = {
    ALIAS_EXACT: 1.0,
    ALIAS_PROBABLE: 0.7,
    ALIAS_UNCERTAIN: 0.4,
}

#: LLM 推断到什么把握才敢升到 `probable`。
#:
#: 低于这个值但 >= 0.5 的，仍然建边但标 `uncertain` —— 用户说得对：
#: 「低置信度返回的时候应当有标记」，不是不许召回，是不许伪装成事实。
ALIAS_PROBABLE_MIN = 0.8


class AliasClaim(BaseModel):
    """一条「A 也叫 B」的声明。

    **一个别名可能对应多个人**：``三哥`` 既能指印度人，也能指家里排行第三的
    男性。别名不能假设一对一 —— 图上的 `(:Alias)` 节点可以被多条 `ALIAS_OF`
    边指向，召回时发现多个主人就要标 `ambiguous`。

    **不参与 `event_id` 哈希**：理由同 `Action`，声明是派生标注。
    """

    model_config = ConfigDict(extra="forbid")

    #: 别名本身（`小张` / `糖包` / `三哥`）
    alias: str = Field(min_length=1)
    #: 它指向的那个更正式的名字（`张三` / `豆包`）
    canonical: str = Field(min_length=1)
    #: 置信度等级
    label: Literal["exact", "probable", "uncertain"] = ALIAS_UNCERTAIN
    #: 谁说的：`user_declared`（用户原话）/ `rule` / `llm_inferred`
    source: Literal["user_declared", "rule", "llm_inferred"] = "user_declared"


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
    #: 这张卡属于哪个会话（群 / 私聊）。spec 046：日常模式靠它回溯上文。
    #:
    #: **不进 `event_id` 哈希** —— 同一段话从群里搬到私聊，事件本身没变。
    session_id: str | None = None
    #: 消息的发送时间（spec 040）。**与事件时间不是一回事**：
    #: 10-30 发的消息可能在说「昨天开的会」，事件日是 10-29。
    msg_time: datetime | None = None
    #: 事件时间是怎么来的（spec 040）：
    #:
    #: - ``stated``   原话明确给了日期（10月28日 / 2026-10-28）
    #: - ``relative`` 相对时间按**消息时间**换算（昨天 / 上周）
    #: - ``msg_ts``  用消息发送时间当事件时间
    #: - ``imported`` 都判不出 → 用导入时间，并明确标出来
    time_source: Literal["stated", "relative", "msg_ts", "imported"] = "imported"
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

    # ---------- 别名声明的留痕 ----------
    #: 这句话里出现的「A 也叫 B」。**不参与 `event_id` 哈希**。
    #:
    #: 别名关系本身存在图上（`(:Person)-[:ALIAS_OF]->(:Alias)`），
    #: 这里留的是「哪句话说的」，用于追溯和撤销。
    #: 一个人可能有多种称呼，也可能一种称呼对应多个人 —— 都靠这张表兜着。
    alias_claims: list[AliasClaim] = Field(default_factory=list)

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
