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
    AGENT_KEY_HEADER: str = "X-Agent-Key"

    # ---------- 召回：条数与阈值 ----------
    ARCHIVE_WINDOW_DAYS: int = 180
    RECALL_TOP_K_MIN: int = 3
    RECALL_TOP_K_MAX: int = 30
    RECALL_DROP_RATIO: float = 0.6
    RECALL_GAP_RATIO: float = 0.15
    CONTEXT_TOKEN_BUDGET: int = 3000
    RECALL_COUNT_MIN_SCORE: float = 0.5
    RECALL_EMPTY_THRESHOLD: float = 0.35

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
    CHUNK_KEEP_PER_EVENT: int = 2

    # ---------- consolidation（P2） ----------
    CONSOLIDATE_MIN_EVENTS: int = 5000
    CONSOLIDATE_DUP_RATIO: float = 0.4
    CONSOLIDATE_LATENCY_MS: int = 300
    GRAPH_HUB_DEGREE: int = 8
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
    RERANK_TIMEOUT_MS: int = 300

    LLM_BASE_URL: str = "http://127.0.0.1:3000/v1"
    LLM_MODEL: str = "deepseek-chat"
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
