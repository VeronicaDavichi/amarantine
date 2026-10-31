"""B3 参数校验与滥用护栏。

设计前提（ADR v0.11）：**意图判断上移给 LLM**，服务端不再正则解析「帮我记一下」。
但服务端**保留全部校验**，不信任 LLM 的自觉——这四件事必须自己做：

| 校验 | 规则 |
|---|---|
| 参数类型 | `pinned`/`important` 必须布尔；`emotion_intensity` 必须 0–1 |
| 保护语义 | 只有 `pinned=true` 进 PROTECTED；`important` 与 `emotion` 永不升 PROTECTED |
| 滥用护栏 | 24h 内 pinned 超 `USER_DIRECTIVE_DAILY_MAX` 则降级 |
| `llm_inferred` 禁令 | `pinned_source` 不接受 `llm_inferred`（D7 硬约束） |

护栏理由：若「记住」可无限使用，PROTECTED 桶会被灌爆，保护机制自我废止。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 超限降级时写入的 anomaly 标记（A33 要求可查）
ANOMALY_DIRECTIVE_ABUSE = "directive_abuse"

#: 唯一允许的 pinned 来源（D7：明确禁止 llm_inferred）
ALLOWED_PINNED_SOURCES = ("manual", "user_directive")


class GuardError(ValueError):
    """参数非法。调用方应转成 MCP 的 ``isError: true`` 返回，而不是抛给 host。"""


def validate_remember_args(
    *,
    summary: object,
    pinned: object,
    important: object,
    emotion_intensity: object,
) -> None:
    """严格校验 LLM 传进来的参数类型。

    为什么要手写而不全靠 pydantic：MCP 走 JSON-RPC，LLM 很容易把
    `true` 写成字符串 `"true"`、把强度写成整数 1。宽进会让护栏形同虚设。

    Raises:
        GuardError: 任一参数不合法。
    """
    if not isinstance(summary, str) or not summary.strip():
        raise GuardError("summary 必须是非空字符串")

    # bool 是 int 的子类，必须显式排除
    if not isinstance(pinned, bool):
        raise GuardError(f"pinned 必须是布尔值，收到 {type(pinned).__name__}")
    if not isinstance(important, bool):
        raise GuardError(f"important 必须是布尔值，收到 {type(important).__name__}")

    if isinstance(emotion_intensity, bool) or not isinstance(
        emotion_intensity, (int, float)
    ):
        raise GuardError(
            f"emotion_intensity 必须是数字，收到 {type(emotion_intensity).__name__}"
        )
    if not 0.0 <= float(emotion_intensity) <= 1.0:
        raise GuardError(f"emotion_intensity 必须在 [0, 1]，收到 {emotion_intensity}")

    # 幂等性前提：summary 不能只是空白
    if not summary.strip():
        raise GuardError("summary 不能是空白字符")


@dataclass(frozen=True)
class GuardDecision:
    """护栏裁决结果，供写卡直接使用。

    ``importance_floor`` 与 ``importance_boost`` 分开给出，
    由打分环节合并（B6），避免这里偷偷改分。
    """

    pinned: bool
    important: bool
    emotion_intensity: float
    pinned_source: str | None
    user_directive: str | None
    importance_floor: float | None
    importance_boost: float
    anomaly: str | None

    @property
    def downgraded(self) -> bool:
        return self.anomaly == ANOMALY_DIRECTIVE_ABUSE


def apply_directive_guards(
    *,
    pinned: bool,
    important: bool,
    emotion_intensity: float,
    pinned_in_window: int,
    daily_max: int,
    explicit_important_floor: float,
    emotion_boost: float,
) -> GuardDecision:
    """应用 C.B13 的三类语言信号与滥用护栏。

    Args:
        pinned: LLM 判断用户是否**显式**要求记住。
        important: 用户是否强调「这很重要」。
        emotion_intensity: 用户情绪强度 0–1。
        pinned_in_window: 该 namespace 过去 24h 内已 pin 的次数（含本次之前的）。
        daily_max: `USER_DIRECTIVE_DAILY_MAX`。
        explicit_important_floor: `EXPLICIT_IMPORTANT_FLOOR`。
        emotion_boost: `EMOTION_BOOST`。

    Returns:
        裁决结果。注意 ``important`` 在超限降级时被置 true——
        「别再 pin 了，但这条仍算重要」，与 A33 的措辞一致。
    """
    if daily_max <= 0:
        raise ValueError(f"daily_max 必须为正：{daily_max}")

    # 1) 滥用护栏：超限则降级（第 max+1 次起）
    if pinned and pinned_in_window >= daily_max:
        return GuardDecision(
            pinned=False,
            important=True,
            emotion_intensity=float(emotion_intensity),
            pinned_source=None,
            user_directive=None,
            importance_floor=float(explicit_important_floor),
            importance_boost=float(emotion_boost) * float(emotion_intensity),
            anomaly=ANOMALY_DIRECTIVE_ABUSE,
        )

    # 2) 显式记住 → PROTECTED
    pinned_source: str | None = None
    user_directive: str | None = None
    if pinned:
        pinned_source = "user_directive"
        user_directive = "remember"
    elif important:
        user_directive = "important"

    # 3) 重要性宣告 → importance 下限（不升 PROTECTED）
    floor: float | None = None
    if important and not pinned:
        floor = float(explicit_important_floor)
    elif important and pinned:
        # 同时 pinned + important：pin 已经是最强保护，下限仍记录但不叠加
        floor = None

    # 4) 情绪 → 只加成，永不构成保护
    boost = float(emotion_boost) * float(emotion_intensity)

    return GuardDecision(
        pinned=pinned,
        important=important,
        emotion_intensity=float(emotion_intensity),
        pinned_source=pinned_source,
        user_directive=user_directive,
        importance_floor=floor,
        importance_boost=boost,
        anomaly=None,
    )


def merge_importance(
    *,
    base: float,
    floor: float | None,
    boost: float,
) -> float:
    """合并基础分、下限与情绪加成，封顶 1.0（ADR C.B13）。"""
    score = float(base)
    if floor is not None:
        score = max(score, float(floor))
    score += float(boost)
    return min(1.0, score)
