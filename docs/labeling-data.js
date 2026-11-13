window.LABEL_DATA = {
 "generated_at": "2026-11-13",
 "instructions": {
  "statement": "判断这句话是否真的表达了「谁负责/维护/依赖什么」；machine 是规则抽取结果，抽错也算错。",
  "behavior": "判断这个人对目标的动作是否构成**稳定关系**（≥2 次且像长期职责），偶发的算「偶发」。",
  "sense": "判断这个词在群里是否真的指这个意思；上下文不足以判断时选「不确定」。"
 },
 "statements": [
  {
   "kind": "statement",
   "id": "stmt-pos-1",
   "evidence": "小鲸鱼称自家是头一号住户，克劳德负责写门牌号并把尘霜记为第二号",
   "machine": {
    "head": "克劳德",
    "predicate": "OWNS",
    "tail": "写门牌号",
    "extracted": true
   },
   "source": "rule_hit"
  },
  {
   "kind": "statement",
   "id": "stmt-pos-2",
   "evidence": "讨论给身份加内容时应让AI自行观察而非提问，记忆只负责记住",
   "machine": {
    "head": "记忆只",
    "predicate": "OWNS",
    "tail": "记住",
    "extracted": true
   },
   "source": "rule_hit"
  },
  {
   "kind": "statement",
   "id": "stmt-neg-3",
   "evidence": "朱倩表示请一位分支机构老师一起的事情还在沟通",
   "machine": {
    "head": null,
    "predicate": null,
    "tail": null,
    "extracted": false
   },
   "source": "rule_miss"
  },
  {
   "kind": "statement",
   "id": "stmt-neg-4",
   "evidence": "棉棉担心两个攻击力强的角色待在一起会出事",
   "machine": {
    "head": null,
    "predicate": null,
    "tail": null,
    "extracted": false
   },
   "source": "rule_miss"
  },
  {
   "kind": "statement",
   "id": "stmt-neg-5",
   "evidence": "乌角鲨在群里大笑并问对方家的鱼怎么不出来一起笑",
   "machine": {
    "head": null,
    "predicate": null,
    "tail": null,
    "extracted": false
   },
   "source": "rule_miss"
  },
  {
   "kind": "statement",
   "id": "stmt-neg-6",
   "evidence": "任泽昂询问老师们现在是否有空一起线上交流反洗钱智能体的方案，预计二三十分钟",
   "machine": {
    "head": null,
    "predicate": null,
    "tail": null,
    "extracted": false
   },
   "source": "rule_miss"
  },
  {
   "kind": "statement",
   "id": "stmt-neg-7",
   "evidence": "Valhalla追问服务器含义，便携式解释这是让鱼儿们聚在一起自行经营生活的空间",
   "machine": {
    "head": null,
    "predicate": null,
    "tail": null,
    "extracted": false
   },
   "source": "rule_miss"
  },
  {
   "kind": "statement",
   "id": "stmt-neg-8",
   "evidence": "任泽昂提出找张老师一起了解清洗数据怎么构建MCP接口",
   "machine": {
    "head": null,
    "predicate": null,
    "tail": null,
    "extracted": false
   },
   "source": "rule_miss"
  },
  {
   "kind": "statement",
   "id": "stmt-neg-9",
   "evidence": "任泽昂问几位老师五点有没有空一起看下现在的产品效果",
   "machine": {
    "head": null,
    "predicate": null,
    "tail": null,
    "extracted": false
   },
   "source": "rule_miss"
  },
  {
   "kind": "statement",
   "id": "stmt-neg-10",
   "evidence": "群友讨论把鱼都放一起会怎样，戏称会赛博吵架、污染文本库、左脚踩右脚",
   "machine": {
    "head": null,
    "predicate": null,
    "tail": null,
    "extracted": false
   },
   "source": "rule_miss"
  }
 ],
 "behaviors": [
  {
   "kind": "behavior",
   "id": "beh-1",
   "person": "王逗",
   "verb": "讨论",
   "target": "SX",
   "evidence_count": 1,
   "evidence": [
    "王逗比–SX在群里转发了聊天记录"
   ],
   "source": "behavior"
  },
  {
   "kind": "behavior",
   "id": "beh-2",
   "person": "安辞",
   "verb": "讨论",
   "target": "Ferry",
   "evidence_count": 1,
   "evidence": [
    "Ferry.安辞在群里分享了一段聊天记录"
   ],
   "source": "behavior"
  },
  {
   "kind": "behavior",
   "id": "beh-3",
   "person": "毛绒",
   "verb": "发布",
   "target": "海洋",
   "evidence_count": 1,
   "evidence": [
    "kk发布第五人格蒸汽朋克海洋风毛绒玩偶挂件上架消息"
   ],
   "source": "behavior"
  },
  {
   "kind": "behavior",
   "id": "beh-4",
   "person": "毛绒",
   "verb": "发布",
   "target": "ENSKY",
   "evidence_count": 1,
   "evidence": [
    "kk发布咒术回战5周年ENSKY猫耳毛绒玩偶上架消息"
   ],
   "source": "behavior"
  },
  {
   "kind": "behavior",
   "id": "beh-5",
   "person": "怀玉玉",
   "verb": "发布",
   "target": "OPED",
   "evidence_count": 1,
   "evidence": [
    "kk发布咒术回战涉谷事变与怀玉玉折OPED拍立得上架消息"
   ],
   "source": "behavior"
  },
  {
   "kind": "behavior",
   "id": "beh-6",
   "person": "阴晴不定",
   "verb": "发布",
   "target": "哥哥",
   "evidence_count": 1,
   "evidence": [
    "kk发布《阴晴不定的大哥哥》公主咖啡和服系列上架消息"
   ],
   "source": "behavior"
  },
  {
   "kind": "behavior",
   "id": "beh-7",
   "person": "克莉丝",
   "verb": "发布",
   "target": "助理多次",
   "evidence_count": 1,
   "evidence": [
    "克莉丝之屋与小助理多次发布女巫笔记系列四团的尾款通知（3月20日晚8点至4月3日截止，10个工作日内发货）"
   ],
   "source": "behavior"
  },
  {
   "kind": "behavior",
   "id": "beh-8",
   "person": "阿强",
   "verb": "讨论",
   "target": "广东",
   "evidence_count": 1,
   "evidence": [
    "群聊围绕给广东蟑螂起个体面名字打趣，从小强、阿强玩到阿珍的梗"
   ],
   "source": "behavior"
  },
  {
   "kind": "behavior",
   "id": "beh-9",
   "person": "阿珍",
   "verb": "讨论",
   "target": "广东",
   "evidence_count": 1,
   "evidence": [
    "群聊围绕给广东蟑螂起个体面名字打趣，从小强、阿强玩到阿珍的梗"
   ],
   "source": "behavior"
  },
  {
   "kind": "behavior",
   "id": "beh-10",
   "person": "修狗",
   "verb": "同意",
   "target": "宝宝",
   "evidence_count": 1,
   "evidence": [
    "我九不狸你发起升级版复读，群友跟风同意所有小鸡、咪咪、修狗和小鼠宝宝都黏在他身上"
   ],
   "source": "behavior"
  }
 ],
 "senses": [
  {
   "kind": "sense",
   "id": "sense-1",
   "term": "dd",
   "gloss": "她家的鱼并发",
   "score": 3,
   "evidence": [
    "凉橙向托确认dd是她家的鱼并发表情",
    "诶嘿称token欠费，询问如何让DSH接入workbuddy以节省开销"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-2",
   "term": "deepdeep",
   "gloss": "大肥鱼假扮的",
   "score": 3,
   "evidence": [
    "幽幽子用欢迎语迎接新人deepdeep",
    "deepdeep回应并反驳「所有人都是大肥鱼假扮的」的说法"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-3",
   "term": "deepdeep",
   "gloss": "谁，对方自称",
   "score": 3,
   "evidence": [
    "幽幽子用欢迎语迎接新人deepdeep",
    "deepdeep回应并反驳「所有人都是大肥鱼假扮的」的说法"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-4",
   "term": "lo",
   "gloss": "毕业典礼，并觉得",
   "score": 3,
   "evidence": [
    "乌角鲨考虑引入Prolog，并因技术问题头痛自嘲",
    "点歌the river并追问对方听没听过lost river，随后吐槽没人会听并推荐珊蔻的old melodie"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-5",
   "term": "专业",
   "gloss": "往喜欢的方向",
   "score": 3,
   "evidence": [
    "猛虎说明自己GPA为3.0/5.0、平均分76且因转专业学分较多，胖吉评价选校情况",
    "胖吉提到高校留学项目变数大，有人读到一半专业被取消"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-6",
   "term": "人",
   "gloss": "猫」那个视频",
   "score": 3,
   "evidence": [
    "牛魔人在群里转发了聊天记录",
    "有人在群里发了名片"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-7",
   "term": "人",
   "gloss": "猫》完整版，",
   "score": 3,
   "evidence": [
    "牛魔人在群里转发了聊天记录",
    "有人在群里发了名片"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-8",
   "term": "人",
   "gloss": "猫》只听",
   "score": 3,
   "evidence": [
    "牛魔人在群里转发了聊天记录",
    "有人在群里发了名片"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-9",
   "term": "人",
   "gloss": "猫》这首歌",
   "score": 3,
   "evidence": [
    "牛魔人在群里转发了聊天记录",
    "有人在群里发了名片"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-10",
   "term": "兔女郎",
   "gloss": "nn系列不得不品鉴",
   "score": 3,
   "evidence": [
    "提到兔女郎是nn系列不得不品鉴的一环，并自述曾是氪金玩家"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-11",
   "term": "内容",
   "gloss": "猫和老鼠后复读吐",
   "score": 3,
   "evidence": [
    "传讯员8号发布今日梦谷小屋上新内容及图片",
    "传讯员8号发布今日梦谷小屋上新内容及图片"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-12",
   "term": "内容",
   "gloss": "“大肥鱼吹",
   "score": 3,
   "evidence": [
    "传讯员8号发布今日梦谷小屋上新内容及图片",
    "传讯员8号发布今日梦谷小屋上新内容及图片"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-13",
   "term": "分词器",
   "gloss": "什么，猜测可能",
   "score": 3,
   "evidence": [
    "乌角鲨追问棉棉用的分词器是什么，猜测可能是jieba或es-ik等",
    "乌角鲨表示看不懂，让棉棉直接问对方分词器和插件名字"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-14",
   "term": "前提",
   "gloss": "口语过关且能",
   "score": 3,
   "evidence": [
    "棉棉询问此前提到的“落”的情况",
    "新机子瓦一兔子建议猛虎尝试英语客服类工作，前提是口语过关且能接受两班倒"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-15",
   "term": "博",
   "gloss": "个人物",
   "score": 3,
   "evidence": [
    "心动与纳垢慈父通知群友今晚七点在微博直播（免费，账号名“斯奎奇大王牢A”）",
    "[有人@我]我怀孕了称姜文博是个人物"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-16",
   "term": "卡",
   "gloss": "毕业生卡",
   "score": 3,
   "evidence": [
    "kk发布新品速递并介绍坂本闪卡上架与价格",
    "重发布暗杀教室花札风透卡卡片补盒通知"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-17",
   "term": "原以为",
   "gloss": "纯日语歌，",
   "score": 3,
   "evidence": [
    "聊《人是猫》这首歌，说除副歌外没听过，原以为是纯日语歌，感叹这类混搭不少"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-18",
   "term": "叔叔",
   "gloss": "谁，B站",
   "score": 3,
   "evidence": [
    "B站峥空白称今天账户涨停、梁叔叔没砸他，莫蒂斯缇感叹财运93应验",
    "莫蒂斯缇追问梁叔叔是谁，B站峥空白回答是梁文峰，莫蒂斯缇调侃没来砸所以涨停"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-19",
   "term": "名字",
   "gloss": "两个女儿自己取",
   "score": 3,
   "evidence": [
    "顾辉扬诺夫在群里发了自己的名字",
    "看到我让我去做题在群里发了「Sakuraba Ema」这个名字"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  },
  {
   "kind": "sense",
   "id": "sense-20",
   "term": "向日葵",
   "gloss": "远程桌面",
   "score": 3,
   "evidence": [
    "乌角鲨建议改用向日葵远程桌面，并称本机扔出来更便宜",
    "往汐反驳说向日葵当长期跑图不划算，一卡全白搭"
   ],
   "scope": "qq-505456846",
   "source": "reference"
  }
 ]
};
