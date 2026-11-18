"""关系谓词（spec 039）：陈述通道 + 行为通道。

为什么要两条通道：

- **陈述**（「王泳负责前端」）精确但稀疏 —— 现有 566 条原文里
  `负责/维护/对接/汇报` 命中 **0**；
- **行为**（部署前端 3 次）稠密但要推断 —— 语料里部署类行为有 11 条。

只做陈述：一条边都抽不出来；只做行为：回答不了「谁**正式**负责」。
两条合并才有可用的关系层。

## 合并不变量

1. `exact`（陈述）**永远不被** `probable`（行为）降级
2. 行为可以给已有边**加证据**，但不能改写 label
3. 头尾类型不符直接拒（`OWNS: Person → Entity`）
4. 冲突（陈述指 A、行为指 B）→ 两条边都留，标 `conflict`
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger("memory.relations")

#: 谓词表 v2。**删掉了 `REPORTS_TO`** —— `汇报/上级/领导/主管` 在语料里 0 命中，
#: 按「没有证据就先不定义」的规矩移除，将来有证据再加。
PREDICATE_SPECS: dict[str, tuple[str, str]] = {
    "OWNS": ("Person", "Entity"),
    "MAINTAINS": ("Person", "Entity"),
    "COLLABORATES_WITH": ("Person", "Person"),
    "DEPENDS_ON": ("Entity", "Entity"),
}

#: 行为动词 → 谓词。只收**职责性**动作：
#: `部署/发布/更新/重启/回滚` 是维护动作，`修改/修复/实现/新增` 是建设动作。
#: 刻意不收 `讨论/确认/汇报` —— 那些说明「参与过」，不说明「负责」。
VERB_TO_PREDICATE: dict[str, str] = {
    # === 维护类：部署/发布/更新 等，动作本身就在维持某个东西 ===
    "部署": "MAINTAINS",
    "发布": "MAINTAINS",
    "上架": "MAINTAINS",
    "更新": "MAINTAINS",
    "重启": "MAINTAINS",
    "回滚": "MAINTAINS",
    "维护": "MAINTAINS",
    "对接": "MAINTAINS",
    # === 建设类：改/修/实现，产出物归主语 ===
    "修改": "OWNS",
    "修复": "OWNS",
    "实现": "OWNS",
    "新增": "OWNS",
    # === 协作类（2026-11-18 按人工标注补，spec 039 v3）===
    #
    # 依据：行为组 10 条人工标注里 `讨论` 出现 6 次（判 2/1/1/0/0/0）、
    # `去` 出现 4 次（判 2/1/0/0）——两个都被判过「2 分＝有关系·协作」。
    # 所以按标注补进来，但**语义上偏弱**：`去` 尤其容易把「去某地」误当关系。
    # 阈值（≥2 次独立事件 + 90 天窗口）是唯一的安全网。
    "讨论": "COLLABORATES_WITH",
    "确认": "COLLABORATES_WITH",
    "同意": "COLLABORATES_WITH",
    "去": "COLLABORATES_WITH",
}

#: 行为证据的门槛：同一 (人, 谓词, 目标) 至少几次独立事件才算「职责」。
#: 1 次可能只是临时帮忙；这条阈值必须可配（A3 要求 1 次不建边）。
DEFAULT_THRESHOLD = 2

#: 行为证据的有效期（天）。陈述没有有效期 —— 用户说的是事实，不会过期。
DEFAULT_WINDOW_DAYS = 90


@dataclass(frozen=True)
class RelationClaim:
    """一条候选关系。"""

    head: str
    predicate: str
    tail: str
    label: str = "probable"            # exact / probable / conflict
    confidence: float = 0.6
    evidence: tuple[str, ...] = ()     # event_id 列表（可回溯）
    source: str = "behavior"           # behavior / statement

    def key(self) -> tuple[str, str, str]:
        return (self.head, self.predicate, self.tail)


def predicate_for_verb(verb: str) -> str | None:
    """行为动词 → 谓词；不在表里的返回 None（不硬凑）。"""
    return VERB_TO_PREDICATE.get((verb or "").strip())


def aggregate_behaviors(
    rows: list[dict],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    window_days: int = DEFAULT_WINDOW_DAYS,
    today: date | None = None,
) -> list[RelationClaim]:
    """把「人-动作-对象」行为行聚合成关系边。

    Args:
        rows: 每行 `{"person", "verb", "target", "event_id", "date"}`。
        threshold: 最少几次独立事件（A3：1 次不建边）。
        window_days: 只看最近 N 天（0/None 表示不限）。
        today: 基准日（测试用）。

    Returns:
        按证据数降序的 claim 列表。**同一个 event 只算一次证据**
        （卡片重放不会把数字刷大）。
    """
    if not rows:
        return []
    cutoff = None
    if window_days and window_days > 0:
        base = today or date.today()
        cutoff = base - timedelta(days=window_days)

    groups: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        person = (row.get("person") or "").strip()
        target = (row.get("target") or "").strip()
        verb = (row.get("verb") or "").strip()
        event_id = (row.get("event_id") or "").strip()
        if not person or not target or not event_id:
            continue
        predicate = predicate_for_verb(verb)
        if not predicate:
            continue
        day = _as_date(row.get("date"))
        if cutoff is not None and day is not None and day < cutoff:
            continue
        if predicate == "MAINTAINS" and person == target:
            continue  # 自己维护自己，没意义
        groups.setdefault((person, predicate, target), set()).add(event_id)

    claims: list[RelationClaim] = []
    for (head, predicate, tail), event_ids in groups.items():
        if len(event_ids) < threshold:
            continue
        claims.append(
            RelationClaim(
                head=head,
                predicate=predicate,
                tail=tail,
                label="probable",
                confidence=round(min(0.95, 0.5 + 0.1 * len(event_ids)), 2),
                evidence=tuple(sorted(event_ids)),
                source="behavior",
            )
        )
    claims.sort(key=lambda c: (-len(c.evidence), c.head, c.tail))
    return claims


#: 陈述句式（规则版）。刻意**不先用 LLM**：陈述语料本来就稀疏，
#: 规则可测、零成本；等真有大量陈述（分工文档/会议纪要）再上 LLM。
_STATEMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    # (谓词, 正则模板) —— 用命名分组 head / tail
    ("OWNS", r"(?P<head>[\u4e00-\u9fa5A-Za-z0-9]{2,8})\s*(?:负责|牵头|主管)\s*(?P<tail>[\u4e00-\u9fa5A-Za-z0-9]{2,10})"),
    ("MAINTAINS", r"(?P<head>[\u4e00-\u9fa5A-Za-z0-9]{2,8})\s*(?:维护|对接)\s*(?P<tail>[\u4e00-\u9fa5A-Za-z0-9]{2,10})"),
    ("COLLABORATES_WITH", r"(?P<head>[\u4e00-\u9fa5]{2,4})\s*(?:和|与|跟)\s*(?P<tail>[\u4e00-\u9fa5]{2,4})\s*(?:一起|共同)"),
    # 只认「依赖」。`调用` 太常见（「调用了接口」是动作不是依赖），实测会抽出
    # `第一个接口是 -[DEPENDS_ON]-> 中登查询` 这种垃圾边。
    ("DEPENDS_ON", r"(?P<head>[\u4e00-\u9fa5A-Za-z0-9]{2,12})\s*依赖\s*(?P<tail>[\u4e00-\u9fa5A-Za-z0-9]{2,12})"),
)


#: 陈述句里「谁在说」的前缀词：`任泽安说王泳负责前端` 的主体是王泳，不是「任泽安说王泳」。
_SPEAKER_MARKERS = ("说", "表示", "提到", "称", "指出", "让", "请", "通知")

#: 尾值的截断符：`前端页面的维护` → `前端页面`
_TAIL_STOPS = ("的", "了", "和", "与", "并", "，", "。", "；", "、", "！", "？")


def _clean_head(raw: str) -> str:
    """去掉「谁在说」的前缀，只留被陈述的主体。"""
    text = (raw or "").strip()
    for marker in _SPEAKER_MARKERS:
        idx = text.rfind(marker)
        if idx > 0 and len(text) - idx - len(marker) >= 2:
            text = text[idx + len(marker):].strip()
    return text


def _clean_tail(raw: str) -> str:
    """在第一个截断符处断开，避免把「的维护」吃进目标名。"""
    text = (raw or "").strip()
    cut = len(text)
    for stop in _TAIL_STOPS:
        idx = text.find(stop)
        if 0 < idx < cut:
            cut = idx
    return text[:cut].strip()


#: 名字里不该出现的虚词/动词：出现就说明抽到的是短语，不是实体名。
_NAME_STOP_CHARS = frozenset("的了是需要来去进行通过获取使用你他她我")

#: 名字不能以这些字开头（「的接口」「是第一个」）
_NAME_BAD_PREFIX = frozenset("的是了和与跟而但也就在把被让给")

#: 名字最长几个字（实体名很少超过 8 个字）
_NAME_MAX_LEN = 8


def _looks_like_name(text: str) -> bool:
    """粗判一个抽取结果是不是「像实体名」。

    规则版陈述通道的防护：宁可漏，也不要把整句短语写成关系边。
    实测反例：`第一个接口是` / `的接口需要前端轮询来` / `获取结果`。
    """
    name = (text or "").strip()
    if len(name) < 2 or len(name) > _NAME_MAX_LEN:
        return False
    if name[0] in _NAME_BAD_PREFIX:
        return False
    return not any(ch in _NAME_STOP_CHARS for ch in name)


def extract_statements(
    text: str,
    *,
    candidates: list[str] | None = None,
    rejected: list[dict] | None = None,
) -> list[RelationClaim]:
    """从文本抽关系陈述（规则版，`label=exact`）。

    **只抽明确句式**：`X 负责 Y` / `X 维护 Y` / `X 和 Y 一起` / `X 依赖 Y`。
    宁可漏 —— 把叙述句误判成职责声明，比少一条边难修得多。

    Args:
        text: 证据句。
        candidates: 传入则启用**实体约束**（改造 A）：尾值必须是候选实体之一，
            否则丢弃并记进 `rejected`。不传则保持旧行为（只做名字形状检查）。
        rejected: 收集被实体约束挡掉的项（含 reason），便于报告。

    Note:
        实测不加约束时，`克劳德负责写门牌号` 的尾值是「写门牌号」（动作）。
        实体约束能挡住这类，代价是也会挡掉一部分正确但分词没覆盖的词。
    """
    import re

    out: list[RelationClaim] = []
    for predicate, template in _STATEMENT_PATTERNS:
        pattern = re.compile(template)
        for match in pattern.finditer(text or ""):
            head = _clean_head(match.group("head"))
            tail = _clean_tail(match.group("tail"))
            if not head or not tail or head == tail:
                continue
            if not _looks_like_name(head) or not _looks_like_name(tail):
                continue
            if candidates is not None:
                # 尾值必须落在候选实体里（或其子串），这是改造 A 的核心约束
                if not (tail in candidates or any(tail in c for c in candidates)):
                    if rejected is not None:
                        rejected.append({"head": head, "tail": tail,
                                         "reason": f"尾 {tail!r} 不是候选实体"})
                    continue
                if not (head in candidates or any(head in c or c in head for c in candidates)):
                    if rejected is not None:
                        rejected.append({"head": head, "tail": tail,
                                         "reason": f"头 {head!r} 不是候选实体"})
                    continue
            out.append(
                RelationClaim(
                    head=head,
                    predicate=predicate,
                    tail=tail,
                    label="exact",
                    confidence=1.0,
                    evidence=(),
                    source="statement",
                )
            )
    return out


def merge_claims(claims: list[RelationClaim]) -> list[RelationClaim]:
    """合并同一 (head, predicate, tail) 的多条证据。

    - `exact` 优先：有任何陈述，label 就是 exact（**不被行为降级**）
    - 证据取并集（按 event_id 去重，保持稳定顺序）
    - 两条不同 tail 的冲突**不合并**：由调用方按 `conflict` 上报
    """
    merged: dict[tuple[str, str, str], RelationClaim] = {}
    for claim in claims:
        key = claim.key()
        prev = merged.get(key)
        if prev is None:
            merged[key] = claim
            continue
        evidence = tuple(sorted(set(prev.evidence) | set(claim.evidence)))
        label = "exact" if "exact" in (prev.label, claim.label) else claim.label
        confidence = max(prev.confidence, claim.confidence)
        source = "statement" if "statement" in (prev.source, claim.source) else claim.source
        merged[key] = RelationClaim(
            head=claim.head,
            predicate=claim.predicate,
            tail=claim.tail,
            label=label,
            confidence=confidence,
            evidence=evidence,
            source=source,
        )
    return list(merged.values())


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None

# ============================================================ LLM 陈述通道（改造 A）

#: 强度 → 谓词。与标注页的 0–4 完全对齐：
#: 0 无关系 / 1 极低·依赖 / 2 有关系·协作 / 3 较大·维护 / 4 强相关·负责
INTENSITY_TO_PREDICATE: dict[int, str] = {
    1: "DEPENDS_ON",
    2: "COLLABORATES_WITH",
    3: "MAINTAINS",
    4: "OWNS",
}

#: 谓词 → 强度（反向查询用）
PREDICATE_TO_INTENSITY: dict[str, int] = {v: k for k, v in INTENSITY_TO_PREDICATE.items()}

_LLM_PROMPT = """你在读中文群聊。下面给出一句话，以及从这句话里抽出的**候选实体**。

任务：从候选实体里挑出**存在稳定关系**的配对，并给强度。

强度五档：
0 = 无关系
1 = 极低：A 依赖 B（B 不可用 A 就工作不了）
2 = 有关系：A 和 B 一起做事 / 协作
3 = 较大：A 长期维护、部署、对接 B
4 = 强相关：A 拥有、负责、建设、发起 B

判断要点：
- **允许合理推断**：叙述句里能看出谁负责什么，就算（「便携式解释服务器是什么」
  → 便携式对服务器有 4 级关系）
- head 和 tail **只能从候选实体里挑**，不要新造词
- 不要输出「一起」「说话」「知道」这类只是同处一句、没有职责含义的配对
- **只输出这句话最主要的那一条关系**（人工标注也是一句一条）；
  并列多件事时选最核心的那条
- head 必须是**行为主体**：群友昵称、人名、**角色**（`运营`＝运营人员）、
  **机器账号**（`传讯员8号`＝店员账号）都算
- tail 可以是物件/系统/概念，也可以是**活动的名词形式**
  （句子里没有名词时，`部署`/`训练`/`维护` 指「那次部署/那个被训练的模型」）
- 强度 0 不要输出；看不出关系就输出 []

候选实体：{candidates}

上下文（仅供理解指代，不要从里面抽关系）：
{context}

句子：
{text}

每条都必须给出 **evidence**：原文里能支撑这个配对的**连续片段**，
必须包含把它们连起来的关系词（负责/维护/对接/依赖/一起/协作/部署/拥有…）。

只输出 JSON 数组。格式：
[{{"head":"便携式","tail":"服务器","intensity":4,"evidence":"便携式负责服务器"}}]"""


#: 关系抽不到时最常见的「伪实体」：动作词/泛称，直接排除
_RELATION_STOP = frozenset({
    "大家", "有人", "对方", "自己", "群里", "群友", "东西", "事情", "问题",
    "时候", "现在", "以后", "这样", "那样", "什么", "怎么", "为什么",
})

#: 「发布/上架某物的人，就是维护该物信息的人」—— 用户定义（2026-11-17）：
#: `运营发布商品截单提醒` → `运营 MAINTAINS 商品`。这不是同处一句，
#: 而是**职责含义**：发商品信息的人负责维护这份信息。
PUBLISH_VERBS = ("发布", "上架", "更新", "通知", "公告", "提醒", "上新")

#: 角色名：`运营` 指运营人员，`客服`/`店长`/`管理` 同理 —— 可以当关系主体
ROLE_WORDS = ("运营", "客服", "店长", "管理", "管理员", "助理", "秘书", "前台")

#: 证据片段里必须出现的关系动词（否则只是「同处一句」，不是关系）
RELATION_CUES = (
    "负责", "牵头", "主管", "分管", "管着", "维护", "运维", "对接", "部署", "发布",
    "更新", "回滚", "重启", "依赖", "调用", "基于", "依托", "一起", "共同", "协作",
    "合作", "配合", "协助", "帮忙", "拥有", "建设", "搭建", "开发", "实现", "新增",
    "修复", "修改", "发起", "组织", "提供", "使用", "需要",
)

#: 这些词性算「实体候选」（jieba flag）
_ENTITY_FLAGS = frozenset({"n", "nz", "vn", "nt", "nr", "ns", "eng"})


def candidate_entities(text: str, *, roster: list[str] | None = None) -> list[str]:
    """收集一句话里可以当关系头/尾的候选实体。

    三个来源：jieba 词性（n/nz/vn/nt/nr/ns/eng）、名册子串、拉丁/中文专名。

    Note:
        实体约束是这次改造的**核心**：规则版把「写门牌号」当尾值，
        就是因为没有这个约束。宁可漏掉一条，也不要建一条动作边。
    """
    out: list[str] = []

    def add(name: str) -> None:
        name = (name or "").strip()
        if not name or name in out:
            return
        if len(name) < 2 or len(name) > _NAME_MAX_LEN:
            return
        if name in _RELATION_STOP or name in _NAME_STOP_CHARS:
            return
        if name[0] in _NAME_BAD_PREFIX:
            return
        out.append(name)

    try:
        import jieba.posseg as pseg

        for w in pseg.cut(text or ""):
            if w.flag in _ENTITY_FLAGS and len(w.word) >= 2:
                add(w.word)
    except Exception:  # noqa: BLE001 - 分词不可用就只靠名册
        pass

    # 名册名先去 emoji/符号/括号：`便携式🦆🍗` → `便携式`，否则永远匹配不上
    import re as _re

    body = text or ""
    for raw in roster or []:
        cleaned = _re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(raw or ""))
        for cand in (str(raw or ""), cleaned):
            if cand and cand in body:
                add(cand)
                break
    return out



#: 中文称呼后缀：`王老师` / `张总` / `李哥` —— 名册里不一定有，但确实指人
_PERSON_SUFFIX = ("老师", "先生", "女士", "小姐", "总", "经理", "主管", "大佬",
                  "师傅", "医生", "律师", "哥", "姐")


def person_candidates(text: str, *, roster: list[str] | None = None) -> list[str]:
    """收集一句话里「像人的」名字。

    三条来源：
    1. jieba 标成 `nr` / `nrfg` / `nrt` 的词
    2. 名册子串（去 emoji）
    3. **称呼模式**：`王老师` / `张总` / `李哥`

    Note:
        只用名册会漏两类真实情况：脱敏改名（`王勇` ↔ 名册里的 `王泳`）
        和称呼（`王老师`）。这两类占语料很大比例，miss 掉会让 precision 判不出来。
    """
    import re as _re

    body = text or ""
    out: list[str] = []

    def add(name: str) -> None:
        name = (name or "").strip()
        if not name or name in out or len(name) < 2 or len(name) > 12:
            return
        out.append(name)

    try:
        import jieba.posseg as pseg

        for w in pseg.cut(body):
            if w.flag in ("nr", "nrfg", "nrt") and len(w.word) >= 2:
                add(w.word)
    except Exception:  # noqa: BLE001
        pass

    for raw in roster or []:
        cleaned = _re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(raw or ""))
        for cand in (str(raw or ""), cleaned):
            if cand and cand in body:
                add(cand)
                break

    # 称呼：取后缀前 1~3 个字
    for suffix in _PERSON_SUFFIX:
        for m in _re.finditer(r"([\u4e00-\u9fa5]{1,3})" + _re.escape(suffix), body):
            add(m.group(0))
    return out


def validate_relation(
    head: str,
    tail: str,
    intensity: int,
    *,
    candidates: list[str],
    text: str,
    evidence: str = "",
    people: list[str] | None = None,
) -> tuple[bool, str]:
    """检查一条 LLM 抽出来的关系是否成立（实体约束）。

    Returns:
        `(ok, reason)`；`reason` 说明被拒原因，便于报告。
    """
    h = (head or "").strip()
    t = (tail or "").strip()
    if intensity not in INTENSITY_TO_PREDICATE:
        return False, f"intensity={intensity} 不在 1~4"
    if not h or not t:
        return False, "头或尾为空"
    if h == t:
        return False, "头尾相同"
    body = text or ""
    if h not in body:
        return False, f"头 {h!r} 不在原文"
    if t not in body:
        return False, f"尾 {t!r} 不在原文"
    # 尾值必须在候选实体里（或其子串，容忍 `老师` ⊂ `老师五点`）
    if t not in candidates and not any(t in c for c in candidates):
        return False, f"尾 {t!r} 不是候选实体"
    if h not in candidates and not any(h in c or c in h for c in candidates):
        return False, f"头 {h!r} 不是候选实体"
    # 证据跨度：必须有把两者连起来的关系词，否则只是「同处一句」
    ev = (evidence or "").strip()
    if not ev:
        return False, "缺少 evidence"
    if ev not in body:
        return False, f"evidence 不在原文: {ev[:30]!r}"
    if h not in ev or t not in ev:
        return False, f"evidence 没覆盖头尾: {ev[:30]!r}"
    if not any(cue in ev for cue in RELATION_CUES):
        return False, f"evidence 里没有关系词: {ev[:30]!r}"
    junk = is_junk_relation(h, t)
    if junk:
        return False, junk
    # 头必须是「人」：关系层要回答「谁负责什么」，主语是物件没有意义
    if people:
        if not any(h == p or h in p or p in h for p in people):
            return False, f"头 {h!r} 不是人"
    return True, ""


def extract_statements_llm(
    text: str,
    settings: Any,
    *,
    context: list[str] | None = None,
    roster: list[str] | None = None,
    people: list[str] | None = None,
    max_claims: int = 1,
    client: Any | None = None,
) -> tuple[list[RelationClaim], list[dict]]:
    """用 LLM 从一句话里抽关系陈述（改造 A）。

    Args:
        text: 证据句（事件卡摘要）。
        settings: LLM 配置。
        context: 同会话的邻近摘要（只读，用来理解指代）。
        roster: 名册名字（补 jieba 认不出的群友）。

    Returns:
        `(claims, rejected)`。`rejected` 记录被实体约束挡掉的项，供报告。

    Note:
        与规则版并行跑：规则命中 → `label=exact`；仅 LLM 命中 → `label=probable`
        （保住「exact 不被 probable 降级」这条不变量）。
    """
    import json as _json

    body = (text or "").strip()
    if not body:
        return [], []
    candidates = candidate_entities(body, roster=roster)
    # 人集合：调用方可覆盖，否则从句子本身推断（jieba nr + 名册 + 称呼）
    if people is None:
        people = person_candidates(body, roster=roster)
    ctx = "\n".join(f"- {c[:100]}" for c in (context or [])[:5]) or "（无）"
    prompt = _LLM_PROMPT.format(
        candidates=_json.dumps(candidates, ensure_ascii=False),
        context=ctx,
        text=body[:400],
    )

    from memory.services.llm import chat

    # 该模型**先输出 thinking**：额度给小了正文会被吃光，报「没有返回文本」。
    # 实测 2048 在 3/10 条上失败；按输入长度给足，并失败重试一次更大的额度。
    est = max(4096, min(12288, len(prompt) * 2))
    raw: Any = None
    last_exc: Exception | None = None
    for attempt, tokens in enumerate((est, min(16384, est * 2))):
        try:
            raw = chat(
                [{"role": "user", "content": prompt}],
                settings,
                system="你是中文社群关系抽取器，只输出 JSON 数组。",
                max_tokens=tokens,
                client=client,
            )
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 - 外部依赖
            last_exc = exc
            logger.warning("LLM 关系抽取失败（第 %d 次, max_tokens=%d）: %s",
                           attempt + 1, tokens, exc)
    if last_exc is not None:
        return [], [{"reason": f"llm_error: {last_exc}", "text": body[:60]}]

    try:
        data = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        return [], [{"reason": "llm_not_json", "text": body[:60]}]
    if isinstance(data, dict):
        data = data.get("relations") or []
    if not isinstance(data, list):
        return [], [{"reason": "llm_not_list", "text": body[:60]}]

    claims: list[RelationClaim] = []
    rejected: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            intensity = int(item.get("intensity") or 0)
        except (TypeError, ValueError):
            intensity = 0
        head = str(item.get("head") or "").strip()
        tail = str(item.get("tail") or "").strip()
        ok, reason = validate_relation(
            head, tail, intensity, candidates=candidates, text=body,
            evidence=str(item.get("evidence") or ""), people=people)
        if not ok:
            rejected.append({"head": head, "tail": tail,
                             "intensity": intensity, "reason": reason})
            continue
        claims.append(RelationClaim(
            head=head,
            predicate=INTENSITY_TO_PREDICATE[intensity],
            tail=tail,
            label="probable",
            confidence={1: 0.5, 2: 0.6, 3: 0.7, 4: 0.8}[intensity],
            evidence=(),
            source="llm",
        ))
        if len(claims) >= max_claims:
            break
    return claims, rejected

def extract_statements_combined(
    text: str,
    settings: Any,
    *,
    context: list[str] | None = None,
    roster: list[str] | None = None,
    people: list[str] | None = None,
    use_llm: bool = True,
    max_claims: int = 1,
    client: Any | None = None,
) -> tuple[list[RelationClaim], list[dict]]:
    """规则 + LLM 双通道抽取（改造 A 的入口）。

    合并规则：
    - 规则命中 → `label=exact`（人工确认过的高置信句式）
    - 仅 LLM 命中 → `label=probable`
    - 同一 (head, predicate, tail) 取 exact（保住「exact 不被降级」不变量）

    Returns:
        `(claims, rejected)`。
    """
    body = (text or "").strip()
    if not body:
        return [], []
    candidates = candidate_entities(body, roster=roster)
    rejected: list[dict] = []

    rule_claims = extract_statements(body, candidates=candidates, rejected=rejected)
    if not use_llm:
        return rule_claims, rejected

    llm_claims, llm_rejected = extract_statements_llm(
        body, settings, context=context, roster=roster, people=people,
        max_claims=max_claims, client=client)
    rejected.extend(llm_rejected)

    by_key: dict[tuple[str, str, str], RelationClaim] = {}
    for claim in rule_claims + llm_claims:
        key = claim.key()
        cur = by_key.get(key)
        if cur is None or (cur.label != "exact" and claim.label == "exact"):
            by_key[key] = claim
    return list(by_key.values()), rejected

#: ⚠️ **不要**加「媒体占位不做尾值」的规则（`图片/信息/内容/视频…`）。
#:
#: 曾经加过，然后发现它跟人工标注直接冲突：2026-11-17 的 20 条机器边核对里
#: `kk -OWNS-> 图片`、`传讯员8号 -MAINTAINS-> 信息/内容`、`安澜 -OWNS-> 视频`
#: **都被判为「对」**。用户的语义是「发布某物的人负责维护该物的信息」，
#: 所以「图片」作为被维护的信息载体是合法的。
#:
#: 结论：媒体名词做尾值**允许**，噪声由人工裁决（见 spec 039 反悔清单）。

#: 动作词不能当**主语**：`训练 -OWNS-> 音频文件` 的主语是动作，不是行为主体。
#: 注意：当**尾值**时这些词是合法的（「部署＝那次部署」，见 spec 039 语义定义）。
_ACTION_HEAD_DENY = frozenset({
    "训练", "部署", "维护", "发布", "更新", "修复", "实现", "分享", "讨论",
    "上传", "下载", "安装", "配置", "编译", "测试", "运行", "启动", "重启",
})


def is_junk_relation(head: str, tail: str) -> str:
    """判断一条关系是否是噪声。返回原因，空串表示通过。

    只拦**主语是动作词**这一类。尾值的合法范围以人工标注为准，
    代码单方面收窄会与标注冲突（见上方注释）。
    """
    h = (head or "").strip()
    t = (tail or "").strip()
    if h in _ACTION_HEAD_DENY:
        return f"头 {h!r} 是动作词，不能当主语"
    if not t:
        return "尾为空"
    return ""

