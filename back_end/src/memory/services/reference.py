"""指称层（spec 041）：把「字面」和「义项」拆开。

## 为什么需要

系统现在把字面直接当指称：`鱼` 在一个群里指 deepseek agent，在菜市场指动物，
但图上只有一个 `Entity{name:"鱼"}`（而且单字根本进不了图）。

本层给每个字面在**某个 scope 内**长出义项（`Sense`），并记录：

- 它凭什么被当成这个意思（信号 + 分值）
- 有多确定（`uncertain` / `probable` / `exact` / `denied` / `conflict`）
- 谁在什么时候说过（`Mention` / `Claim`）

## 两条硬规则

1. **弱边不传递**：传递闭包只在 `exact` 上做，深度 ≤1
2. **弱边不参与破坏性操作**：合并实体 / 删重复 / 改归属，一律只认 `exact`

## 触发式发现（不做全量）

| 信号 | 分值 | 例 |
|---|---|---|
| 称呼位 `@w` / `w说` / `问w` / `w帮我` / `w在吗` | +3 | `@鱼 帮我查下` |
| 定义句 `w是X` / `w指X` / `我们说的w是X` | +3 | `鱼就是那个 deepseek 的 bot` |
| 群内同名账号 | +2 | `xxxx的大肥鱼` |
| 宾语位 + 食物/动作语境 | −2 | `这鱼新鲜吗` |

`接口` 这类高频词指称没变 —— **频次只能当候选，不能当判据**。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: 候选阈值：≥2 建 `uncertain`；≥4 且无反向信号 → `probable`
CANDIDATE_MIN = 2
PROBABLE_MIN = 4

#: 同一字面最多允许几个义项参与「一条 Mention」（超过就不猜）
MAX_SENSES_PER_MENTION = 2

#: 证据数组上限（保留最新；超出记 truncated）
EVIDENCE_LIMIT = 20

#: 三档查询 → 允许的状态
TIER_STATUSES: dict[str, tuple[str, ...]] = {
    "strict": ("exact",),
    "default": ("exact", "probable"),
    # denied 也留在 explore：人否认过这件事本身是证据，不该凭空消失
    "explore": ("exact", "probable", "uncertain", "conflict", "denied"),
}

_SURFACE = r"[\u4e00-\u9fa5A-Za-z0-9_]{1,8}"

#: 称呼位：`@鱼`（`@` 后面紧跟的**词**由分词给出）
_AT_MARK = "@"
#: 前置动词：`问鱼` / `找鱼`
_CALL_VERBS = ("问", "找", "喊", "叫")
#: 后置标记：`鱼说` / `鱼在吗` / `鱼帮我`
_SPEAK_SUFFIX = ("说", "问", "帮我", "在吗", "回复")
#: 定义句的连接词
_DEFINE_LEADS = ("就是", "是指", "指的是", "是", "指")

#: 信号权重
W_AT = 3
W_DEFINE = 3
W_ACCOUNT = 2
W_NEGATIVE = -2

#: 不是可指称名词的「词」：代词、虚词、常见动词、时间词
_SURFACE_STOP = frozenset({
    "他", "她", "它", "我", "你", "咱", "这", "那", "什么", "怎么", "为什么",
    "这个", "那个", "一下", "现在", "今天", "明天", "昨天", "然后", "所以",
    "但是", "因为", "如果", "可以", "没有", "不是", "就是", "还是",
    "请", "还", "题", "问", "说", "是", "就", "也", "都", "要", "会", "能",
    "想", "看", "给", "用", "做", "来", "去", "上", "下", "中", "里", "外",
    "好", "行", "对", "错", "多", "少", "大", "小", "高", "低", "长", "短",
    "吧", "呢", "啊", "呀", "了", "的", "地", "得", "和", "与", "跟", "或",
})

#: 反向语境的动词/形容词（食物、动作）—— 说明指的是原义
_NEGATIVE_VERBS = ("吃", "钓", "买", "煮", "做")
_NEGATIVE_ADJ = ("新鲜", "好吃", "刺")

#: 词性白名单：只在这些词性里找字面（人名/名词/英文/专名）
_NOUN_FLAGS = ("n", "nr", "ns", "nt", "nz", "vn", "eng", "x")


@dataclass(frozen=True)
class Signal:
    """一次触发信号。"""

    surface: str
    weight: int
    kind: str                 # at / speak / call / define / account / negative
    gloss: str | None = None
    quote_id: str | None = None
    ts: str | None = None


@dataclass
class SenseCandidate:
    """一个候选义项（term + scope + gloss）。"""

    term: str
    scope: str
    gloss: str | None = None
    score: int = 0
    negative: int = 0
    evidence: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.negative > 0 and self.score + self.negative < PROBABLE_MIN:
            return "uncertain"
        if self.score >= PROBABLE_MIN:
            return "probable"
        return "uncertain"

    @property
    def confidence(self) -> float:
        base = min(0.95, 0.4 + 0.1 * self.score)
        return round(max(0.1, base + 0.1 * self.negative), 2)

    def to_dict(self) -> dict:
        return {
            "term": self.term,
            "scope": self.scope,
            "gloss": self.gloss,
            "score": self.score,
            "status": self.status,
            "confidence": self.confidence,
            "evidence": self.evidence[:EVIDENCE_LIMIT],
            "truncated": max(0, len(self.evidence) - EVIDENCE_LIMIT),
        }


def _clean_surface(text: str) -> str:
    """去掉粘在字面尾部的语气词/量词。"""
    name = (text or "").strip()
    tails = ("一下", "一会", "吗", "呢", "吧", "啊", "呀", "了", "的")
    changed = True
    while changed and name:
        changed = False
        for tail in tails:
            if name.endswith(tail) and len(name) - len(tail) >= 1:
                name = name[: -len(tail)]
                changed = True
                break
    return name


#: 引号内整段锁住：`《人是猫》` 不能被切成 `人 / 是 / 猫`
#: （实测把「人」当定义主语，抽出 `人 → 猫》完整版`）。
_QUOTE_PAIR_RE = re.compile(
    r"《[^》]{1,60}》|「[^」]{1,60}」|『[^』]{1,60}』|“[^”]{1,60}”|\"[^\"]{1,60}\""
)


def _tokenize(text: str) -> list[tuple[str, str]]:
    """分词 → [(word, flag)]。词边界比裸正则可靠得多。

    **引号/书名号整段先锁起来**：先替换成占位符再分词，分词后还原成一个 token。
    不然 `《那些年》` 会被拆成 `那些` + `年`，`《人是猫》` 会被当成 `人 是 猫`。
    """
    src = text or ""
    held: list[str] = []

    def _hold(m: re.Match) -> str:
        # 单个 PUA 字符占位：jieba 会把 `\uE0000\uE001` 拆成三个 token，
        # 但单字符它只切一刀。
        held.append(m.group(0))
        return chr(0xE000 + len(held) - 1)

    masked = _QUOTE_PAIR_RE.sub(_hold, src)
    try:
        import jieba.posseg as pseg

        tokens = [(w.word, w.flag) for w in pseg.cut(masked)]
    except Exception:  # noqa: BLE001 - 分词不可用时退化为按空白切
        tokens = [(w, "x") for w in re.split(r"\s+", masked) if w]
    out: list[tuple[str, str]] = []
    for word, flag in tokens:
        if len(word) == 1 and 0xE000 <= ord(word) <= 0xE0FF:
            out.append((held[ord(word) - 0xE000], "nz"))
        else:
            out.append((word, flag))
    return out


def extract_signals(text: str, *, quote_id: str | None = None, ts: str | None = None) -> list[Signal]:
    """从一段文本里抽触发信号（基于**词边界**，不做全量语义推断）。

    只认三种明确句式：

    1. `@w`（`@` 后紧跟的词）
    2. `w说` / `问w` / `w在吗` / `w帮我`
    3. `w 就是/是/指 X`（定义句，带 gloss）
    外加反向：`吃w` / `w新鲜`（说明指原义）。
    """
    tokens = _tokenize(text)
    words = [w for w, _ in tokens]
    flags = [f for _, f in tokens]
    out: list[Signal] = []

    def _is_noun(idx: int) -> bool:
        if idx < 0 or idx >= len(words):
            return False
        word, flag = words[idx], flags[idx]
        if not word or word in _SURFACE_STOP or word.isdigit():
            return False
        # 纯标点（jieba 的 `x` 也包含标点）不是字面：`）`/`：`/`/` 都被当成过
        if not any(c.isalnum() or "\u4e00" <= c <= "\u9fa5" for c in word):
            return False
        if len(word) > 8:
            return False
        # 允许单字，但必须是名词/专名（`鱼` 是 x/nz，`请` 不是）
        return flag.startswith(_NOUN_FLAGS) or flag in ("x", "eng")

    for idx, word in enumerate(words):
        # 1) @w
        if word == _AT_MARK and idx + 1 < len(words):
            nxt = words[idx + 1]
            if _is_noun(idx + 1):
                out.append(Signal(_clean_surface(nxt), W_AT, "at", quote_id=quote_id, ts=ts))
            continue
        # 2a) 前置动词：`问鱼` / `找鱼`（动词本身不是名词，所以放在名词判断之外）
        if word in _CALL_VERBS and idx + 1 < len(words) and _is_noun(idx + 1):
            out.append(Signal(_clean_surface(words[idx + 1]), W_AT, "call",
                              quote_id=quote_id, ts=ts))
            continue

        # 2a') jieba 有时把「问鱼」切成一个词（`问鱼/n`）—— 去掉动词前缀再看
        if len(word) >= 2 and word[0] in _CALL_VERBS:
            rest = _clean_surface(word[1:])
            if rest and rest not in _SURFACE_STOP and not rest.isdigit():
                out.append(Signal(rest, W_AT, "call", quote_id=quote_id, ts=ts))
                continue

        # 2b) 后置标记：`w说` / `w在吗` / `w帮我`
        if _is_noun(idx):
            nxt = words[idx + 1] if idx + 1 < len(words) else ""
            after = words[idx + 2] if idx + 2 < len(words) else ""
            if nxt in _SPEAK_SUFFIX:
                out.append(Signal(_clean_surface(word), W_AT, "speak",
                                  quote_id=quote_id, ts=ts))
            elif nxt == "在" and after == "吗":
                # jieba 把「在吗」切成 `在` + `吗`
                out.append(Signal(_clean_surface(word), W_AT, "speak",
                                  quote_id=quote_id, ts=ts))
        # 3) w 是/就是/指 X
        #
        # **书名号/引号里的「X 是 Y」不是定义**：实测 `《人是猫》完整版`
        # 被切成 `《 人 是 猫 》 完整版`，于是抽出 `人 → 猫》完整版` 这种垃圾义项
        # （评测时 20 条义项里 16 条是这类碎片）。
        if _is_noun(idx) and idx + 1 < len(words) and words[idx + 1] in _DEFINE_LEADS:
            gloss = "".join(words[idx + 2: idx + 6]).strip()
            prev = words[idx - 1] if idx > 0 else ""
            in_title = prev in ("《", "「", "『", "“", "\"") or any(
                ch in gloss for ch in ("》", "」", "』", "”", "\""))
            fragment = (not gloss) or gloss[0] in "，。；、！？" or gloss[-1] in "，、；："
            if _gloss_ok(gloss) and not in_title and not fragment:
                out.append(Signal(_clean_surface(word), W_DEFINE, "define", gloss=gloss,
                                  quote_id=quote_id, ts=ts))
        # 4) 反向：吃w / w新鲜
        if word in _NEGATIVE_VERBS and idx + 1 < len(words) and _is_noun(idx + 1):
            out.append(Signal(_clean_surface(words[idx + 1]), W_NEGATIVE, "negative",
                              quote_id=quote_id, ts=ts))
        if _is_noun(idx) and idx + 1 < len(words) and words[idx + 1] in _NEGATIVE_ADJ:
            out.append(Signal(_clean_surface(word), W_NEGATIVE, "negative",
                              quote_id=quote_id, ts=ts))

    # 去重：同一 (surface, kind, gloss) 只留一次
    seen: set[tuple[str, str, str | None]] = set()
    uniq: list[Signal] = []
    for sig in out:
        key = (sig.surface, sig.kind, sig.gloss)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(sig)
    return uniq


def account_signals(nicks: list[str], *, surface: str) -> Signal | None:
    """群内是否存在包含该字面的账号（`xxxx的大肥鱼` → 鱼）。"""
    for nick in nicks:
        if surface and surface in (nick or ""):
            return Signal(surface, W_ACCOUNT, "account")
    return None


def build_candidates(
    signals: list[Signal],
    *,
    scope: str,
    nicks: list[str] | None = None,
    candidate_min: int = CANDIDATE_MIN,
) -> list[SenseCandidate]:
    """把信号聚合成候选义项。

    Returns:
        按分数降序的候选；**低于 `candidate_min` 的不返回**（不建 Sense）。
    """
    groups: dict[tuple[str, str | None], SenseCandidate] = {}
    for sig in signals:
        key = (sig.surface, sig.gloss)
        cand = groups.setdefault(
            key, SenseCandidate(term=sig.surface, scope=scope, gloss=sig.gloss)
        )
        if sig.weight < 0:
            cand.negative += abs(sig.weight)
        else:
            cand.score += sig.weight
        if sig.quote_id:
            cand.evidence.append(sig.quote_id)

    # 账号信号：字面出现在群成员昵称里
    for nick in nicks or []:
        for (surface, gloss), cand in list(groups.items()):
            if surface and surface in nick and cand.gloss is None:
                cand.score += W_ACCOUNT
                break

    out = [c for c in groups.values() if c.score >= candidate_min]
    out.sort(key=lambda c: (-c.score, c.term))
    return out


def tier_allows(status: str, tier: str) -> bool:
    """三档查询：`strict` 只认 exact；`default` 含 probable；`explore` 全含。"""
    allowed = TIER_STATUSES.get(tier or "default")
    if allowed is None:
        raise ValueError(f"未知 tier: {tier}")
    return status in allowed


def pick_sense(candidates: list[dict], *, tier: str = "default") -> tuple[dict | None, bool]:
    """从候选里挑一个义项。

    Returns:
        `(sense, ambiguous)`。**多于 `MAX_SENSES_PER_MENTION` 个可选项时返回
        `(None, True)`** —— 选不了就留痕，不猜（「ds」既可能是 deepseek 也可能是大神）。
    """
    usable = [c for c in candidates if tier_allows(c.get("status", ""), tier)]
    if not usable:
        return None, False
    if len(usable) > MAX_SENSES_PER_MENTION:
        return None, True
    usable.sort(key=lambda c: (-float(c.get("confidence") or 0), -int(c.get("score") or 0)))
    return usable[0], len(usable) > 1


#: 写 Term / Sense / Mention
MERGE_SENSE_CYPHER = """
UNWIND $items AS item
MERGE (t:Term {text: item.term})
ON CREATE SET t.first_seen = $now
MERGE (s:Sense {term: item.term, scope: item.scope, gloss: item.gloss})
ON CREATE SET s.created_at = $now, s.evidence = []
SET s.status = CASE WHEN s.status = 'exact' THEN 'exact' ELSE item.status END,
    s.confidence = item.confidence,
    s.evidence = reduce(acc = coalesce(s.evidence, []), x IN item.evidence |
        CASE WHEN x IN acc THEN acc ELSE acc + x END),
    s.updated_at = $now
MERGE (t)-[:HAS_SENSE]->(s)
RETURN count(s) AS n
"""

#: 按 tier 查义项
QUERY_SENSES_CYPHER = """
MATCH (t:Term {text: $term})-[:HAS_SENSE]->(s:Sense)
WHERE s.scope = $scope AND s.status IN $statuses
RETURN s.term AS term, s.scope AS scope, s.gloss AS gloss,
       s.status AS status, s.confidence AS confidence, s.evidence AS evidence
ORDER BY s.confidence DESC
"""

# ============================================================ M2：Claim

#: Claim 的三种模式。`question` 只记证据、**不建边**（REQ-4）
CLAIM_MODES = ("assert", "question", "deny")

#: 断言句式后缀词（分词后判断）
_ASSERT_LEADS = ("是", "就是", "指", "指的是", "是指")

#: gloss 里出现这些字符 → 是句子碎片，不是解释（实测 `分词器 → 什么，猜测可能`）
_GLOSS_BAD = set("，。；！？、,.;!?：:（）()")
#: gloss 以这些词开头 → 是口语碎片，不是定义
_GLOSS_BAD_PREFIX = ("什么", "怎么", "谁", "哪", "还是", "因为", "可以", "例如")


def _gloss_ok(gloss: str) -> bool:
    """判断一个 gloss 是否像「完整的解释」。"""
    text = (gloss or "").strip()
    if not (2 <= len(text) <= 24):
        return False
    if any(ch in _GLOSS_BAD for ch in text):
        return False
    return not text.startswith(_GLOSS_BAD_PREFIX)
_QUESTION_TAILS = ("吗", "吧", "？", "?", "呢")
#: gloss 里出现这些词说明是疑问句（`密码是多少`），不是定义
_QUESTION_WORDS = ("什么", "多少", "怎么", "哪", "是否", "何时", "为何")
_DENY_LEADS = ("不是", "并不是", "不指")


@dataclass(frozen=True)
class Claim:
    """一条关于义项的断言。"""

    term: str
    scope: str
    gloss: str | None
    mode: str                      # assert / question / deny
    speaker: str | None = None
    quote_id: str | None = None
    ts: str | None = None


def extract_claims(text: str, *, scope: str, quote_id: str | None = None,
                   ts: str | None = None, speaker: str | None = None) -> list[Claim]:
    """从文本抽断言（三态）。

    - `X是Y` → assert
    - `X是Y吧？` / `X是Y吗` → question（**只记证据，不建边**）
    - `X不是Y` → deny

    判据仍是 jieba 词边界：`X` 必须是名词性词，`Y` 是最多 4 个词。
    """
    tokens = _tokenize(text)
    words = [w for w, _ in tokens]
    flags = [f for _, f in tokens]
    out: list[Claim] = []

    def _noun(idx: int) -> bool:
        if idx < 0 or idx >= len(words):
            return False
        word, flag = words[idx], flags[idx]
        if not word or word in _SURFACE_STOP or word.isdigit():
            return False
        if not any(c.isalnum() or "\u4e00" <= c <= "\u9fa5" for c in word):
            return False
        return flag.startswith(_NOUN_FLAGS) or flag in ("x", "eng")

    for idx, word in enumerate(words):
        if not _noun(idx) or idx + 1 >= len(words):
            continue
        nxt = words[idx + 1]
        mode = None
        lead_len = 1
        if nxt in _DENY_LEADS:
            mode, lead_len = "deny", 1
        elif nxt in _ASSERT_LEADS:
            mode, lead_len = "assert", 1
        if mode is None:
            continue

        gloss_words = words[idx + 1 + lead_len: idx + 1 + lead_len + 4]
        gloss = "".join(gloss_words).strip().rstrip("的了")
        # 去掉开头的标点/引号（`“中登不可查` / `，单点登录有`）
        gloss = gloss.lstrip("，。、：；！？“”‘’\"'（）()【】[]")
        if not gloss or len(gloss) < 1:
            continue
        # 疑问判定看**整句结尾**：`X是Y吧？` / `X是Y吗`
        _tail_text = (text or "").rstrip()
        if mode == "assert" and (
            _tail_text.endswith("？") or _tail_text.endswith("?")
            or (_tail_text and _tail_text[-1] in ("吗", "吧", "呢"))
            or any(w in gloss for w in _QUESTION_WORDS)
        ):
            mode = "question"

        out.append(Claim(
            term=_clean_surface(word), scope=scope, gloss=gloss, mode=mode,
            speaker=speaker, quote_id=quote_id, ts=ts,
        ))

    # 去重
    seen: set[tuple[str, str, str | None]] = set()
    uniq: list[Claim] = []
    for c in out:
        key = (c.term, c.mode, c.gloss)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def resolve_status(current: str, claims: list[Claim]) -> str:
    """按断言裁决义项状态（spec 041 §6 升降级）。

    规则顺序（先到先得）：

    1. 有人否认 → `denied`
    2. 两个不同的 gloss 都被断言过 → `conflict`
    3. 人确认过（`exact`）**不被降级** —— 只被否认改
    4. ≥2 条 assert → `probable`
    5. 其余保持原状（`uncertain` 不因单条断言升高）
    """
    if current == "exact":
        return "denied" if any(c.mode == "deny" for c in claims) else "exact"

    if any(c.mode == "deny" for c in claims):
        return "denied"

    asserted = {c.gloss for c in claims if c.mode == "assert" and c.gloss}
    if len(asserted) >= 2:
        return "conflict"

    if sum(1 for c in claims if c.mode == "assert") >= 2:
        return "probable"

    return current


#: 写 Claim 并按裁决更新 Sense
APPLY_CLAIM_CYPHER = """
UNWIND $items AS item
MATCH (s:Sense {term: item.term, scope: item.scope, gloss: item.gloss})
MERGE (c:Claim {term: item.term, scope: item.scope, gloss: item.gloss,
                mode: item.mode, quote_id: item.quote_id})
ON CREATE SET c.ts = item.ts, c.speaker = item.speaker, c.created_at = $now
SET c.last_seen = $now
// question 只记证据、**不建边**（REQ-4）：`这个是鱼吧？` 不能当成支持
FOREACH (_ IN CASE WHEN item.mode <> 'question' THEN [1] ELSE [] END |
    MERGE (c)-[:SUPPORTS]->(s)
)
SET s.status = item.status, s.updated_at = $now
RETURN count(c) AS n
"""

#: 人确认 / 否认（不经 Claim，直接改状态，留 operator）
SET_SENSE_STATUS_CYPHER = """
MATCH (s:Sense {term: $term, scope: $scope, gloss: $gloss})
SET s.status = $status, s.verdict_operator = $operator, s.verdict_at = $now
RETURN s.term AS term, s.scope AS scope, s.gloss AS gloss, s.status AS status
"""

#: 写 Mention（一次出现）
MERGE_MENTION_CYPHER = """
UNWIND $items AS item
MATCH (s:Sense {term: item.term, scope: item.scope, gloss: item.gloss})
MERGE (m:Mention {scope: item.scope, quote_id: item.quote_id, surface: item.term})
ON CREATE SET m.ts = item.ts, m.created_at = $now
MERGE (m)-[r:USES_SENSE]->(s)
SET r.label = s.status, r.confidence = s.confidence
RETURN count(m) AS n
"""

#: 指称层 ↔ 账号族（spec 041 M3 / 042 对接）：
#: 义项的 gloss 里出现某个 Bot 主体名 → 建立 `REFERS_TO`。
#:
#: **只在同一 namespace 内连**：`鱼` 在 A 群指 bot、在 B 群指动物，
#: 跨 namespace 连边会把两件事混成一件。
LINK_SENSE_BOT_CYPHER = """
MATCH (s:Sense) WHERE s.scope STARTS WITH 'session:'
MATCH (b:Bot {namespace: $namespace})
WHERE s.gloss CONTAINS b.name
MERGE (s)-[r:REFERS_TO]->(b)
ON CREATE SET r.created_at = $now
SET r.evidence = coalesce(r.evidence, [])
RETURN count(r) AS n
"""
