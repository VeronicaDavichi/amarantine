"""B4 打标 ①② 层：规则层 + LLM 层。

两层分工（ADR D4）：

- **规则层**：关键词/正则命中，零成本、可复现、可解释 → 先跑
- **LLM 层**：结构化输出补长尾，候选集受 `TYPES_VOCAB` 约束 → 后跑

P0 决定：规则层**必须**独立可用（LLM 挂了也能写卡），LLM 层通过注入才启用。
这样 A21 的打标验收可以先用手工规则基线跑起来，再接 LLM 提升。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from memory.models.event_card import (
    ALIAS_EXACT,
    ALIAS_UNCERTAIN,
    AliasClaim,
    Entity,
)

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

#: 复姓（双字）。`_COMMON_SURNAMES` 的大字符串里也包含它们，
#: 这里单独列出来是因为「姓 + 单字名」的结构判定要区分单姓和复姓：
#: 「欧阳锋」是复姓+单字名，「李小狼」是单姓+双字名，两者长度相同但结构不同。
COMPOUND_SURNAMES: tuple[str, ...] = (
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟",
    "闻人", "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政",
    "濮阳", "公冶", "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙",
    "宇文", "司徒", "鲜于", "司空",
)

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
    "批评估判阅审问询查找点看听闻写读算测量查验证明"
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

#: 人名允许出现的位置：句首，或紧跟这些字之后。
#:
#: `了` 也在里面：`张三批评了李四` 的 `李四` 就在助词后面，
#: 这是口语里最常见的引入新人的方式，漏了它等于漏掉半句对话。
_PERSON_BOUNDARY = "和与跟被让问找见是对向给及同了、，,。；; ：:"

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

#: 虚化动词：抽到也不要。`到`/`去`/`做`/`要`/`有` 这些不承载事件语义，
#: 建成 Action 节点只会污染图，让「延期」这类真动词淹没在噪声里。
WEAK_VERBS = frozenset(
    {
        "到", "去", "做", "要", "有", "是", "在", "来", "过", "对", "给", "让",
        "被", "把", "从", "向", "为", "和", "与", "跟", "及", "或", "但", "而",
        "能", "会", "可", "该", "将", "才", "就", "都", "也", "还", "又",
        "说", "想", "看", "见", "用", "开", "起", "下", "上", "出", "进", "成",
        "见", "算", "完", "好", "没", "不", "很", "太", "更", "最",
    }
)

#: jieba 人名尾部常被误吞的函数词（`张三让` → `张三`）
_PERSON_TAIL_CHARS = frozenset("让的被给和与跟对把向从在是有个这那也都还又就")

#: 人名最短长度。单字人名噪声太大（`周会上` 的 `周` 会被标成 nr）
MIN_PERSON_LEN = 2

#: 排行称谓：`三哥` `四妹` `三叔` `一姐`……
#:
#: jieba 词典里没有这一批，`三哥是我哥` 会被切成 `三/m` + `哥/n`，
#: 于是整句抽不到人（实测）。而这类词的模式**极明确**：
#: 「三哥」不可能是别的东西，误伤几乎为零。
#:
#: 刻意**不做「老X」「小X」**：`小鹏`/`小米`/`小说`/`老总` 全是坑，
#: 姓氏表再全也躲不掉。排行模式没这个问题。
_RANK_DIGITS = "一二三四五六七八九十"
_RANK_SUFFIXES = "哥姐弟妹叔伯婶舅姨"

#: 切分结果里要剥掉的连词前缀。
#:
#: `老李和小王吵架` 会被切成 `老李/nr` + `和小王/nr` —— 「和」粘进了名字，
#: `和小王` 就这么进图了。剥前缀只在剥完仍是合格名字时才动手，
#: 否则 `和平` 会被剥成 `平`。
_LEADING_CONJ = ("和", "与", "跟", "及", "同")

#: 别名声明模式：`A 就是 B` / `A 也叫 B` / `A 又称 B` / `A 也就是 B`
#:
#: 只认**显式的等价词**。`A 是 B` 不算 —— `张三是个好人` 不是别名声明。
_ALIAS_PATTERN = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9_]{2,12})\s*"
    # 交替**按长度降序**：`也叫作` 必须排在 `也叫` 前面。
    # 否则短词先匹配，把 `作` 甩到右边，抽出 `李四 -> 作小李`。
    r"(?:也叫作|也就是|也叫|又称|亦称|又叫|就是|即)\s*"
    r"([\u4e00-\u9fa5A-Za-z0-9_]{2,12})"
)

#: 描述性内容里常见的**多字**词。出现它们说明这不是一个干净的名字，
#: 而是「那个……的东西」，得靠推理才能落到具体名字上 → 置信度降级。
#:
#: ## 这里一个单字都不能有
#:
#: 判据是**包含式**匹配（`marker in candidate`），而汉语单字在构词里
#: 会换角色 —— 同一个字单用时和当零件时不是一类东西：
#:
#:     家  单用是量词 → 科学家 / 作家 / 国家 里是类词缀
#:     人  单用是名词 → 印度人 / 主持人 里是类词缀
#:     个  单用是量词 → 个别 / 个体 里是名语素
#:     种  单用是量词 → 种类 / 种族 里是名语素
#:     位  单用是量词 → 单位 / 位置 里是名语素
#:
#: 依据两条：
#:
#: 1. 中央研究院《詞類標記表》把词类分成封闭类（D 副詞 / Nep 指代定詞 /
#:    C 連接詞 / P 介詞 / T 語助詞）与开放类（N 名詞 / A 形容詞 /
#:    Nf 量詞 / V 動詞）。封闭类不参与构词，开放类参与。
#: 2. 吕叔湘《說自由和粘著》用「能不能单用」区分自由语素与粘着语素。
#:    `家`/`人`/`个`/`种`/`位` 全是**自由语素** —— 既能单用成词，又能
#:    构词，位置不固定。自由语素一旦进包含式匹配，必然在名词里误伤。
#:
#: 所以规则是：
#:
#:   **单字只能做位置检查（开头/结尾），包含式匹配只能用于多字词。**
#:
#: 这条规则本来就在代码里用了两处（`_LEADING_CHARS` 查开头、下面的
#: 结尾助词表查结尾），只有这个集合是唯一异类 —— 也是
#: 「`三哥就是印度人` 建不出边」和「`科学家` 被当成描述」的共同源头。
#:
#: `tests/test_alias.py` 有断言锁死：`all(len(m) >= 2 for m in ...)`。
_DESCRIPTIVE_MARKERS = frozenset(
    {
        # 指示 / 代词（封闭类）
        "那个", "这个", "这种", "那种", "这些", "那些", "哪种",
        "一个", "某个", "整个", "每个",
        "这位", "那位", "各位", "诸位",
        "什么", "任何", "别的", "其他", "其它", "有些", "很多", "某种",
        "人家", "大家",
        # 副词（封闭类）
        "特别", "非常", "比较", "稍微", "极其", "十分", "相当",
        "格外", "尤其", "甚至", "简直", "几乎", "大概", "可能",
        # 名词，但语义固定指物/指人，不可能当名字
        "东西", "家伙",
        # 后置词
        "之一", "等等", "的话", "而已", "罢了",
        # 「家」的实词实现 —— 用具体词，不用单字规则
        "家里", "家人", "家门口", "家庭", "家中", "家族",
    }
)

#: 「X 人」里 X 是形容词时，整个短语是**评价**而不是名字。
#:
#: `人` 曾经直接在 `_DESCRIPTIVE_MARKERS` 里，一刀切拦掉了所有
#: 带「人」的串 —— 包括 `印度人`。但汉典把 `人` 列为**名词**，举证里
#: 明确有 `主持人` `介绍人` `证人` `强人` `恩人` —— 说明 `X人` 是
#: **能产构词**（类词缀），造出来的是合法名词。
#:
#:     名字（exact）                评价（降级为 uncertain）
#:     ─────────────────────      ─────────────────────────
#:     印度人 / 中国人 / 上海人     老实人 / 聪明人 / 坏人
#:     主持人 / 负责人 / 介绍人     懒人 / 陌生人 / 什么人
#:
#: **这里只降置信度，不再硬拒。** 区别很重要：
#:
#: - 硬拒 = 边建不出来，而且**没有任何日志** —— 那正是当初 `印度人`
#:   那个 bug 的形态：claim 在卡上看得见，图上却什么都没有。
#: - 降级 = 边照建，只是置信度 0.4，召回不当强证据，`rebind`/撤销可清。
#:
#: 宁可让 `老实人` 当个低置信度实体，也不要再出现「用户说清楚了、
#: 图上却没有、还查不出为什么」。判得宽容，可修；判得严，看不见。
_DESCRIPTIVE_BEFORE_REN = frozenset(
    {
        # 评价类形容词
        "好", "坏", "笨", "傻", "呆", "懒", "聪明", "老实", "勤快", "靠谱",
        "漂亮", "丑", "帅", "可爱", "讨厌", "善良", "恶毒", "虚伪", "实在",
        "正经", "普通", "一般", "厉害", "牛逼", "有钱", "没钱", "穷", "富",
        "陌生", "熟悉", "年轻", "老", "小", "胖", "瘦", "高", "矮", "弱",
        # 限定词 —— `什么人` / `那种人` 同样不是名字
        "什么", "任何", "别的", "其他", "有些", "很多", "个别",
        "这种", "那种", "哪种", "每", "所有", "全部",
    }
)


def _is_descriptive_ren(candidate: str) -> bool:
    """`老实人` 是评价，`印度人` 是名字。

    判的是 `人` 前面那个词：形容词 → 描述，名词 → 放行。
    **只用来降置信度，不用来拒绝**（`人` 是能产的名词类词缀）。
    """
    if len(candidate) < 2 or not candidate.endswith("人"):
        return False
    return candidate[:-1] in _DESCRIPTIVE_BEFORE_REN


#: 别名左侧常被误带进来的前缀（`我说的豆包` → `豆包`）
_ALIAS_PREFIXES = ("我说的", "我说的是", "就是说", "那个", "这个", "叫做", "称为")

#: 开头的代词 —— 出现就说明是句子片段，不是名字
_PRONOUN_CHARS = frozenset("我你他她它您咱这那")

#: 开头的单字副词 —— 同样是位置检查，不是包含检查。
#:
#: 放在开头判是安全的：这些字不参与构词，不会藏在名词中间。
#: 反过来说，如果拿 `最` 做包含式匹配，会命中 `最近`/`最终`/`最初`；
#: 做开头检查只命中 `最好之一` 这种真正的描述。
#:
#: 这些字也都不是姓氏，不会误伤真名。
_LEADING_ADVERBS = frozenset("最很太更挺蛮极已刚才就都也还又再不没别")

#: 开头的单字量词 —— 同样是位置检查。
#:
#: `小明就是个老实人` 会被正则捕获成 `个老实人`（`就是` 后面紧跟量词），
#: 抓到的不是名字而是半截短语。`位`/`种` 同理（`就是位老师` / `就是种人`）。
#:
#: `个`/`位` 没有姓氏风险。`种` 是罕见姓（`种师道`，读 chóng）—— 这里
#: 选择把它也当量词拦掉：`种` 出现在别名候选开头时几乎必然是量词，
#: 而丢掉一个罕见姓氏的别名边是可接受的代价（且 claim 仍在卡上）。
_LEADING_MEASURES = frozenset("个位种")

#: 位置检查：开头出现这些字，就不是名字。
#:
#: 单字判据只走这一条路（另一条是 `_judge_name` 里的结尾检查）。
#: 三条都只看首尾，**不做包含式匹配** —— 见 `_DESCRIPTIVE_MARKERS`。
_LEADING_CHARS = _PRONOUN_CHARS | _LEADING_ADVERBS | _LEADING_MEASURES


#: 位置检查：结尾出现这些助词/描述词，就不是名字（`三哥主打的` / `前面说的`）
_DESCRIPTIVE_ENDINGS = frozenset("的者地了呢吧啊")


#: 「这人姓种」—— `姓X` 且 X 后面**不接汉字**。
#:
#: 为什么要求后面不接汉字：`姓` 是高频构词字，
#: `姓名` / `姓氏` / `老百姓` / `百家姓` 都会误命中。
#: 只认句末/标点前的形式，代价是 `这人姓种啊` 认不出来 ——
#: 那种情况仍然会进 `alias_reject`（`reason="leading"`），看得见、能补。
SURNAME_DECL_RE = re.compile(r"姓\s*([\u4e00-\u9fa5])(?![\u4e00-\u9fa5])")

#: `姓` 后面跟着这些字时不是姓氏声明（`姓名` / `姓氏`）
_NOT_SURNAME_CHARS = frozenset("名氏")


def parse_surname_declarations(text: str) -> list[str]:
    """从原话里抽姓氏声明。返回去重后的单字列表（保持出现顺序）。

    捕获到的字还要再过一道 `_NOT_SURNAME_CHARS`：`姓名` / `姓氏` 里
    `姓` 后面确实跟着一个汉字，而且它后面没有汉字 —— 正则拦不住，
    只能按字排掉。
    """
    out: list[str] = []
    for match in SURNAME_DECL_RE.finditer(text):
        ch = match.group(1)
        if ch in _NOT_SURNAME_CHARS:
            continue
        if ch not in out:
            out.append(ch)
    return out


# ==================== 罕见姓豁免 ====================
# 「量词打头」这条判据会误伤罕见姓：`种师道`（种读 chóng）是真人名，
# 但 `种` 就在 `_LEADING_MEASURES` 里。两条独立的豁免路径：
#
# 1. **jieba 词典**：整串就是词典里的单一人名词条（`nr`）。
#    `种师道` → `[('种师道','nr')]`；`种人` → `[('种人','n')]`。
#    免费，且覆盖词典里所有人名。
# 2. **用户声明**：`这人姓种` 这种话，解析后登记成姓氏。
#    jieba 不认识的生僻姓（`姓种，种小明`）靠这条。
#
# 两条都只**豁免量词那一条**，不放宽别的判据。

#: 在 `_LEADING_MEASURES` 里、**同时是姓氏**的字。
#:
#: 只有这类字有机会走豁免。`个`/`位` 不是姓氏，所以 `位小明` 即使被
#: jieba 的 HMM 误标成 `nr` 也不放行 —— 实测 jieba 确实会把 `位小明`
#: 标成 `nr`，没有这层过滤，豁免就变成「量词后面跟个像名字的就行」。
#:
#: 汉典 `种` 条：`●种 chóng — 姓。如宋代有种师道。`
_MEASURE_SURNAMES = frozenset("种")

#: 本次进程内已登记的姓氏（启动时从 `surname_decl` 表灌进来）
_DECLARED_SURNAMES: set[str] = set()

#: 权威名册（spec 035）：数据源明确给出的名字列表。
#:
#: 为什么需要它：`extract_entities("孙柏林")` 会被 jieba 切成
#: `[('孙','zg'), ('柏林','nr')]` —— 「柏林」是德国首都，在词典里，
#: 于是姓氏被丢掉，实体名变成「柏林」。同类还测出 7/14 坏：
#:
#:   孙柏林 -> 柏林      刘北京 -> 北京      陈上海 -> 上海
#:   李长春 -> []        李向前 -> []        王光明 -> []      赵和平 -> []
#:
#: 「整个人消失」比「名字不对」更严重 —— 那个人在所有记忆里都不存在。
#:
#: 名册来自数据源本身（DingTalk 的 contacts / 群标题 / sender_name），
#: 从这些出来的字符串**就是人名**，不需要用通用词典去猜。
#:
#: 和 `_DECLARED_SURNAMES` 同一个模式：模块级状态 + 注册函数 + 测试用重置。
_ROSTER: set[str] = set()


def roster_names() -> frozenset[str]:
    """当前生效的权威名册（只读快照）。"""
    return frozenset(_ROSTER)


def register_roster(names: object) -> int:
    """登记权威名册。返回新登记了几个（重复的不算）。

    只收长度 >= 2 的字符串 —— 单字不可能是完整人名（且会误伤大量普通词）。
    """
    fresh = {
        n.strip()
        for n in names  # type: ignore[union-attr]
        if isinstance(n, str) and len(n.strip()) >= 2
    } - _ROSTER
    _ROSTER.update(fresh)
    return len(fresh)


def reset_roster() -> None:
    """清空名册（测试用）。"""
    _ROSTER.clear()


def _extract_with_roster(text: str) -> tuple[list["Entity"], str]:
    """在文本里找名册中的名字，返回 (命中的实体, 挖掉后的剩余文本)。

    **最长优先**：名册里同时有「柏林」和「孙柏林」时，文本「孙柏林」
    必须只出一个「孙柏林」，不能出「柏林」。

    **挖掉**：命中的片段要从文本里删除。不删的话 jieba 会在剩余文本里
    再抽一次「柏林」，同一个人变成两条记录。
    """
    if not _ROSTER:
        return [], text

    # 长的排前面，保证最长优先
    candidates = sorted(_ROSTER, key=len, reverse=True)
    found: list[Entity] = []
    remaining = text
    for name in candidates:
        if name in remaining:
            found.append(Entity(name=name, kind="person"))
            remaining = remaining.replace(name, "\x00")  # 占位，防止拼接出新词
    return found, remaining.replace("\x00", " ")


def split_name_surname(word: str) -> str | None:
    """若 `word` 是「姓 + **单字**名」结构，返回姓；否则返回 None。

    只看结构，不看语义：
    - 「张三」→ "张"（单姓 + 单字名）
    - 「欧阳锋」→ "欧阳"（复姓 + 单字名）
    - 「李小狼」→ None（单姓 + **双**字名）
    - 「豆包」→ None（`豆` 不是姓）

    用途见 `alias_resolver.single_char_name_prefix_conflict`：中文里
    「张三」和「张三丰」字面极像但**通常不是同一人**，这个结构判定是
    唯一能把它和「豆包 / 豆包AI」（同人）分开的可靠信号。
    """
    if len(word) == 2 and word[0] in _COMMON_SURNAMES:
        return word[0]
    if len(word) == 3 and word[:2] in COMPOUND_SURNAMES:
        return word[:2]
    return None


def declared_surnames() -> frozenset[str]:
    """当前生效的已声明姓氏（只读快照）。"""
    return frozenset(_DECLARED_SURNAMES)


def register_surnames(chars: object) -> int:
    """登记姓氏。返回新登记了几个（重复的不算）。"""
    fresh = {c for c in chars if isinstance(c, str) and len(c) == 1} - _DECLARED_SURNAMES
    _DECLARED_SURNAMES.update(fresh)
    return len(fresh)


def reset_surnames() -> None:
    """清空登记（测试用；也供启动时重新加载）。"""
    _DECLARED_SURNAMES.clear()


def _is_person_name_token(candidate: str) -> bool:
    """整串是不是 jieba 词典里的**单一**人名词条（`nr`）。

    只在「首字是量词」这条窄路才被调用，所以开销可以忽略。
    要求「单一词条且覆盖整串」，避免 `种师道也叫老种` 这种句子蒙混过关。
    """
    if not _jieba_ready():
        return False
    try:
        import jieba.posseg as pseg

        pairs = [(p.word, p.flag) for p in pseg.cut(candidate)]
    except Exception:  # noqa: BLE001 - 词典挂了不该影响判据
        return False
    return (
        len(pairs) == 1
        and pairs[0][0] == candidate
        and str(pairs[0][1]).startswith("nr")
    )


def _measure_word_exempt(candidate: str) -> bool:
    """量词打头的例外：这其实是罕见姓。

    **先看这个量词字是不是姓氏**，不是就直接不豁免 —— 否则 jieba 的
    HMM 会把 `位小明` 这种也标成 `nr`，豁免就漏成筛子。

    是姓氏时，两个条件任一成立：
    - 用户在 namespace 里声明过它，且**名字至少 3 个字**（姓 + 名≥2）；
    - jieba 词典认得整串是人名。

    2 个字（`种明`）且 jieba 不认识的情况判不准，选择不豁免 ——
    但它会进 `alias_reject` 表，`reason="leading"`，看得见、可修。
    """
    first = candidate[:1]
    if first not in _MEASURE_SURNAMES and first not in _DECLARED_SURNAMES:
        return False
    if first in _DECLARED_SURNAMES and len(candidate) >= 3:
        return True
    return _is_person_name_token(candidate)


def _leading_blocks(candidate: str) -> tuple[bool, str]:
    """开头位置检查。返回 `(是否拦住, 命中的字符)`。"""
    first = candidate[:1]
    if first not in _LEADING_CHARS:
        return False, ""
    if first in _LEADING_MEASURES and _measure_word_exempt(candidate):
        return False, ""
    return True, first


# ==================== 裁决：带理由，不是布尔 ====================
#
# 原来判据只回答「过 / 不过」。不过的时候**什么都不留**，于是
# `印度人` 建不出边这件事，花了两个会话、查了真库才发现。
# 现在每一次判定都带 `reason`，`reason` 会进 `alias_reject` 表。

NAME_OK = "ok"
NAME_EMPTY = "empty"
NAME_TOO_LONG = "too_long"
NAME_MARKER = "marker"
NAME_LEADING = "leading"
NAME_ENDING = "ending"

#: 所有可能的拒绝理由 —— 文档和 `alias_reject.reason` 共用这套词
NAME_REJECT_REASONS = (
    NAME_EMPTY,
    NAME_TOO_LONG,
    NAME_MARKER,
    NAME_LEADING,
    NAME_ENDING,
)


@dataclass(frozen=True)
class NameVerdict:
    """一个串能不能当名字，以及**为什么**。

    `detail` 放具体的证据：命中的标记 / 开头的那个字。
    没有它，`alias_reject` 表只能说「被拒了」，说不出被什么拒的。
    """

    ok: bool
    reason: str = NAME_OK
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "reason": self.reason, "detail": self.detail}


def _judge_name(candidate: str) -> NameVerdict:
    """名字判据的**唯一实现**。`_is_clean_name` 只是它的布尔壳。"""
    if not candidate:
        return NameVerdict(False, NAME_EMPTY)
    if len(candidate) > 6:
        return NameVerdict(False, NAME_TOO_LONG, str(len(candidate)))
    marker = _first_marker(candidate)
    if marker is not None:
        return NameVerdict(False, NAME_MARKER, marker)
    blocked, hit = _leading_blocks(candidate)
    if blocked:
        return NameVerdict(False, NAME_LEADING, hit)
    if candidate[-1:] in _DESCRIPTIVE_ENDINGS:
        return NameVerdict(False, NAME_ENDING, candidate[-1:])
    return NameVerdict(True)


def _first_marker(candidate: str) -> str | None:
    """命中的描述标记里挑一个**确定性**的。

    `_DESCRIPTIVE_MARKERS` 是 frozenset，`next(iter(...))` 的结果随
    字符串哈希种子变化（每个进程不同）。而 `detail` 是要落
    `alias_reject` 表的 —— 让它随机，等于「同一句话两次记下不同理由」，
    查起来会莫名其妙。

    排序键：最早出现 → 最长 → 字典序最小。
    """
    hits = [m for m in _DESCRIPTIVE_MARKERS if m in candidate]
    if not hits:
        return None
    return min(hits, key=lambda m: (candidate.index(m), -len(m), m))


@dataclass(frozen=True)
class AliasReject:
    """一条被拒的别名声明 —— 带理由，不静默。

    `side` 区分是哪一头被拒：

    - ``alias``：左值（`三哥主打的`）。拒绝发生在 `extract_alias_claims`，
      这样的串**连 claim 都不会生成**，所以必须单独记。
    - ``canonical``：右值（`那个ai中最弱智的`）。claim 会生成、留在卡上，
      只是建边时被跳过 —— 记下来，让 LLM/人能看见。
    """

    alias: str
    canonical: str
    side: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "alias": self.alias,
            "canonical": self.canonical,
            "side": self.side,
            "reason": self.reason,
            "detail": self.detail,
        }


def _strip_prefix(name: str) -> str:
    for prefix in _ALIAS_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix) :]
    return name


def _looks_like_name(candidate: str) -> bool:
    """这个串能不能当名字用（左右两侧同一个判据）。

    `印度人` 像名字（它是个名词短语），`那个ai中最弱智的` 不像 ——
    它是描述。`老实人` **也算名字**（`X人` 是能产构词），只是会被
    `_alias_label` 降成 uncertain。

    ## 单字只做位置检查，包含检查只用于多字词

    汉语单字在构词里会换角色：`家` 单用是量词，在 `科学家` 里是类词缀；
    `人` 单用是名词，在 `印度人` 里是类词缀。所以单字判据只能看**位置**
    （开头 / 结尾 / 是否紧邻某个方位字），不能看**包含** —— 一看包含
    就会在名词里误伤，而且误伤之后没有任何痕迹。

    这条规则的具体落点：
    - 开头：`_LEADING_CHARS`（代词 + 单字副词 + 单字量词）
    - 结尾：`_DESCRIPTIVE_ENDINGS`
    - 包含：只有 `_DESCRIPTIVE_MARKERS`，且**一个单字都没有**

    实现只有一份：`_judge_name`。这个函数和 `_is_clean_name` 都是它的
    布尔壳，两处判据永远一致（以前分成两个函数，`_looks_like_name` 少查
    开头，改一处忘另一处就会不一致）。

    ## 为什么要卡左侧

    `三哥主打的就是有声有色有味` 会匹配成 `三哥主打的 -> 有声有色有味`。
    右边过了判据，于是建出 `(:Alias {name:"三哥主打的"})` ——
    **一个句片短语进了图**，而且「有声有色有味」被当成实体。

    左侧才是最容易脏的一侧：它紧挨着等价词，正则 `{2,12}` 会
    一路吞到谓语为止。
    """
    if not candidate:
        return False
    return _judge_name(candidate).ok


def _is_clean_name(candidate: str) -> bool:
    """能不能作为名字写入图 —— `_judge_name` 的布尔壳。

    要理由的时候直接调 `_judge_name`。保留这个名字是因为它散落在
    `graph_outbox` / `extract_alias_claims` 里，全是「过不过」的用法。
    """
    return _judge_name(candidate).ok


def _alias_label(canonical: str) -> str:
    """判断一条别名声明的置信度。

    判据是**右边是不是一个干净的名字**：

    - `糖包就是豆包` → `豆包` 干净 → ``exact``（用户直接给了等价关系）
    - `糖包就是那个ai中最弱智的` → 右边是描述 → ``uncertain``
      （得先知道「最弱智的 AI」是谁，才能推出「糖包 = 豆包」，这一步会错）

    描述性右边界**不直接丢弃** —— 它仍是线索，只是要打上存疑标记，
    召回时让调用方自己去判断。

    `印度人` 这种名词短语是 ``exact``：用户用「就是」把等价关系说死了。
    `老实人` 是评价，退回 ``uncertain``（`_is_descriptive_ren`）。

    注意这里查的是**置信度**，不是准入 —— 被打成 uncertain 的右边
    仍然会建边（`_judge_name` 只拦首尾和明显描述，不拦 `X人`）。
    准入严、置信度宽，才不会重演「边建不出来还查不到原因」。
    """
    if any(marker in canonical for marker in _DESCRIPTIVE_MARKERS):
        return ALIAS_UNCERTAIN
    blocked, _hit = _leading_blocks(canonical)
    if blocked:
        return ALIAS_UNCERTAIN
    if _is_descriptive_ren(canonical):
        return ALIAS_UNCERTAIN
    # 结尾助词（`那个ai中最弱智的`）。这里**不用 `_has_stop_char`**：
    # 那是全文扫描，`上` 也收在 `_STOP_CHARS` 里（来自「上下」），
    # 于是 `上海人` 被 downgrade 成 uncertain —— 又一个「单字做包含式
    # 匹配」的误伤。`_has_stop_char` 在本模块别处已被明确弃用，见
    # `warm_up_dictionary` 的注释。
    if canonical[-1:] in _DESCRIPTIVE_ENDINGS:
        return ALIAS_UNCERTAIN
    if len(canonical) > 6:
        return ALIAS_UNCERTAIN
    return ALIAS_EXACT


def extract_alias_claims(text: str) -> list[AliasClaim]:
    """从文本抽「A 也叫 B」的别名声明。

    只认显式等价词，不做推断 —— 推断是 LLM 层的事（P2）。
    规则层只负责把话说死了的那些抓出来，并且**如实标注置信度**。

    被拒的**不返回**（用 `extract_alias_claims_verbose` 连拒绝理由一起要）。
    """
    return extract_alias_claims_verbose(text)[0]


def extract_alias_claims_verbose(
    text: str,
) -> tuple[list[AliasClaim], list[AliasReject]]:
    """抽别名声明，**同时**返回被拒的和拒绝理由。

    ## 为什么要有 verbose 版

    左值被拒时原来直接 `continue` —— **连 claim 都不生成**。于是
    `种师道也叫老种` 这种句子在卡上、表上、图上全都看不见，
    LLM 复核层也拿不到它。现在被拒的进 `alias_rejects`，
    由 `remember` 落到 `alias_reject` 表。

    Returns:
        `(claims, rejects)`。`rejects` 里 `side` 恒为 ``"alias"`` ——
        右值的拒绝发生在建边阶段（`graph_outbox`），那边自己记。
    """
    claims: list[AliasClaim] = []
    rejects: list[AliasReject] = []
    seen: set[tuple[str, str]] = set()

    for match in _ALIAS_PATTERN.finditer(text):
        raw_alias = match.group(1).strip()
        raw_canonical = match.group(2).strip()
        alias = _strip_prefix(raw_alias)
        canonical = _strip_prefix(raw_canonical)
        if alias == canonical:
            continue
        if (alias, canonical) in seen:
            continue
        # 两边都得像个名字，不能把整句话当名字。
        #
        # **左侧也要查**，这是原来漏掉的一半：`三哥主打的就是有声有色有味`
        # 会匹配成 `三哥主打的 -> 有声有色有味`，右边过了判据，于是
        # `(:Alias {name:"三哥主打的"})` 进了图。
        # 左侧紧挨等价词，正则 `{2,12}` 会一路吞到谓语为止，比右侧更容易脏。
        verdict = _judge_name(alias)
        if not verdict.ok:
            rejects.append(
                AliasReject(
                    alias=alias,
                    canonical=canonical,
                    side="alias",
                    reason=verdict.reason,
                    detail=verdict.detail,
                )
            )
            continue
        if len(alias) < 2 or len(alias) > 12:
            rejects.append(
                AliasReject(
                    alias=alias,
                    canonical=canonical,
                    side="alias",
                    reason="alias_length",
                    detail=str(len(alias)),
                )
            )
            continue
        seen.add((alias, canonical))
        # **用原始串判置信度**：裁剪前缀会把描述性标记（`那个`）一起削掉，
        # 削完再看就「看起来像名字」了 —— `那个国产大模型` 会被误判成 exact。
        claims.append(
            AliasClaim(
                alias=alias,
                canonical=canonical,
                label=_alias_label(raw_canonical),  # type: ignore[arg-type]
                source="user_declared",
            )
        )
    return claims, rejects

#: jieba 是否可用。`None` = 还没探测过。
_JIEBA_OK: bool | None = None


def _jieba_ready() -> bool:
    """探测 jieba 是否可用（只探测一次，结果缓存）。

    首次 `import jieba.posseg` 要加载词典，约 1s。放在第一次抽取时发生，
    而不是模块导入时——否则 `import memory.services.labeler` 会拖慢所有冷启动。
    """
    global _JIEBA_OK
    if _JIEBA_OK is None:
        try:
            import jieba

            # jieba 加载词典时会往 **stderr** 打这几行：
            #
            #   Building prefix dict from the default dictionary ...
            #   Loading model from cache /tmp/jieba.cache
            #
            # MCP 契约要求 stderr 为空（只允许 stdout 走协议消息）。
            # 不压掉的话，预热词典就会让 stderr 冒字，端到端测试直接红。
            jieba.setLogLevel(logging.ERROR)

            import jieba.posseg  # noqa: F401

            _JIEBA_OK = True
        except Exception:  # noqa: BLE001 - 装了但坏了也要能降级
            _JIEBA_OK = False
    return _JIEBA_OK


def reset_jieba_probe() -> None:
    """清掉 jieba 探测缓存（测试用，模拟「jieba 不可用」）。"""
    global _JIEBA_OK, _DICT_READY
    _JIEBA_OK = None
    _DICT_READY = False


#: 静态词典是否已灌进 jieba。
_DICT_READY = False

#: 已经灌进 jieba 的实体名（`load_entity_dictionary` 用，避免重复 add_word）。
_DICT_LOADED: set[str] = set()


def _rank_appellations() -> list[str]:
    """`一哥` `二哥` … `九姨` —— 10 位数字 × 10 个称谓 = 100 个词。"""
    return [d + s for d in _RANK_DIGITS for s in _RANK_SUFFIXES]


def _ensure_dictionary() -> None:
    """把静态排行称谓灌进 jieba，只做一次。"""
    global _DICT_READY
    if _DICT_READY or not _jieba_ready():
        return
    import jieba

    for word in _rank_appellations():
        jieba.add_word(word, freq=10_000, tag="nr")
        _DICT_LOADED.add(word)
    _DICT_READY = True


def load_entity_dictionary(
    names: Any = (), *, persons: Any = (), entities: Any = ()
) -> int:
    """把图上已有的实体名灌进 jieba 词典，返回这次新加了多少个。

    ## 为什么需要「记反哺分词」

    `豆包` `糖包` `三哥` 这类名字 jieba 词典里没有，会被切成单字或标成
    普通名词，于是抽不出来。但它们**已经在图上了** —— 说明确实是名字。
    用记忆本身反哺分词，比穷举词表靠谱。

    ## 词性必须分开给

    标成 `nr`（人名）的词会被后续抽取当成人。把「项目A」标成 `nr`
    → 下次 `张三决定项目A延期` 会抽出「项目A」是个人。

    所以：

    - `persons` → `nr`（人名）
    - `entities` → `nz`（其他专名，不当人）

    `names` 是不知类别的兜底，按 `nz` 处理。

    ## 过滤

    **不做 `_has_stop_char` 全字检查**。那个函数是给「被截断的句子片段」
    用的，而这里的名字**已经在图上**，是抽取过一轮的结果。拿它再筛会误伤：
    「明」在 `_STOP_CHARS` 里（来自「证明」「说明」），于是「小明」被拒。

    只剥开头的连词（`和小王` → `小王`），剥完还是不合格就跳过。
    `_STOP_ENTITIES` 和 `_NOT_PERSON_WORDS` 仍然生效。

    重复调用是安全的：`_DICT_LOADED` 记住灌过的名字。
    """
    if not _jieba_ready():
        return 0
    _ensure_dictionary()
    import jieba

    # (名字, 词性)；同一名字出现两次取先来的（persons 优先）
    tagged: list[tuple[str, str]] = []
    tagged.extend((n, "nr") for n in (persons or ()))
    tagged.extend((n, "nz") for n in (entities or ()))
    tagged.extend((n, "nz") for n in (names or ()))

    added = 0
    for raw, tag in tagged:
        name = _strip_leading_conj((raw or "").strip())
        if len(name) < MIN_PERSON_LEN or len(name) > 8:
            continue
        if name in _DICT_LOADED or name in _STOP_ENTITIES:
            continue
        if name in _NOT_PERSON_WORDS:
            continue
        _DICT_LOADED.add(name)
        # freq 给高一点：太低的话 HMM 仍会把它切碎
        jieba.add_word(name, freq=100_000, tag=tag)
        added += 1
    return added


def reset_dictionary() -> None:
    """把 `load_entity_dictionary` 灌进去的词全部撤掉（测试用）。

    jieba 没有公开的删词 API：`del_word` 只是把频率压到 0，
    HMM 仍可能重新发现它，切分结果不保证回到原样。
    唯一干净的办法是直接从 `jieba.dt.FREQ` 里删。

    **为什么必须能撤**：词典是进程级全局状态，`remember` 会往里灌词。
    测试之间不清理的话，`test_dual_run` 灌的「项目A」会让
    `test_labeler` 把「项目A」抽成人 —— 单跑通过、全量失败，
    而且失败信息完全指不到真正的原因。
    """
    global _DICT_READY
    if not _jieba_ready():
        return
    import jieba

    for name in list(_DICT_LOADED):
        jieba.dt.FREQ.pop(name, None)
        tag_tab = getattr(jieba.dt, "user_word_tag_tab", None)
        if tag_tab is not None:
            tag_tab.pop(name, None)
    _DICT_LOADED.clear()
    _DICT_READY = False


def _strip_leading_conj(name: str) -> str:
    """剥掉粘连在名字前面的连词：`和小王` → `小王`。

    只在**剥完仍是合格名字**时才动手 —— 否则 `和平` 会被剥成 `平`
    （长度不足，会保留原样）。`同事` 也一样。
    """
    for conj in _LEADING_CONJ:
        if not name.startswith(conj):
            continue
        rest = name[len(conj) :]
        if len(rest) < MIN_PERSON_LEN:
            return name
        if rest in _NOT_PERSON_WORDS or rest in _STOP_ENTITIES:
            return name
        if _has_stop_char(rest):
            return name
        return rest
    return name


def _suffix_kind(suffix: str) -> str:
    if suffix in ("公司", "团队", "部门", "集团", "工作室"):
        return "org"
    return "topic"


def _extract_with_jieba(text: str) -> list[Entity]:
    """jieba 词性标注抽取。

    词性表：`nr` 人名 / `ns` 地名 / `nt` 机构 / `n`·`nz` 普通名词 / `v` 动词。

    比分词前的正则强在**有词边界**：`张三让李四去做项目A` 能被切成
    `张三/nr 让/v 李四/nr 去/v 做/v 项目/n A/x` —— 两个人和一个项目都拿得到，
    正则做不到（它会从「张三」一路吞到「项目」）。
    """
    import jieba.posseg as pseg

    _ensure_dictionary()

    tokens = [(w.word, w.flag) for w in pseg.cut(text)]
    persons: list[str] = []
    entities: list[Entity] = []
    seen: set[str] = set()

    def _add(name: str, kind: str) -> None:
        name = name.strip()
        if len(name) < 2 or name in seen or name in _STOP_ENTITIES:
            return
        seen.add(name)
        entities.append(Entity(name=name, kind=kind))  # type: ignore[arg-type]

    i = 0
    while i < len(tokens):
        word, flag = tokens[i]

        # 人名：裁掉尾部被误吞的函数词（`张三让` → `张三`），
        # 也剥掉粘连在前面的连词（`和小王` → `小王`）
        if flag == "nr":
            name = _strip_leading_conj(word)
            while len(name) > MIN_PERSON_LEN and name[-1] in _PERSON_TAIL_CHARS:
                name = name[:-1]
            name = _strip_leading_conj(name)
            if len(name) >= MIN_PERSON_LEN and name not in persons:
                persons.append(name)
            i += 1
            continue

        if flag == "ns":
            _add(word, "place")
            i += 1
            continue

        if flag == "nt":
            _add(word, "org")
            i += 1
            continue

        # 名词 + 编号：`项目` + `A` → `项目A`
        if flag in ("n", "nz", "vn") and i + 1 < len(tokens):
            nxt, nxt_flag = tokens[i + 1]
            if nxt_flag in ("x", "m", "eng") and nxt.isalnum() and len(nxt) <= 2:
                _add(word + nxt, "topic")
                i += 2
                continue
            # `腾讯` + `公司` → `腾讯公司`
            if nxt in ("公司", "团队", "部门", "集团", "工作室", "项目", "系统", "平台"):
                _add(word + nxt, _suffix_kind(nxt))
                i += 2
                continue

        # 已确认过的专名单独出现也收。
        #
        # 这是唯一的例外。`nz`（其他专名）默认只在上面的合并规则里收，
        # 因为「句子里随便一个名词」不可信 —— 那条规矩挡住过
        # `助手`/`接口`/`响应速度` 这类噪声。
        #
        # 但 `_DICT_LOADED` 里的词不一样：它们**已经在图上**，
        # 是之前抽取确认过的实体。`豆包` 就靠这一条，第二次起能单独收下。
        if flag == "nz" and word in _DICT_LOADED:
            _add(word, "topic")
            i += 1
            continue

        i += 1

    return [Entity(name=n, kind="person") for n in persons] + entities


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

    **优先走 jieba 分词**（有词性标注，人名/地名/机构名一步到位），
    jieba 不可用时降级到下面那套正则。降级路径必须可用——
    抽取失败会直接阻断写卡。

    最后补一道**别名声明**：`糖包就是豆包` 里的两个词都是名字，
    但 jieba 会把「糖包」标成普通名词 `n`，任何合并规则都收不到它。
    别名声明是规则层唯一**能确定**「这是个名字」的地方 —— 不是靠位置猜，
    是用户自己把它跟另一个名字连了等号。
    """
    # 第一段：权威名册（spec 035）
    #
    # 名册是**不完备的**（文本里可能有外部厂商、第三方），所以名册匹配
    # 完之后剩余文本**继续走 jieba** —— 顺序是「名册 > jieba」，
    # 不是「有名册就不用 jieba」。
    roster_entities, rest = _extract_with_roster(text)

    # 第二段：原有逻辑，跑在「挖掉名册命中」之后的剩余文本上。
    # 没有名册时 rest == text，行为和改动前完全一致。
    if _jieba_ready():
        entities = _extract_with_jieba(rest)
    else:
        entities = _extract_with_rules(rest)

    # 名册命中的排前面：它们比 jieba 的猜测可信
    seen = {e.name for e in roster_entities}
    merged = list(roster_entities) + [e for e in entities if e.name not in seen]

    # 别名声明仍在**原文**上做（挖掉后的文本会丢掉 `A 就是 B` 的结构）
    return _merge_alias_names(text, merged)


def _merge_alias_names(text: str, entities: list[Entity]) -> list[Entity]:
    """把别名声明两侧的干净名字并进实体列表。

    **为什么只收这一处**：实体纠错机制目前约等于不存在 ——
    没有任何 API 能删掉一个错误的 `(:Entity)` 节点，唯一手段是
    连整张卡一起 `memory_forget`，代价比错误本身还大。
    所以规则层宁可漏，也不能靠「句首名词」「4 字以内」这种位置特征去猜。

    别名声明是唯一的高可靠来源：用户说 `A 就是 B`，
    等于亲口断言 A 和 B 都是名字。这个不需要猜。

    描述性的不收（`那个AI里最弱智的`）—— 那本来就不是名字，
    走 `_is_clean_name` 同一个判据。
    """
    claims = extract_alias_claims(text)
    if not claims:
        return entities

    seen = {e.name for e in entities}
    merged = list(entities)
    for claim in claims:
        for name in (claim.alias, claim.canonical):
            if not name or name in seen:
                continue
            if not _is_clean_name(name):
                continue
            if _has_stop_char(name):
                continue
            seen.add(name)
            # 别名声明说的是「这两个称呼指同一类东西」，分不出人还是物。
            # 标 topic 而不是 person：宁可让它作为泛实体参与召回，
            # 也不要凭空把「豆包」认成一个人。
            merged.append(Entity(name=name, kind="topic"))
    return merged


def _extract_with_rules(text: str) -> list[Entity]:
    """正则兜底路径（jieba 不可用时）。

    能力明显弱于分词：`张三让李四去做项目A` 这种含两个人和一个项目，
    靠姓氏 + 位置约束抽不全。但它是**零依赖**的，必须保底可用。
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
    #: 这里的「A 也叫 B」声明。**不参与 event_id 哈希**。
    alias_claims: list[AliasClaim] = field(default_factory=list)
    #: 被拒的别名声明（带理由）。**不参与 event_id 哈希**，只用于留痕。
    alias_rejects: list[AliasReject] = field(default_factory=list)
    #: 本段文本里声明的姓氏（`这人姓种` → `["种"]`）。由 `remember` 落库。
    surnames: list[str] = field(default_factory=list)
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
    """规则层打标：永远给出非空 types（否则事件卡无法落库）。

    ## 姓氏声明先于别名抽取

    `这人姓种，种小明就是老种` 这一句里，别名抽取**必须**先知道
    「种」是姓，否则 `种小明` 会被量词判据拒掉。所以顺序是：

        parse_surname_declarations → register_surnames → extract_alias_claims

    副作用（写 `_DECLARED_SURNAMES`）是有意的：`labeler` 是纯文本层，
    没有 namespace 和落库能力。持久化由 `remember` 紧接着做
    （`surname_decl` 表），下次启动再灌回来。

    **不影响 `event_id`**：姓氏只影响别名判据，`event_id` 里的
    `entity_names` 来自 jieba 抽取，不看这套判据。
    """
    types = _match_types(summary, vocab or [])
    surnames = parse_surname_declarations(summary)
    if surnames:
        register_surnames(surnames)
    entities = extract_entities(summary)
    actions = extract_actions(summary, entities=entities)
    claims, rejects = extract_alias_claims_verbose(summary)
    return LabelResult(
        types=types,
        entities=entities,
        actions=list(actions),
        alias_claims=claims,
        alias_rejects=rejects,
        surnames=surnames,
        source="rules",
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
