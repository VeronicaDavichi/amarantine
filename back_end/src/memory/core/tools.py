"""MCP 工具契约（LLM 侧）。

`description` 会被模型直接读到，**它决定工具调不调**——措辞按「何时调 / 何时不调」写，
不要写成函数说明。完整指南见 docs/memory-tool-guide.md（E3 后由它生成 instructions）。
"""

from __future__ import annotations

from typing import Any

SERVER_NAME = "amarantine-memory"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

#: M1 阶段的精简版 instructions；E3 换成从 guide 生成
INSTRUCTIONS = """\
你有 4 个长期记忆工具（跨会话持久）：
- memory_recall：用户提到过去（「上次/之前/那个」）、问你记不记得、或你要陈述具体事实时调用。纯知识问答与寒暄不要调用。
- memory_remember：用户明确要求记住、或发生长期有效的决定/约定/状态变化/偏好时调用。summary 必须写「谁+做了什么+结果如何」，禁止抽象概括。
- memory_forget：用户要求忘记或指出信息过时的时候调用；只做软删，原文仍保留。
- memory_feedback：本次回答实际引用了哪些 memory_recall 返回的 event_id 就报哪些；没用到就不要调用，不许虚报。
"""

TOOLS: dict[str, dict[str, Any]] = {
    "memory_recall": {
        "name": "memory_recall",
        "description": (
            "查长期记忆。当用户提到过去发生的事（「上次」「之前」「那个项目」「我们说好的」）、"
            "问你记不记得、或你需要陈述具体事实（日期/决定/偏好/承诺）时调用。"
            "纯知识问答、寒暄、答案已在本轮上下文中时不要调用。"
            "返回 materials=[] 表示确实没有相关记忆，此时不要编造。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索意图，用关键词组合而非整句问话",
                    "minLength": 1,
                },
                "token_budget": {
                    "type": "integer",
                    "description": "返回材料的 token 预算，缺省取服务端配置",
                    "minimum": 200,
                    "default": 3000,
                },
                "top_k": {
                    "type": ["integer", "null"],
                    "description": "调试用硬上限；null 表示走动态截断",
                    "default": None,
                },
                "budget_ms": {
                    "type": "integer",
                    "description": "整体耗时预算",
                    "default": 1500,
                },
                "now": {
                    "type": "string",
                    "description": "当前时间（ISO 8601），用于时效计算与相对时间解析",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "memory_remember": {
        "name": "memory_remember",
        "description": (
            "记下值得长期保留的事。当用户明确要求「记住」、或发生长期有效的决定/约定/状态变化/稳定偏好时调用。"
            "临时信息、闲聊、可从上下文推出的内容不要调用。"
            "summary 必须具体：谁 + 做了什么 + 结果如何；"
            "写成抽象概括会让这张卡和其它卡在向量空间里糊在一起。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "事件摘要：谁做了什么、结果如何。必须具体，禁止抽象概括",
                    "minLength": 1,
                },
                "date_hint": {
                    "type": "string",
                    "description": "自然语言时间线索（如「上个月」「去年底」），服务端会解析",
                },
                "pinned": {
                    "type": "boolean",
                    "description": "只有用户明确要求记住时才填 true；不要因为你觉得重要就填",
                    "default": False,
                },
                "important": {
                    "type": "boolean",
                    "description": "用户强调「这很重要」「千万别忘」时填 true",
                    "default": False,
                },
                "emotion_intensity": {
                    "type": "number",
                    "description": "用户情绪强度 0–1；只影响重要度加成，不构成保护",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.0,
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
    "memory_forget": {
        "name": "memory_forget",
        "description": (
            "遗忘一条记忆。当用户说「忘了这个」「删掉那条」「不用记了」，"
            "或明确指出某条信息已过时且不需要保留时调用。"
            "不要因为你觉得自己判断某条不重要就调用。只做软删：检索不到，但原文保留供审计。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "要遗忘的事件卡 id（通常先用 memory_recall 定位）",
                    "pattern": "^evt_[0-9a-f]{16}$",
                },
                "reason": {
                    "type": "string",
                    "enum": ["user_forget", "outdated"],
                    "description": "遗忘原因",
                    "default": "user_forget",
                },
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
    },
    "memory_feedback": {
        "name": "memory_feedback",
        "description": (
            "回报本次回答实际引用了哪些记忆材料。"
            "只报真正用到的 event_id；一条都没用到就不要调用。"
            "虚报会污染「有效引用」统计，导致保护机制失效。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "trace_id": {
                    "type": "string",
                    "description": "memory_recall 返回的 trace_id",
                    "minLength": 1,
                },
                "used_material_ids": {
                    "type": "array",
                    "description": "本次实际引用的 event_id 列表",
                    "items": {"type": "string", "pattern": "^evt_[0-9a-f]{16}$"},
                    "minItems": 1,
                },
            },
            "required": ["trace_id", "used_material_ids"],
            "additionalProperties": False,
        },
    },
}

TOOL_NAMES: tuple[str, ...] = tuple(TOOLS)
