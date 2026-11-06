"""A1 配置加载。

优先级：显式传参 > 环境变量 / .env > yaml > 代码默认值。
所有配置项与 ADR D6 一一对应；新增字段必须同步更新 D6 表格。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# namespace 规则与附录 B 的 JSON Schema 保持一致
NAMESPACE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# 由 load_settings() 注入，供 _YamlSource 读取
_YAML_PATH: Path | None = None


class _YamlSource(PydanticBaseSettingsSource):
    """最低优先级来源：yaml 文件。env 与 init 均可覆盖它。"""

    def __init__(self, settings_cls: type[BaseSettings], path: Path | None) -> None:
        super().__init__(settings_cls)
        self._path = path

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # yaml 整份读入，不逐字段取值
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if self._path is None or not self._path.exists():
            return {}
        data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"配置文件必须是 mapping: {self._path}")
        return {str(k).upper(): v for k, v in data.items()}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 身份与隔离 ----------
    MEMORY_NAMESPACE: str | None = Field(
        default=None,
        description="MCP 侧由 MCP host 通过环境变量注入；Admin 侧不使用",
    )
    #: 管理接口读 token 的 header 名。
    #: **默认 `Authorization`**：实现一直是 Bearer，ADR v0.10 写的 `X-Agent-Key`
    #: 从未实现过 —— 配置的默认值不该描述一个不存在的协议（2026-11-04 审计）。
    #: 值等于 `Authorization`（不区分大小写）时按 `Bearer <token>` 解析；
    #: 其它名字则整个 header 值就是 token。
    AGENT_KEY_HEADER: str = "Authorization"

    # ---------- 召回：条数与阈值 ----------
    ARCHIVE_WINDOW_DAYS: int = 180
    RECALL_TOP_K_MIN: int = 3
    RECALL_TOP_K_MAX: int = 30
    #: 每路召回的候选条数（ADR：候选每路 top 50，再交给动态 K 截断）
    RECALL_CANDIDATES: int = 50
    RECALL_DROP_RATIO: float = 0.6
    RECALL_GAP_RATIO: float = 0.15
    CONTEXT_TOKEN_BUDGET: int = 3000
    RECALL_COUNT_MIN_SCORE: float = 0.5
    RECALL_EMPTY_THRESHOLD: float = 0.35
    #: 向量通道的绝对余弦闸门。
    #: **必须在融合前过滤**：RRF 只保留排名，`normalize_relevance` 又把 top-1
    #: 归一成 1.0，于是无关查询也会拿到接近满分。绝对相似度是唯一还能区分
    #: 「真相关」和「矮子里拔将军」的信号。
    #: bge-m3 中文短句实测：相关 0.6–0.8，无关 0.35–0.50，故取 0.5。
    RECALL_VECTOR_MIN_SIM: float = 0.5

    # ---------- 召回：超时 ----------
    GRAPH_TIMEOUT_MS: int = 200
    VECTOR_TIMEOUT_MS: int = 800
    RECALL_BUDGET_MS: int = 1500

    # ---------- 时间与排序 ----------
    TIMEZONE: str = "Asia/Shanghai"
    RECENCY_HALF_LIFE_DAYS: int = 180
    W_TIME_BASE: float = 0.7
    W_IMP_BASE: float = 0.8
    TOKENIZER: str = "cl100k_base"
    ENTITY_ALIAS_SIM: float = 0.92

    # ---- 实体消歧：确定性规则层（spec 026）----
    #
    # ADR C.B3 原本的 ② 级是「实体名 embedding 余弦 >= ENTITY_ALIAS_SIM」。
    # 实测（bge-m3，14 组中文实体名对照）证明该特征**不可分**：
    #   应合并最低 0.4806（周会/每周例会）
    #   不该合并最高 0.8801（项目A/项目A组）
    # 区间完全重叠，任何单一阈值都同时误并和漏并。
    # 所以 ② 级改成确定性规则层，向量方案作废（ENTITY_ALIAS_SIM 保留待重评）。

    #: 规则层总开关。关掉 = 完全回退到「只有 ③ LLM」的旧行为。
    ENTITY_RULE_ENABLED: bool = True

    #: 后缀白名单：短名是长名前缀、且多出的部分在此表内 -> 判同一实体。
    #: 逗号分隔；**显式设为空字符串表示禁用这条规则**（不是用默认值）。
    #: 注意「组」故意不在默认值里 —— 项目A/项目A组 是不同实体（实测 0.8801）。
    ENTITY_SUFFIXES: str = (
        "AI,助手,公司,集团,科技,股份,有限公司,市,省,县,区,镇,部,处,科,院,局"
    )

    #: 称呼后缀：剥掉后相等 -> 判同一实体。逗号分隔；空字符串禁用。
    #: 剥离后长度必须 >= 2，否则「王总 -> 王」会被误判（单字名无法判定）。
    ENTITY_HONORIFICS: str = "总,经理,老师,哥,姐,先生,女士,同学,工"

    #: 同义词表：`a=b,c=d` 格式，双向生效。空字符串禁用。
    #: 这是**承认规则能力边界**的做法 —— 周会/每周例会 字面不包含、
    #: 余弦仅 0.4806，任何规则都判不出来，只能靠词典。
    ENTITY_SYNONYMS: str = "周会=每周例会,例会=每周例会,月会=每月例会,腾讯=Tencent,阿里=Alibaba"

    TIME_CONF_MIN: float = 0.5

    # ---------- 标签 ----------
    TYPES_VOCAB: list[str] = Field(default_factory=list)
    LABEL_DUAL_RUN: bool = True
    LABEL_DUAL_RUN_JACCARD: float = 0.8
    LABEL_LLM_TEMP: float = 0.2
    LABEL_REVIEW_BATCH: int = 50

    # ---------- 遗忘 ----------
    FORGET_SWEEP_DAYS: int = 30
    FORGET_IDLE_DAYS: int = 180
    FORGET_GRACE_DAYS: int = 90
    FORGET_MIN_RECALLS: int = 1
    PROTECT_IMPORTANCE_REVIEW: float = 0.7
    CONFIRMED_MIN: int = 2

    # ---------- chunk 层（A16，默认关）----------
    #: 关掉时写入/检索路径完全不变（连 chunk_store 都不会被 import）。
    #: 打开后长 summary 按 CHUNK_SIZE 滑窗切块、各存一个向量，
    #: 检索命中 chunk 再聚合回卡 —— 一条卡最多保留 CHUNK_KEEP_PER_EVENT 个块。
    CHUNK_ENABLED: bool = False
    #: 每块字符数。中文按字符切，256 字约 256 token，远小于 bge-m3 的 8192 上限。
    CHUNK_SIZE: int = 256
    #: 相邻块重叠字符数，防止关键信息正好被切断。
    CHUNK_OVERLAP: int = 32
    CHUNK_KEEP_PER_EVENT: int = 2

    # ---------- consolidation（P2） ----------
    CONSOLIDATE_MIN_EVENTS: int = 5000
    CONSOLIDATE_DUP_RATIO: float = 0.4
    CONSOLIDATE_LATENCY_MS: int = 300
    GRAPH_HUB_DEGREE: int = 8
    #: 孤岛判定（spec 022）：事件级邻域 <= 此值视为孤立子图。
    #:
    #: **0 = 关闭（默认）**，与 `GRAPH_HUB_DEGREE` 的 `0=关` 惯例一致。
    #: 关闭的理由不是「没做完」，是「冷启动阶段数据少，孤立是常态而非异常」——
    #: 建议等 namespace 有 50+ 条真实记忆后再设为 2。
    ISLAND_MAX_NEIGHBORS: int = 0
    #: 孤岛判定的数据量门槛：namespace 事件数低于此值时不判孤岛。
    #: 没有它，只有 3 条记忆时会把全部判成孤岛并一起加速淘汰。
    ISLAND_MIN_EVENTS: int = 50
    CLUSTER_MIN_SIZE: int = 15
    CLUSTER_MERGE_SIM: float = 0.85
    COLD_QUERY_P95_MS: int = 800

    # ---------- 模型供给 ----------
    EMBEDDING_PROVIDER: str = "local"
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_BASE_URL: str = "http://127.0.0.1:8082"
    EMBEDDING_DIM: int = 1024
    EMBEDDING_API_KEY: str | None = None

    RERANK_ENABLED: bool = False
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"
    RERANK_BASE_URL: str = "http://127.0.0.1:8083"
    RERANK_CANDIDATES: int = 50
    #: CPU 上 bge-reranker-v2-m3 实测热身后 0.22-0.26s、冷启动更久。
    #: 原来 300ms 卡在边界上，真服务一开就几乎必然 ReadTimeout（等于白开）。
    #: 800ms 留足余量，且仍在 RECALL_BUDGET_MS=1500 之内。
    RERANK_TIMEOUT_MS: int = 800

    #: `anthropic`（Messages API）或 `openai`（chat/completions）。
    #: 本机接的是 hongwang 网关，走 Anthropic 协议。
    LLM_PROVIDER: str = "anthropic"
    LLM_BASE_URL: str = "http://192.168.0.58:3000"
    LLM_MODEL: str = "deepseek-v4-flash"
    LLM_API_KEY: str | None = None
    LLM_TIMEOUT_MS: int = 60000

    # ---------- 语言信号（C.B13 v0.11 形态） ----------
    USER_DIRECTIVE_DAILY_MAX: int = 20
    EMOTION_BOOST: float = 0.15
    EXPLICIT_IMPORTANT_FLOOR: float = 0.85

    # ---------- 存储 ----------
    DATABASE_URL: str = "sqlite:///./data/memory.db"
    SQLITE_WAL: bool = True
    DB_BUSY_TIMEOUT_MS: int = 5000
    WEAVIATE_URL: str = "http://127.0.0.1:8081"
    #: gRPC 端口与 REST 不同（本机为避开 Dify 占用改成 50061）
    WEAVIATE_GRPC_PORT: int = 50061
    NEO4J_URI: str = "bolt://127.0.0.1:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "password"

    # ---------- 运维 ----------
    GUIDE_CHECK_CRON: str = "0 9 * * 1"
    ADMIN_TOKEN: str | None = None
    ADMIN_HOST: str = "127.0.0.1"
    ADMIN_PORT: int = 8765
    LOG_FILE: str = "logs/memory.log"
    LOG_LEVEL: str = "INFO"

    # ---------- 校验 ----------

    @field_validator("MEMORY_NAMESPACE")
    @classmethod
    def _check_namespace(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not NAMESPACE_PATTERN.fullmatch(v):
            raise ValueError(
                f"MEMORY_NAMESPACE 非法: {v!r}；须匹配 ^[a-z0-9][a-z0-9_-]{{0,63}}$"
            )
        return v

    @field_validator("W_TIME_BASE", "W_IMP_BASE", "RECALL_DROP_RATIO", "RECALL_GAP_RATIO")
    @classmethod
    def _check_ratio(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"比例型配置须在 [0, 1]：{v}")
        return v

    @field_validator("EMBEDDING_DIM")
    @classmethod
    def _check_dim(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"EMBEDDING_DIM 必须为正：{v}")
        return v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls, _YAML_PATH),
            file_secret_settings,
        )


def load_settings(
    yaml_path: str | Path | None = None,
    **overrides: Any,
) -> Settings:
    """加载配置。

    Args:
        yaml_path: yaml 配置文件路径；None 表示不读 yaml。
        **overrides: 显式传参，优先级最高。

    Returns:
        完成校验的 Settings 实例。
    """
    global _YAML_PATH
    _YAML_PATH = Path(yaml_path) if yaml_path is not None else None
    try:
        return Settings(**overrides)
    finally:
        _YAML_PATH = None
