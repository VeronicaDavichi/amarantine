"""B4 打标 ①② 层：规则层 + LLM 层。

两层分工（ADR D4）：

- **规则层**：关键词/正则命中，零成本、可复现、可解释 → 先跑
- **LLM 层**：结构化输出补长尾，候选集受 `TYPES_VOCAB` 约束 → 后跑

P0 决定：规则层**必须**独立可用（LLM 挂了也能写卡），LLM 层通过注入才启用。
这样 A21 的打标验收可以先用手工规则基线跑起来，再接 LLM 提升。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from memory.models.event_card import Entity

#: 规则层内置词表（`TYPES_VOCAB` 为空时才用）。类型值应保持稳定，便于回溯统计。
DEFAULT_TYPE_RULES: dict[str, tuple[str, ...]] = {
    "decision": ("决定", "定了", "拍板", "敲定", "确定为", "就这么办"),
    "work": ("项目", "需求", "上线", "版本", "排期", "会议", "汇报", "代码", "部署"),
    "study": ("学习", "考试", "课程", "论文", "读书", "练习"),
    "health": ("体检", "住院", "生病", "就医", "手术", "复查"),
    "relationship": ("朋友", "家人", "同事", "伴侣", "结婚", "分手", "吵架"),
    "emotion": ("开心", "难过", "生气", "焦虑", "兴奋", "沮丧", "委屈"),
    "travel": ("出差", "旅行", "航班", "酒店", "签证"),
    "money": ("借钱", "还款", "工资", "报销", "预算", "投资"),
}

#: 兜底类型：任何规则都没命中时使用（保证 types 非空，否则 EventCard 校验失败）
FALLBACK_TYPE = "general"

#: 常见中文姓氏（单姓 + 复姓）。用来做「主语」识别。
#: 不求全 —— 漏抽一个人只是少一条边，抽错会把人名写进图里污染。
_COMMON_SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
    "程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓"
    "牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭历戎祖武符刘景詹束龙"
    "叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴鬱胥能苍双"
    "闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍卻璩桑桂濮牛寿通边扈燕冀郏浦尚农"
    "温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘"
    "匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空"
    "曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
    # 复姓
    "欧阳太史端木上官司马东方独孤南宫万俟闻人夏侯诸葛尉迟公羊赫连"
    "澹台皇甫宗政濮阳公冶太叔申屠公孙慕容仲孙钟离长孙宇文司徒鲜于司空"
)

#: 谓语 / 助词 / 副词 —— 遇到就说明「前面是句子，不是实体名」。
#:
#: **这是整套抽取的关键**。中文没有空格，`[\u4e00-\u9fa5]{2,12}?项目` 这种写法
#: 会从句子开头一路吞到「项目」，于是 `张三决定项目A` 抽出 `张三决定项目`。
#: 正确做法是从后缀位置**往前扫，撞到谓语字就停**。
_STOP_CHARS = frozenset(
    # 助词 / 副词 / 介词
    "的了着过在是和与跟对把被让给向从到就都也还又很更最不没要会能可"
    # 高频动词
    "说讲谈提想叫请去来到回上下进出做成办搞弄干得"
    "定决延期推迟提前完成开始结束改变化调整新增删除修改确认讨论研究"
    # 情态 / 连词
    "安排计划准备打算希望要求需要应该必须所以但是因为如果虽然而且并且"
    # 代词
    "我你他她它您咱这那些"
)

#: 过度泛化的实体名，抽到也不要
_STOP_ENTITIES = frozenset(
    {
        "这个项目", "那个项目", "公司", "团队", "部门", "项目", "系统",
        "产品团队", "这个事情", "那个事情", "这件事", "那件事",
    }
)

#: 姓氏字开头、但实际是普通词的组合。
#:
#: `方`/`时`/`单`/`公`/`和`/`成`/`白`/`马`/`黄` 这些都是真姓氏，
#: 也都在 `方案`/`时间`/`单词`/`公司`/`和平`/`成功`/`白天`/`马上`/`黄色` 里出现。
#: 位置约束能挡住句中大部分误判，但句首的 `时间定在…` 挡不住。
#: 规则层做不到完美，这里穷举最高频的一批；剩下的交给 LLM 层补。
_NOT_PERSON_WORDS = frozenset(
    {
        "时间", "时候", "时机", "时常",
        "方案", "方向", "方式", "方法", "方面", "方便",
        "单词", "单位", "单独", "简单", "单据",
        "公司", "公平", "公开", "公主",
        "费用", "非常", "常见", "常常", "常规",
        "黄色", "黄金", "黄昏",
        "白色", "白天", "白菜",
        "马上", "马路",
        "和好", "和平", "和谐",
        "成功", "成了", "成人",
        "习惯", "系统",
        "文字", "文章", "文明",
        "林立", "林中",
        "车子", "车间",
        "关键", "关闭",
        "石头", "石油",
        "何时", "何况",
        "于是", "于今",
        "余额", "余下",
        "季度", "季节",
    }
)

#: 后缀 → 实体类型。从后缀往前扫，扫到停用字为止。
_SUFFIX_KINDS: tuple[tuple[str, str], ...] = (
    ("项目", "topic"),
    ("系统", "topic"),
    ("平台", "topic"),
    ("公司", "org"),
    ("团队", "org"),
    ("部门", "org"),
    ("集团", "org"),
    ("工作室", "org"),
    ("市", "place"),
    ("区", "place"),
    ("路", "place"),
    ("街", "place"),
    ("大厦", "place"),
)

#: 一个实体名最多往回取几个字（超过就不是名字了，是半句话）
MAX_NAME_LOOKBACK = 4

#: 带编号的项目/系统：`项目A` `系统2` —— 这类最准，不受分词影响
_CODED_PATTERN = re.compile(
    r"((?:项目|系统|产品|平台|版本)[A-Za-z0-9]+|[A-Za-z0-9]+(?:项目|系统))"
)

#: 人名允许出现的位置：句首，或紧跟这些字之后
_PERSON_BOUNDARY = "和与跟被让问找见是对向给及同、，,。；; ：:"

#: 动词表：同义词归并到同一个归一化动词。
#:
#: **必须归并** —— `延期`/`推迟`/`延后` 都建成节点的话，图上就是三个孤立节点，
#: 「哪些事被延过」这种聚合查询直接废掉。归并后一个 `:Action {verb:"延期"}`
#: 就能把三种说法都收进来。
_VERB_SYNONYMS: dict[str, str] = {
    # 延期类
    "延期": "延期", "推迟": "延期", "延后": "延期", "顺延": "延期",
    # 决定类
    "决定": "决定", "定了": "决定", "拍板": "决定", "敲定": "决定",
    "确定": "确定", "确认": "确认",
    # 推进类
    "开始": "开始", "启动": "开始", "开工": "开始",
    "完成": "完成", "搞定": "完成", "交付": "完成",
    "取消": "取消", "终止": "取消", "砍掉": "取消",
    # 沟通类
    "讨论": "讨论", "商量": "讨论", "沟通": "讨论", "聊": "讨论",
    "汇报": "汇报", "反馈": "汇报",
    "批评": "批评", "指责": "批评", "批评了": "批评",
    "同意": "同意", "批准": "同意", "通过": "同意",
    "拒绝": "拒绝", "否决": "拒绝",
    # 变更类
    "新增": "新增", "添加": "新增", "增加": "新增",
    "删除": "删除", "删掉": "删除", "移除": "删除",
    "修改": "修改", "调整": "修改", "改动": "修改",
    # 学习类
    "学习": "学习", "复习": "学习", "背": "学习",
    "考试": "考试", "考": "考试",
    # 财务类
    "投资": "投资", "融资": "融资", "报销": "报销", "借款": "借款",
    # 移动类
    "出差": "出差", "去了": "去", "去": "去", "来到": "去",
}

#: 一个事件最多保留几个动作（防长文本抽出一串动词把图打散）
MAX_ACTIONS = 5


@dataclass(frozen=True)
class ActionMatch:
    """规则层抽到的一个动作 + 它在原文里的位置（用来绑宾语）。"""

    verb: str
    start: int
    end: int


def _has_stop_char(text: str) -> bool:
    """候选里出现谓语/助词，说明它是被截断的句子片段，不是实体名。"""
    return any(ch in _STOP_CHARS for ch in text)


def _scan_backward(text: str, end: int, *, min_len: int) -> str | None:
    """从 `end` 往前扫，取一段连续中文，撞到停用字/非中文就停。

    这是解决「张三决定项目A」被抽成「张三决定项目」的核心：
    不从句子开头匹配，而是**从后缀边界往前收**，收到谓语就停手。
    """
    start = end
    for i in range(1, MAX_NAME_LOOKBACK + 1):
        pos = end - i
        if pos < 0:
            break
        ch = text[pos]
        if not ("\u4e00" <= ch <= "\u9fa5"):
            break
        if ch in _STOP_CHARS:
            break
        start = pos
    if end - start < min_len:
        return None
    return text[start:end]


def _extract_by_suffix(summary: str) -> list[Entity]:
    """后缀驱动的实体抽取：先定位后缀，再往前收名字。"""
    found: list[Entity] = []
    taken: set[tuple[int, int]] = set()  # 已被更长的后缀覆盖的区间

    # 长后缀优先（`工作室` 要先于 `室`），避免被短后缀抢先
    for suffix, kind in sorted(_SUFFIX_KINDS, key=lambda kv: -len(kv[0])):
        for match in re.finditer(re.escape(suffix), summary):
            span = (match.start(), match.end())
            if any(s <= span[0] and span[1] <= e for s, e in taken):
                continue
            # `项目A` 这类带编号的整串已经抽过，别再拆出 `项目`
            if suffix in ("项目", "系统") and summary[match.start():match.end() + 1][-1:].isalnum():
                continue
            prefix = _scan_backward(summary, match.start(), min_len=2)
            if prefix is None:
                continue
            name = prefix + suffix
            taken.add((len(summary) - len(name), match.end()))
            found.append(Entity(name=name, kind=kind))
    return found


def _extract_persons(summary: str) -> list[str]:
    """抽人名：姓氏开头 + 位置约束 + 停用字过滤。

    位置约束是必要的 —— 姓氏表里`方`/`时`/`单`/`公`这些字都是常用字，
    不约束位置的话 `方案`/`时间`/`单词`/`公司` 全会被当成人名。
    """
    names: list[str] = []
    surnames = set(_COMMON_SURNAMES)

    for idx, ch in enumerate(summary):
        if ch not in surnames:
            continue
        # 位置约束：句首，或紧跟在连词/介词/标点之后
        at_start = idx == 0
        after_boundary = idx > 0 and summary[idx - 1] in _PERSON_BOUNDARY
        if not (at_start or after_boundary):
            continue

        # 往后取 1–2 个字，撞到停用字就停
        end = idx + 1
        for nxt in range(1, 3):
            pos = idx + nxt
            if pos >= len(summary):
                break
            c = summary[pos]
            if not ("\u4e00" <= c <= "\u9fa5") or c in _STOP_CHARS:
                break
            end = pos + 1

        name = summary[idx:end]
        if not (2 <= len(name) <= 3):
            continue
        if _has_stop_char(name) or name in _STOP_ENTITIES:
            continue
        if name in _NOT_PERSON_WORDS:
            continue
        if name not in names:
            names.append(name)
    return names


def _extract_entities(summary: str, *, skip: set[str] | None = None) -> list[Entity]:
    """抽非人实体，去重且保序。

    顺序有讲究：先抽「带编号的项目」（最准，不受分词影响），
    再走后缀扫描。已被当作人名的串会跳过，避免同一串既是人又是项目。
    """
    skip = skip or set()
    seen: set[str] = set(skip)
    entities: list[Entity] = []

    def _add(name: str, kind: str) -> None:
        name = name.strip()
        if len(name) < 2 or name in seen or name in _STOP_ENTITIES:
            return
        if _has_stop_char(name):
            return
        seen.add(name)
        entities.append(Entity(name=name, kind=kind))

    for match in _CODED_PATTERN.finditer(summary):
        _add(match.group(1), "topic")

    for entity in _extract_by_suffix(summary):
        _add(entity.name, entity.kind)

    return entities


def extract_entities(text: str) -> list[Entity]:
    """从自由文本抽实体（召回时从 query 里抽，用于图通道）。

    与打标共用同一套规则，保证「写入时抽到的实体」和「查询时抽到的实体」
    口径一致——否则图通道会系统性漏检。
    """
    persons = _extract_persons(text)
    entities = _extract_entities(text, skip=set(persons))
    return [Entity(name=n, kind="person") for n in persons] + entities


def _match_verbs(summary: str) -> list[ActionMatch]:
    """按动词表扫描，长的优先（`延期` 要先于 `延`）。

    同义词归并到 `_VERB_SYNONYMS` 的归一化动词，这样 `推迟`/`延后`/`延期`
    在图上都是同一个 Action 节点。

    **跨度重叠要去掉**：`李四确定了…` 里 `确定`(2,4) 和 `定了`(3,5) 都命中，
    不去重会同时抽出「确定」和「决定」两个动作 —— 同一句话说了两遍。
    """
    candidates: list[tuple[int, int, str]] = []

    for surface, verb in sorted(_VERB_SYNONYMS.items(), key=lambda kv: -len(kv[0])):
        for match in re.finditer(re.escape(surface), summary):
            candidates.append((match.start(), match.end(), verb))

    # 长的优先；等长时靠前的优先。已被占用区间覆盖的直接丢。
    candidates.sort(key=lambda c: (-(c[1] - c[0]), c[0]))
    taken: list[tuple[int, int]] = []
    kept: list[ActionMatch] = []
    for start, end, verb in candidates:
        if any(s < end and start < e for s, e in taken):  # 有交集就算覆盖
            continue
        taken.append((start, end))
        kept.append(ActionMatch(verb=verb, start=start, end=end))

    kept.sort(key=lambda a: a.start)
    return kept


def _bind_patient(
    action: ActionMatch, *, summary: str, entities: list[Entity]
) -> str | None:
    """给动作绑宾语：优先取动词**之后**最近的实体，没有就往前找。

    为什么两个方向都要看：中文口语里宾语常被话题化到动词前面。
    `项目A延期了` 和 `延期项目A` 是同一个意思，但宾语位置相反。
    """
    after: Entity | None = None
    after_dist = len(summary) + 1
    before: Entity | None = None
    before_dist = len(summary) + 1

    for ent in entities:
        pos = summary.find(ent.name)
        if pos < 0:
            continue
        if pos >= action.end:
            dist = pos - action.end
            if dist < after_dist:
                after_dist = dist
                after = ent
        elif pos + len(ent.name) <= action.start:
            dist = action.start - (pos + len(ent.name))
            if dist < before_dist:
                before_dist = dist
                before = ent

    pick = after or before
    return pick.name if pick else None


def extract_actions(summary: str, *, entities: list[Entity] | None = None) -> list[Action]:
    """从文本抽动作。

    Args:
        summary: 事件摘要。
        entities: 已抽出的实体（用于绑宾语）。不传就现抽一遍。

    Returns:
        按出现顺序排列的 Action；第一个是 `primary=True`。
    """
    from memory.models.event_card import Action

    matches = _match_verbs(summary)
    if not matches:
        return []

    pool = entities if entities is not None else extract_entities(summary)
    actions: list[Action] = []
    seen: set[str] = set()
    for idx, m in enumerate(matches[:MAX_ACTIONS]):
        if m.verb in seen:
            continue
        seen.add(m.verb)
        actions.append(
            Action(
                verb=m.verb,
                patient=_bind_patient(m, summary=summary, entities=pool),
                primary=idx == 0,
            )
        )
    return actions


@dataclass(frozen=True)
class LabelResult:
    """打标产物。`source` 便于统计规则命中率，也便于 A22 的双跑对比。"""

    types: list[str] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    #: 动作（谁对谁做了什么）。**不参与 event_id 哈希**。
    actions: list[object] = field(default_factory=list)
    source: str = "rules"


def _match_types(summary: str, vocab: list[str]) -> list[str]:
    """按词表匹配类型；命中多个时全保留（多类型在 ADR 里是允许的）。"""
    rules = {k: v for k, v in DEFAULT_TYPE_RULES.items() if not vocab or k in vocab}
    if vocab:
        rules = {k: DEFAULT_TYPE_RULES.get(k, ()) for k in vocab}
    hits = [
        type_name
        for type_name, keywords in rules.items()
        if any(keyword in summary for keyword in keywords)
    ]
    return hits or [FALLBACK_TYPE]


def label_by_rules(summary: str, *, vocab: list[str] | None = None) -> LabelResult:
    """规则层打标：永远给出非空 types（否则事件卡无法落库）。"""
    types = _match_types(summary, vocab or [])
    entities = extract_entities(summary)
    actions = extract_actions(summary, entities=entities)
    return LabelResult(
        types=types, entities=entities, actions=list(actions), source="rules"
    )


def label_event(
    summary: str,
    *,
    vocab: list[str] | None = None,
    llm_labeler: object | None = None,
) -> LabelResult:
    """两层打标入口。

    Args:
        summary: 事件摘要。
        vocab: `TYPES_VOCAB`；为空表示用内置词表。
        llm_labeler: 可选的可调用对象，签名 ``(summary, candidates) -> LabelResult | None``。
            返回 None 时回退规则层结果（LLM 失败不该阻断写入）。

    Returns:
        LabelResult；types 保证非空。
    """
    base = label_by_rules(summary, vocab=vocab)
    if llm_labeler is None:
        return base

    try:
        refined = llm_labeler(summary, base.types)  # type: ignore[operator]
    except Exception:  # noqa: BLE001 - LLM 层失败必须降级，不影响写卡
        return base

    if not isinstance(refined, LabelResult) or not refined.types:
        return base
    # 候选集约束：LLM 只能从词表里挑，防止自由生成
    allowed = set(vocab) if vocab else set(DEFAULT_TYPE_RULES) | {FALLBACK_TYPE}
    filtered = [t for t in refined.types if t in allowed] or base.types
    return LabelResult(types=filtered, entities=refined.entities or base.entities, source="llm")
